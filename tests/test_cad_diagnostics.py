"""cad_diagnostics 脱敏诊断包回归测试。

run_doctor 与文件写入全部打桩或写入 tmp_path，测试完全离线确定。
"""
from __future__ import annotations

import json
import runpy
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cad_diagnostics  # noqa: E402


def test_redact_masks_sensitive_keys_case_insensitively():
    """@brief 敏感键（含大小写和分隔符变体）一律替换为 [redacted]。"""
    payload = {
        "prompt": "secret",
        "apiKey": "abc",
        "API-KEY": "abc2",
        "apikey": "abc3",
        "token": "t",
        "access_token": "t2",
        "secret": "s",
        "password": "p",
        "credentials": "c",
        "content": "x",
        "cwd": r"C:\Users\Alice",
        "projectPath": r"D:\work\proj",
        "sourcePath": r"D:\work\file.sldprt",
        "endpoint": "https://api.example.com",
        "message": "保留",
    }
    redacted = cad_diagnostics._redact(payload)
    assert redacted["message"] == "保留"
    sensitive = {key: value for key, value in redacted.items() if key != "message"}
    assert set(sensitive.values()) == {"[redacted]"}


def test_redact_handles_nested_containers_and_scalars():
    """@brief 字典递归、列表截断到 100 项、非字符串值原样保留、数字键转字符串。"""
    payload = {
        1: {"nested": [{"cwd": "leak"}, "plain", 7, None]},
        "items": list(range(150)),
        "long": "x" * 1500,
    }
    redacted = cad_diagnostics._redact(payload)

    assert redacted["1"]["nested"][0] == {"cwd": "[redacted]"}
    assert redacted["1"]["nested"][1] == "plain"
    assert redacted["1"]["nested"][2] == 7
    assert redacted["1"]["nested"][3] is None
    assert len(redacted["items"]) == 100
    assert redacted["items"][0] == 0
    assert len(redacted["long"]) == 1000


def test_redact_collapses_absolute_paths_to_file_names():
    """@brief 绝对路径只保留文件名；根路径没有文件名时用 [path] 占位。"""
    assert cad_diagnostics._redact(r"C:\Users\Alice\report.txt") == "report.txt"
    assert cad_diagnostics._redact(r"C:\Windows") == "Windows"
    assert cad_diagnostics._redact("C:\\") == "[path]"
    assert cad_diagnostics._redact("/home/alice/notes.md") == "notes.md"
    assert cad_diagnostics._redact("/") == "[path]"
    assert cad_diagnostics._redact(r"\\server\share\dir\data.json") == "data.json"


def test_create_diagnostic_bundle_redacts_doctor_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cad_diagnostics,
        "run_doctor",
        lambda *, probe_cad: {
            "checks": [{"id": "demo", "message": "ok"}],
            "python": {"executable": "python.exe"},
            "cwd": r"C:\Users\Alice\project",
        },
    )

    output = cad_diagnostics.create_diagnostic_bundle(
        tmp_path / "diag" / "report.zip", events={"prompt": "secret", "kinds": ["a", "b"]}
    )

    assert output == tmp_path / "diag" / "report.zip"
    assert (tmp_path / "diag").is_dir()
    with zipfile.ZipFile(output) as archive:
        payload = json.loads(archive.read("diagnostic.json"))
    assert payload["schemaVersion"] == "1.0"
    assert "T" in payload["createdAt"]
    assert payload["doctor"]["cwd"] == "[redacted]"
    assert payload["doctor"]["checks"] == [{"id": "demo", "message": "ok"}]
    assert payload["events"]["prompt"] == "[redacted]"
    assert payload["events"]["kinds"] == ["a", "b"]
    assert payload["privacy"] == {
        "telemetry": "off",
        "redacted": ["prompt", "API key", "token", "完整私人路径"],
    }


def test_create_diagnostic_bundle_normalizes_suffix_and_default_events(monkeypatch, tmp_path):
    """@brief 非 .zip 后缀统一改写为 .zip；未提供 events 时序列化为空列表。"""
    monkeypatch.setattr(cad_diagnostics, "run_doctor", lambda *, probe_cad: {})

    output = cad_diagnostics.create_diagnostic_bundle(tmp_path / "report.txt", events=None)
    assert output == tmp_path / "report.zip"

    output2 = cad_diagnostics.create_diagnostic_bundle(tmp_path / "no-suffix")
    assert output2 == tmp_path / "no-suffix.zip"

    with zipfile.ZipFile(output2) as archive:
        payload = json.loads(archive.read("diagnostic.json"))
    assert payload["events"] == []


def test_main_parses_events_and_outputs_json(monkeypatch, tmp_path, capsys):
    """@brief CLI 要解析 --events、支持缺失的 events 文件并使用默认输出路径。"""
    recorded = {}

    def fake_create(output, *, events):
        recorded["output"] = Path(output)
        recorded["events"] = events
        return tmp_path / "made.zip"

    monkeypatch.setattr(cad_diagnostics, "create_diagnostic_bundle", fake_create)
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps({"token": "t"}), encoding="utf-8")

    assert cad_diagnostics.main(["--output", str(tmp_path / "out.zip"), "--events", str(events_file)]) == 0
    assert recorded["events"] == {"token": "t"}
    assert json.loads(capsys.readouterr().out) == {"path": "made.zip", "status": "created"}

    missing = tmp_path / "missing.json"
    assert cad_diagnostics.main(["--events", str(missing)]) == 0
    assert recorded["events"] is None
    capsys.readouterr()

    monkeypatch.chdir(tmp_path)
    assert cad_diagnostics.main([]) == 0
    assert recorded["output"] == tmp_path / "cad-studio-diagnostics.zip"


def test_entrypoint_help(monkeypatch):
    """@brief 以 __main__ 方式执行脚本时 --help 直接退出。"""
    monkeypatch.setattr(sys, "argv", ["cad_diagnostics.py", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "cad_diagnostics.py"), run_name="__main__")
    assert excinfo.value.code == 0
