"""@brief Provider 无关的交付复核、能力门禁和大型文件格式回归。"""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from apps.desktop.cad_workbench import agent_contracts, queue_worker
from apps.desktop.cad_workbench.artifact_ledger import build_artifact_ledger
from apps.desktop.cad_workbench.reviewer_gate import evaluate_ledger, validate_known_format


PROVIDERS = ["codex", "claude", "gemini", "opencode"]
EXECUTION_PATHS = [
    {"executor": "codex", "kind": "codex_task"},
    {"executor": "agent", "kind": "agent_task"},
    {"executor": "agent", "kind": "create_shell"},
    {"kind": "codex_task"},
    {"kind": "agent_task"},
]


def _artifact(path: Path, *, fresh: bool = True, kind: str | None = None) -> dict:
    return {
        "path": str(path), "kind": kind or path.suffix.lstrip("."),
        "exists": True, "isDirectory": False, "sizeBytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "producedThisRun": fresh,
    }


def _job(tmp_path: Path, **overrides: object) -> dict:
    return {
        "schemaVersion": "2.0", "id": "reliability", "kind": "create_shell", "status": "queued",
        "progress": 0, "cwd": str(tmp_path), "detail": "交付复核", "capabilities": ["part_and_features"],
        **overrides,
    }


@pytest.mark.parametrize("execution", EXECUTION_PATHS)
@pytest.mark.parametrize("fresh", [False, None])
def test_all_agent_paths_reject_stale_or_unproven_deliverables(tmp_path: Path, execution: dict, fresh: bool | None) -> None:
    model = tmp_path / "model.step"
    model.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    artifact = _artifact(model)
    artifact["producedThisRun"] = fresh
    review = evaluate_ledger({
        **execution, "expectedOutput": "STEP", "artifacts": [artifact],
        "verification": [{"command": "check", "status": "passed"}],
    })
    assert review["status"] == "fail"
    assert any(item["id"] == "expected-cad-deliverable-step" and item["status"] == "fail" for item in review["checks"])


@pytest.mark.parametrize("execution", EXECUTION_PATHS)
@pytest.mark.parametrize("verification", [[], None, "passed", ["passed"]])
def test_all_agent_paths_require_real_verification_records(tmp_path: Path, execution: dict, verification: object) -> None:
    model = tmp_path / "model.step"
    model.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    review = evaluate_ledger({**execution, "artifacts": [_artifact(model)], "verification": verification})
    assert review["status"] in {"warning", "fail"}
    assert any(item["id"].startswith("executor-verification") and item["status"] != "pass" for item in review["checks"])


@pytest.mark.parametrize("execution", EXECUTION_PATHS)
def test_local_cad_fallback_requires_current_files_for_every_agent_path(tmp_path: Path, execution: dict) -> None:
    model = tmp_path / "model.step"
    model.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    review = evaluate_ledger({
        **execution, "localCadAutomation": True, "artifacts": [_artifact(model, fresh=False)],
        "verification": [{"command": "check", "status": "passed"}],
    })
    assert review["status"] == "warning"
    assert any(item["id"] == "cad-deliverable-not-declared" for item in review["checks"])


@pytest.mark.parametrize("receipt_kind", ["codex_output", "agent_output"])
def test_agent_receipts_cannot_satisfy_expected_deliverables(tmp_path: Path, receipt_kind: str) -> None:
    receipt = tmp_path / "receipt.step"
    receipt.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    review = evaluate_ledger({
        "executor": "agent", "expectedOutput": "STEP", "artifacts": [_artifact(receipt, kind=receipt_kind)],
        "verification": [{"command": "check", "status": "passed"}],
    })
    assert review["status"] == "fail"


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("new_output", [False, True])
def test_provider_runtime_freshness_flows_into_shared_reviewer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, new_output: bool) -> None:
    """@brief 在真实 Provider 归一化与账本路径上验证新旧文件，不调用外部 CLI。"""
    model = tmp_path / "model.step"
    if not new_output:
        model.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(agent_contracts, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(queue_worker, "agent_output_path", lambda job, cwd: receipt)
    monkeypatch.setattr(queue_worker, "codex_output_path", lambda job, cwd: receipt)
    job = _job(
        tmp_path, kind="agent_task", executor="agent", expectedOutput="STEP", prompt="检查交付",
        uiConfig={"agentRuntime": {"provider": provider}, "engineeringOrchestration": {"mode": "off"}},
    )

    def runner(command: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
        if new_output:
            model.write_text("ISO-10303-21;\nEND-ISO-10303-21;\n")
        payload = {
            "summary": "完成", "changedFiles": [str(model)],
            "verification": [{"command": "check", "status": "passed", "note": "ok"}],
            "risks": [], "nextSteps": [],
        }
        if provider == "codex":
            receipt.write_text(json.dumps(payload))
            stdout = ""
        elif provider == "claude":
            stdout = json.dumps({"structured_output": payload})
        elif provider == "gemini":
            stdout = json.dumps({"response": json.dumps(payload)})
        else:
            stdout = json.dumps({"type": "text", "part": {"text": json.dumps(payload)}})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    result = queue_worker.run_agent_job(job, runner=runner)
    review = evaluate_ledger(build_artifact_ledger(tmp_path, job, result))
    assert review["status"] == ("pass" if new_output else "fail")


@pytest.fixture(params=["step", "stl", "dxf"])
def large_text_cad(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """@brief 创建结束标记超出旧 1 MiB 首部样本的格式夹具。"""
    kind = request.param
    if kind == "step":
        data = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n/*" + b" model comment " * 170000 + b"*/\nENDSEC;\nEND-ISO-10303-21;\n"
    elif kind == "stl":
        triangle = b"facet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\n"
        data = b"solid large\n" + triangle * 25000 + b"endsolid large\n"
    else:
        data = b"0\nSECTION\n2\nENTITIES\n" + b"0\nPOINT\n8\n0\n10\n0.0\n20\n0.0\n30\n0.0\n" * 65000 + b"0\nENDSEC\n0\nEOF\n"
    path = tmp_path / f"large.{kind}"
    path.write_bytes(data)
    assert path.stat().st_size > 2 * 1024 * 1024
    return path


def test_large_text_formats_validate_using_actual_file_tail(large_text_cad: Path) -> None:
    assert validate_known_format(large_text_cad.suffix, large_text_cad)["status"] == "pass"


def test_large_text_formats_reject_trailing_corruption(large_text_cad: Path) -> None:
    with large_text_cad.open("ab") as handle:
        handle.write(b"\nNOT A VALID TRAILER")
    assert validate_known_format(large_text_cad.suffix, large_text_cad)["status"] == "fail"


def test_format_validation_uses_bounded_reads(large_text_cad: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = Path.open
    requests: list[int] = []

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def read(self, size=-1):
            requests.append(size)
            assert 0 <= size <= 1024 * 1024
            return self.handle.read(size)

        def seek(self, *args):
            return self.handle.seek(*args)

    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs: Reader(original_open(path, *args, **kwargs)))
    assert validate_known_format("cad", large_text_cad)["status"] == "pass"
    assert len(requests) == 2


@pytest.mark.parametrize("header", [b"binary mesh", b"solid binary mesh"])
@pytest.mark.parametrize("count", [1, 24000])
def test_binary_stl_validates_count_and_allows_solid_header(tmp_path: Path, header: bytes, count: int) -> None:
    path = tmp_path / "model.stl"
    triangle = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0)
    path.write_bytes(header.ljust(80, b" ") + struct.pack("<I", count) + triangle * count)
    assert validate_known_format("stl", path)["status"] == "pass"


@pytest.mark.parametrize("data", [
    b"random bytes" * 20,
    b"\x00" * 84,
    b"x" * 80 + struct.pack("<I", 2) + b"\x00" * 50,
    b"solid binary".ljust(80, b" ") + struct.pack("<I", 1) + b"\x00" * 49,
    b"x" * 80 + struct.pack("<I", 1) + b"\x00" * 51,
    b"x" * 80 + struct.pack("<I", 2**32 - 1),
    b"solidity\nendsolid\n",
])
def test_malformed_binary_or_ascii_stl_is_rejected(tmp_path: Path, data: bytes) -> None:
    path = tmp_path / "invalid.stl"
    path.write_bytes(data)
    assert validate_known_format("stl", path)["status"] == "fail"


@pytest.mark.parametrize("extension,data", [("step", b"END-ISO-10303-21;"), ("dxf", b"SECTION\nEOF"), ("stl", b"solid a\n")])
def test_partial_or_incidental_format_markers_are_rejected(tmp_path: Path, extension: str, data: bytes) -> None:
    path = tmp_path / f"invalid.{extension}"
    path.write_bytes(data)
    assert validate_known_format(extension, path)["status"] == "fail"


def test_missing_file_is_a_review_failure(tmp_path: Path) -> None:
    assert validate_known_format("step", tmp_path / "missing.step")["status"] == "fail"


@pytest.mark.parametrize("schema", ["0.9", "3.0", "", None, 2, {}])
def test_unsupported_schema_cannot_bypass_capability_gate(tmp_path: Path, schema: object) -> None:
    job = _job(tmp_path, schemaVersion=schema)
    assert queue_worker._capability_block_reasons(job)


@pytest.mark.parametrize("capabilities", [None, "part_and_features", {}, [None], [1], [""], ["  "]])
def test_invalid_capability_declaration_is_blocked(tmp_path: Path, capabilities: object) -> None:
    assert queue_worker._capability_block_reasons(_job(tmp_path, capabilities=capabilities))


@pytest.mark.parametrize("schema", [None, "1.0", "2.0"])
def test_legacy_schema_does_not_bypass_declared_capability_restrictions(tmp_path: Path, schema: str | None) -> None:
    job = _job(tmp_path, capabilities=["unknown-capability"])
    if schema is None:
        job.pop("schemaVersion")
    else:
        job["schemaVersion"] = schema
    assert any("unknown-capability" in reason for reason in queue_worker._capability_block_reasons(job))


@pytest.mark.parametrize("schema", [None, "1.0", "2.0"])
@pytest.mark.parametrize("capabilities", [None, [], ["git_push"]])
def test_missing_cad_declarations_preserve_execution_but_never_auto_pass(tmp_path: Path, schema: str | None, capabilities: list | None) -> None:
    job = _job(tmp_path)
    if schema is None:
        job.pop("schemaVersion")
    else:
        job["schemaVersion"] = schema
    if capabilities is None:
        job.pop("capabilities")
    else:
        job["capabilities"] = capabilities
    path = tmp_path / "queue" / "compatibility.json"
    queue_worker.write_job(path, job)
    calls: list[str] = []

    def handler(active_job: dict) -> dict:
        calls.append(active_job["id"])
        output = tmp_path / "report.txt"
        output.write_text("review draft")
        return {"outputs": {"report": str(output)}}

    result = queue_worker.process_job(path, handlers={"create_shell": handler})
    assert calls == ["reliability"]
    assert result["status"] == "review_required"
    saved_review = json.loads(Path(result["reviewGatePath"]).read_text())
    assert any(item["id"] == "capability-declaration-review" and item["status"] == "warning" for item in saved_review["checks"])


@pytest.fixture
def capabilities_module():
    scripts = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("capabilities")


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), ValueError("malformed"), ImportError("unavailable")])
def test_manifest_load_failure_blocks_before_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capabilities_module, error: Exception) -> None:
    def fail():
        raise error

    monkeypatch.setattr(capabilities_module, "load_capabilities", fail)
    path = tmp_path / "queue" / "blocked.json"
    queue_worker.write_job(path, _job(tmp_path))
    calls: list[dict] = []
    result = queue_worker.process_job(path, handlers={"create_shell": lambda job: calls.append(job) or {}})
    assert result["status"] == "blocked"
    assert not calls
    assert any("能力清单" in reason for reason in result["blockedReasons"])


@pytest.mark.parametrize("level,policy,blocked", [
    ("verified", {}, False),
    ("pilot", {}, True),
    ("pilot", {"requireReviewerPass": True}, False),
    ("reference_only", {}, True),
    ("reference_only", {"approval": "manual-required"}, False),
    ("not_implemented", {"approval": "manual-required", "requireReviewerPass": True}, True),
])
def test_supported_capability_policy_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capabilities_module, level: str, policy: dict, blocked: bool) -> None:
    monkeypatch.setattr(capabilities_module, "load_capabilities", lambda: {"capabilities": [{"id": "fixture", "level": level}]})
    job = _job(tmp_path, capabilities=["fixture", "cad_macro", "full_access"], policy=policy)
    assert bool(queue_worker._capability_block_reasons(job)) is blocked


@pytest.mark.parametrize("policy", [None, [], "manual-required"])
def test_invalid_policy_cannot_bypass_capability_gate(tmp_path: Path, policy: object) -> None:
    assert queue_worker._capability_block_reasons(_job(tmp_path, policy=policy))


def test_empty_capability_manifest_blocks_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capabilities_module) -> None:
    monkeypatch.setattr(capabilities_module, "load_capabilities", lambda: {"capabilities": []})
    assert any("能力清单" in reason for reason in queue_worker._capability_block_reasons(_job(tmp_path)))


def test_directory_named_as_cad_file_cannot_satisfy_delivery(tmp_path: Path) -> None:
    path = tmp_path / "fake.step"
    path.mkdir()
    review = evaluate_ledger({
        "executor": "agent", "expectedOutput": "STEP",
        "verification": [{"command": "check", "status": "passed"}],
        "artifacts": [{"kind": "step", "path": str(path), "exists": True, "isDirectory": True, "producedThisRun": True}],
    })
    assert review["status"] == "fail"


def test_handler_cannot_erase_missing_capability_review(tmp_path: Path) -> None:
    job = _job(tmp_path, capabilities=[])
    path = tmp_path / "queue" / "compatibility.json"
    queue_worker.write_job(path, job)

    def handler(active_job: dict) -> dict:
        active_job["capabilities"] = ["part_and_features"]
        output = tmp_path / "report.txt"
        output.write_text("review draft")
        return {"outputs": {"report": str(output)}}

    result = queue_worker.process_job(path, handlers={"create_shell": handler})
    assert result["status"] == "review_required"
