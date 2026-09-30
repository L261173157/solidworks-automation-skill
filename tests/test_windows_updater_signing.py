"""Keep temporary CI signing isolated from the production updater trust root."""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/windows-release.yml"
VERIFY = ROOT / "scripts/verify_updater_release.ps1"
BUILD = ROOT / "scripts/build_windows_release.ps1"
PWSH = shutil.which("pwsh")
POWERSHELL = PWSH or shutil.which("powershell")
PRODUCTION_KEY = base64.b64encode(b"production-public-key").decode()
TEST_KEY = base64.b64encode(b"temporary-ci-public-key").decode()


def signing_step():
    source = WORKFLOW.read_text(encoding="utf-8")
    step = source.split("      - name: Configure updater signing key\n", 1)[1]
    step = step.split("      - name: Build release packages\n", 1)[0]
    return "\n".join(line[10:] for line in step.split("        run: |\n", 1)[1].splitlines())


def run_powershell(tmp_path, source, environment, *, pwsh_only=False):
    shell = PWSH if pwsh_only else POWERSHELL
    if not shell:
        pytest.skip("PowerShell is unavailable; behavioral contracts run in Windows CI")
    script = tmp_path / "harness.ps1"
    script.write_text('$ErrorActionPreference = "Stop"\n' + source, encoding="utf-8")
    env = os.environ.copy()
    for name in (
        "GITHUB_ACTIONS", "GITHUB_REF", "CAD_STUDIO_UPDATER_SIGNING_MODE",
        "CAD_STUDIO_TEST_UPDATER_PUBLIC_KEY", "RELEASE_SIGNING_KEY",
        "RELEASE_SIGNING_KEY_PASSWORD", "TAURI_SIGNING_PRIVATE_KEY",
        "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
    ):
        env.pop(name, None)
    env.update(environment)
    return subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
        env=env, text=True, encoding="utf-8", errors="replace", capture_output=True,
        timeout=30, check=False,
    )


def test_workflow_masks_secrets_before_export_and_suppresses_signer_output():
    step = signing_step()
    generate = step.index("npx tauri signer generate")
    assert step.index("Protect-LogValue $signingPassword") < generate
    assert "--ci *> $null" in step
    assert step.index("Protect-LogValue $signingKey") < step.index("TAURI_SIGNING_PRIVATE_KEY<<")
    assert "foreach ($line" in step and "::add-mask::$escaped" in step
    assert ".Replace('%', '%25')" in step
    assert "finally {" in step and "Remove-Item -LiteralPath $keyPath -Force" in step


def test_workflow_release_guard_precedes_fallback_and_publish_requires_production():
    step = signing_step()
    assert step.index('if ($env:GITHUB_REF -like "refs/tags/v*")') < step.index("npx tauri signer generate")
    assert "TAURI_SIGNING_PRIVATE_KEY is required for a public release tag." in step
    assert "$env:GITHUB_ACTIONS -ne \"true\"" in step
    assert "$env:GITHUB_REF -notmatch '^refs/(heads|pull)/'" in step
    workflow = WORKFLOW.read_text(encoding="utf-8")
    publish = workflow.split("      - name: Publish GitHub Release\n", 1)[1]
    assert "if: startsWith(github.ref, 'refs/tags/v') && env.CAD_STUDIO_UPDATER_SIGNING_MODE == 'production'" in publish
    assert "&& 'release' || 'ci-test'" in workflow
    assert "CI-TEST-ONLY.txt" not in publish


def test_temporary_key_never_replaces_embedded_production_trust():
    source = VERIFY.read_text(encoding="utf-8")
    assert "$verificationPublicKey = [string]$tauriConfig.plugins.updater.pubkey" in source
    assert "if ($isTestSigning) { $verificationPublicKey = $TestPublicKey }" in source
    for path in (VERIFY, BUILD):
        script = path.read_text(encoding="utf-8")
        assert "$env:GITHUB_ACTIONS -ne \"true\"" in script
        assert "$env:GITHUB_REF -notmatch '^refs/(heads|pull)/'" in script
        assert '"production", "ci-test"' in script
        assert "CI-TEST-ONLY.txt" in script
    assert "$checksumArtifacts += $testMarkerPath" in BUILD.read_text(encoding="utf-8")
    assert '$isTestSigning -ne (Test-Path -LiteralPath $testMarkerPath -PathType Leaf)' in source
    assert 'if ($LASTEXITCODE -ne 0) { throw "Updater signature verification failed." }' in source


@pytest.mark.parametrize(
    "ref,key,password,expected_mode,should_generate,success",
    [
        ("refs/pull/12/merge", "", "", "ci-test", True, True),
        ("refs/heads/main", "", "", "ci-test", True, True),
        ("refs/tags/v0.3.4", "", "", None, False, False),
        ("refs/tags/other", "", "", None, False, False),
        ("refs/tags/v0.3.4", "configured-private-key", "configured-password", "production", False, True),
        ("refs/tags/v0.3.4", "configured-private-key", "", None, False, False),
    ],
)
def test_configure_signing_fork_and_release_guards(
    tmp_path, ref, key, password, expected_mode, should_generate, success,
):
    generated = tmp_path / "generated"
    github_env = tmp_path / "github-env"
    # Fake signer tests routing/log hygiene without creating real credentials.
    fake_signer = r'''
function npx {
    $keyPath = $args[[Array]::IndexOf($args, "--write-keys") + 1]
    [System.IO.File]::WriteAllText($keyPath, "fake-private-key`nsecond-private-line")
    [System.IO.File]::WriteAllText("$keyPath.pub", $env:FAKE_PUBLIC_KEY)
    [System.IO.File]::WriteAllText($env:GENERATED_MARKER, "called")
    Write-Output "SIGNER OUTPUT MUST NEVER APPEAR"
    $global:LASTEXITCODE = 0
}
'''
    result = run_powershell(tmp_path, fake_signer + signing_step(), {
        "GITHUB_ACTIONS": "true", "GITHUB_REF": ref,
        "RELEASE_SIGNING_KEY": key, "RELEASE_SIGNING_KEY_PASSWORD": password,
        "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(github_env),
        "GENERATED_MARKER": str(generated), "FAKE_PUBLIC_KEY": TEST_KEY,
    }, pwsh_only=True)
    assert (result.returncode == 0) == success, result.stderr
    assert generated.exists() == should_generate
    assert "SIGNER OUTPUT MUST NEVER APPEAR" not in result.stdout + result.stderr
    assert not (tmp_path / "cad-studio-ci-updater.key").exists()
    assert not (tmp_path / "cad-studio-ci-updater.key.pub").exists()
    if success:
        exported = github_env.read_text(encoding="utf-8-sig")
        assert f"CAD_STUDIO_UPDATER_SIGNING_MODE={expected_mode}" in exported
        expected_key = TEST_KEY if should_generate else ""
        assert f"CAD_STUDIO_TEST_UPDATER_PUBLIC_KEY={expected_key}\n" in exported
        masks = [line.removeprefix("::add-mask::") for line in result.stdout.splitlines() if line.startswith("::add-mask::")]
        if should_generate:
            assert "fake-private-key" in masks and "second-private-line" in masks
            generated_password = exported.split("TAURI_SIGNING_PRIVATE_KEY_PASSWORD=", 1)[1].strip()
            assert generated_password in masks
    else:
        assert not github_env.exists()


def make_verification_fixture(tmp_path, *, marker):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(VERIFY, scripts / VERIFY.name)
    config = tmp_path / "apps/workbench-ui/src-tauri/tauri.conf.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": "0.3.4", "plugins": {"updater": {"pubkey": PRODUCTION_KEY}}}), encoding="utf-8")
    release = tmp_path / "release-output"
    release.mkdir()
    setup = "CAD-Studio-0.3.4-Setup-x64.exe"
    signature = base64.b64encode(b"fixture-signature").decode()
    (release / setup).write_bytes(b"fixture-installer")
    (release / f"{setup}.sig").write_text(signature, encoding="utf-8")
    latest = {"version": "0.3.4", "platforms": {"windows-x86_64": {
        "signature": signature,
        "url": f"https://github.com/wzyn20051216/solidworks-automation-skill/releases/download/v0.3.4/{setup}",
    }}}
    (release / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
    if marker:
        (release / "CI-TEST-ONLY.txt").write_text("CI-only test fixture", encoding="utf-8")
    checksums = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in release.iterdir()]
    (release / "SHA256SUMS.txt").write_text("\n".join(checksums), encoding="ascii")
    return scripts / VERIFY.name


@pytest.mark.parametrize(
    "actions,ref,mode,key,marker,success,expected_key",
    [
        ("true", "refs/pull/12/merge", "ci-test", TEST_KEY, True, True, b"temporary-ci-public-key"),
        ("true", "refs/heads/main", "ci-test", TEST_KEY, True, True, b"temporary-ci-public-key"),
        ("true", "refs/tags/v0.3.4", "production", "", False, True, b"production-public-key"),
        ("", "", "", "", False, True, b"production-public-key"),
        ("true", "refs/tags/v0.3.4", "ci-test", TEST_KEY, True, False, None),
        ("true", "refs/tags/other", "ci-test", TEST_KEY, True, False, None),
        ("false", "refs/heads/main", "ci-test", TEST_KEY, True, False, None),
        ("true", "", "ci-test", TEST_KEY, True, False, None),
        ("true", "refs/heads/main", "production", TEST_KEY, True, False, None),
        ("true", "refs/heads/main", "ci-test", "", True, False, None),
        ("true", "refs/heads/main", "ci-test", TEST_KEY, False, False, None),
        ("true", "refs/tags/v0.3.4", "production", "", True, False, None),
        ("true", "refs/heads/main", "unknown", "", False, False, None),
    ],
)
def test_verifier_selects_test_key_only_for_explicit_nonrelease_ci(
    tmp_path, actions, ref, mode, key, marker, success, expected_key,
):
    script = make_verification_fixture(tmp_path, marker=marker)
    captured_key = tmp_path / "selected-key"
    # The existing Rust example owns cryptographic verification. This stub only
    # observes the selected key and checks that rejected modes never invoke it.
    harness = r'''
function cargo {
    [System.IO.File]::Copy($args[-3], $env:CAPTURED_KEY)
    $global:LASTEXITCODE = 0
}
& $env:CONTRACT_SCRIPT
'''
    result = run_powershell(tmp_path, harness, {
        "GITHUB_ACTIONS": actions, "GITHUB_REF": ref,
        "CAD_STUDIO_UPDATER_SIGNING_MODE": mode,
        "CAD_STUDIO_TEST_UPDATER_PUBLIC_KEY": key,
        "CONTRACT_SCRIPT": str(script), "CAPTURED_KEY": str(captured_key),
        "TEMP": str(tmp_path),
    })
    assert (result.returncode == 0) == success, result.stderr
    if success:
        assert captured_key.read_bytes() == expected_key
        if mode == "ci-test":
            assert "NOT production-trusted" in result.stdout
    else:
        assert not captured_key.exists()
    assert not list(tmp_path.glob("cad-studio-updater-public-*.key"))
    assert not list(tmp_path.glob("cad-studio-updater-signature-*.sig"))


@pytest.mark.parametrize("failure", ["signature", "checksum"])
def test_verifier_rejects_signature_tool_failure_and_tampered_artifacts(tmp_path, failure):
    script = make_verification_fixture(tmp_path, marker=False)
    if failure == "checksum":
        (tmp_path / "release-output/CAD-Studio-0.3.4-Setup-x64.exe").write_bytes(b"tampered-installer")
    harness = r'''
function cargo {
    $global:LASTEXITCODE = [int]$env:SIGNATURE_EXIT_CODE
}
& $env:CONTRACT_SCRIPT
'''
    result = run_powershell(tmp_path, harness, {
        "CONTRACT_SCRIPT": str(script), "TEMP": str(tmp_path),
        "SIGNATURE_EXIT_CODE": "1" if failure == "signature" else "0",
    })
    assert result.returncode != 0
    expected = "Updater signature verification failed" if failure == "signature" else "SHA256SUMS.txt does not cover"
    assert expected in result.stderr
    assert not list(tmp_path.glob("cad-studio-updater-public-*.key"))
    assert not list(tmp_path.glob("cad-studio-updater-signature-*.sig"))


def test_instrumented_build_is_explicit_ci_only_and_preserves_production_config():
    script = BUILD.read_text(encoding="utf-8")
    assert 'if ($CiE2e -and (-not $isTestSigning -or $SkipBuild))' in script
    assert script.index('if ($CiE2e -and') < script.index('& python $syncScript')
    assert '--remote-debugging-address=127.0.0.1 --remote-debugging-port=9227' in script
    assert 'Join-Path $env:RUNNER_TEMP "cad-studio-ci-e2e.tauri.json"' in script
    assert 'npm run desktop:bundle -- --config $overlayPath' in script
    assert 'CI-E2E-INSTRUMENTED.txt' in script
    assert '$checksumArtifacts += $instrumentedMarker' in script
    assert 'Set-Content -LiteralPath (Join-Path $tauriRoot "tauri.conf.json")' not in script
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert workflow.index('Smoke-test production-config portable startup') < workflow.index('Build isolated CI E2E package')
    assert 'path: output/ci/portable-e2e-*.json' in workflow


@pytest.mark.parametrize('mode,ref,actions,skip_build', [
    ('production', 'refs/tags/v0.3.4', 'true', False),
    ('production', 'refs/heads/main', 'true', False),
    ('ci-test', 'refs/tags/v0.3.4', 'true', False),
    ('ci-test', 'refs/heads/main', 'false', False),
    ('ci-test', 'refs/heads/main', 'true', True),
])
def test_instrumented_build_rejects_release_local_and_skip_build(tmp_path, mode, ref, actions, skip_build):
    result = run_powershell(tmp_path, '& $env:BUILD_SCRIPT -CiE2e' + (' -SkipBuild' if skip_build else ''), {
        'BUILD_SCRIPT': str(BUILD), 'GITHUB_ACTIONS': actions, 'GITHUB_REF': ref,
        'CAD_STUDIO_UPDATER_SIGNING_MODE': mode, 'CAD_STUDIO_TEST_UPDATER_PUBLIC_KEY': TEST_KEY if mode == 'ci-test' else '',
    })
    assert result.returncode != 0
    assert 'CI test' in result.stderr or 'non-release CI test build' in result.stderr
