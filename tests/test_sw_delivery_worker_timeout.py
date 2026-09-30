"""Pack and Go 隔离 worker 的有界等待和所有权回归测试。"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import sw_delivery


WORKER_OPTIONS = {
    "include_drawings": True,
    "include_simulation_results": False,
    "include_toolbox_components": True,
    "include_suppressed": False,
    "flatten": False,
}


class FakeWorker:
    """只记录本次 Python 子进程的 wait/kill，模拟超时与回收失败。"""

    pid = 12345

    def __init__(self, *, timeout=False, cleanup_timeout=False, kill_error=False, returncode=0):
        self.timeout = timeout
        self.cleanup_timeout = cleanup_timeout
        self.kill_error = kill_error
        self.returncode = returncode
        self.waits = []
        self.kills = 0

    def wait(self, *, timeout):
        self.waits.append(timeout)
        if (len(self.waits) == 1 and self.timeout) or (len(self.waits) > 1 and self.cleanup_timeout):
            raise subprocess.TimeoutExpired("owned-pack-worker", timeout)
        return self.returncode

    def kill(self):
        self.kills += 1
        if self.kill_error:
            raise OSError("worker termination denied")


def install_worker(monkeypatch, worker, *, stdout=b"", stderr=b""):
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs, json.load(kwargs["stdin"])))
        kwargs["stdout"].write(stdout)
        kwargs["stderr"].write(stderr)
        return worker

    monkeypatch.delenv("CADSTUDIO_PACK_WORKER", raising=False)
    monkeypatch.setattr(sw_delivery.subprocess, "Popen", popen)
    return calls


def test_worker_success_preserves_json_protocol_and_closes_streams(tmp_path, monkeypatch):
    expected = {"backend": "comtypes", "status_codes": [0], "outputs": [{"path": "零件.sldprt"}]}
    worker = FakeWorker()
    calls = install_worker(
        monkeypatch, worker,
        stdout=("worker log\n" + json.dumps({"result": expected}, ensure_ascii=False) + "\n").encode("utf-8"),
    )
    source = str(tmp_path / "装配.sldasm")
    existing = {str(tmp_path / "old.sldprt"): (8, 123), str(tmp_path / "missing"): None}

    result = sw_delivery._comtypes_pack_and_go(
        source, tmp_path / "output", existing, dependencies=["part.sldprt"], **WORKER_OPTIONS,
    )

    assert result == expected
    assert worker.waits == [sw_delivery.PACK_WORKER_TIMEOUT_SECONDS]
    assert worker.kills == 0
    assert len(calls) == 1
    command, kwargs, payload = calls[0]
    assert command == [sys.executable, str(Path(sw_delivery.__file__).with_name("sw_delivery_comtypes_worker.py"))]
    assert kwargs["close_fds"] is True
    assert kwargs["cwd"] == str(Path(sw_delivery.__file__).resolve().parents[1])
    assert all(kwargs[name].closed for name in ("stdin", "stdout", "stderr"))
    assert payload == {
        "source_path": source,
        "target": str(tmp_path / "output"),
        "existing_files": {key: list(value) if value is not None else None for key, value in existing.items()},
        "dependencies": ["part.sldprt"],
        **WORKER_OPTIONS,
    }


def test_worker_timeout_kills_and_reaps_only_owned_child(tmp_path, monkeypatch):
    worker = FakeWorker(timeout=True)
    calls = install_worker(monkeypatch, worker)

    with pytest.raises(sw_delivery.PackAndGoWorkerTimeout) as raised:
        sw_delivery._comtypes_pack_and_go("assembly.sldasm", tmp_path, {}, **WORKER_OPTIONS)

    error = raised.value
    assert error.code == "SW_PACK_AND_GO_TIMEOUT"
    assert error.stage == "save"
    assert error.timeout_seconds == sw_delivery.PACK_WORKER_TIMEOUT_SECONDS
    assert error.worker_cleanup == {"pid": worker.pid, "terminated": True, "errors": []}
    assert worker.waits == [sw_delivery.PACK_WORKER_TIMEOUT_SECONDS, sw_delivery.PACK_WORKER_CLEANUP_TIMEOUT_SECONDS]
    assert worker.kills == 1
    assert len(calls) == 1
    assert all(calls[0][1][name].closed for name in ("stdin", "stdout", "stderr"))


def test_worker_cleanup_failure_remains_bounded_and_keeps_timeout_error(tmp_path, monkeypatch):
    worker = FakeWorker(timeout=True, cleanup_timeout=True, kill_error=True)
    calls = install_worker(monkeypatch, worker)

    with pytest.raises(sw_delivery.PackAndGoWorkerTimeout) as raised:
        sw_delivery._comtypes_pack_and_go("assembly.sldasm", tmp_path, {}, **WORKER_OPTIONS)

    assert raised.value.worker_cleanup["terminated"] is False
    assert len(raised.value.worker_cleanup["errors"]) == 2
    assert worker.waits == [sw_delivery.PACK_WORKER_TIMEOUT_SECONDS, sw_delivery.PACK_WORKER_CLEANUP_TIMEOUT_SECONDS]
    assert worker.kills == 1
    assert len(calls) == 1


@pytest.mark.parametrize("fallback_policy", ["stage_dependencies", "blocked"])
def test_public_timeout_is_failure_without_staging_or_further_com(tmp_path, monkeypatch, fallback_policy):
    source = tmp_path / "assembly.sldasm"
    source.write_bytes(b"assembly")
    dependency = tmp_path / "part.sldprt"
    dependency.write_bytes(b"part")
    target = tmp_path / "output"
    worker = FakeWorker(timeout=True)
    install_worker(monkeypatch, worker)

    class Model:
        def GetPathName(self):
            assert not worker.waits, "超时后禁止再次调用共享 COM 会话"
            return str(source)

    def native_failure(*_args, **_kwargs):
        (target / "partial.sldprt").write_bytes(b"incomplete")
        raise RuntimeError("pywin32 failed")

    def unexpected_stage(*_args, **_kwargs):
        pytest.fail("超时后禁止暂存依赖或宣称交付成功")

    monkeypatch.setattr(sw_delivery, "_document_dependency_paths", lambda *_args: [str(dependency)])
    monkeypatch.setattr(sw_delivery, "_collect_component_audit_records", lambda *_args: [])
    monkeypatch.setattr(sw_delivery, "_associated_drawing_paths", lambda *_args: [])
    monkeypatch.setattr(sw_delivery, "_model_doc_extension", lambda *_args: object())
    monkeypatch.setattr(sw_delivery, "_pywin32_pack_and_go", native_failure)
    monkeypatch.setattr(sw_delivery, "_stage_pack_and_go_dependencies", unexpected_stage)

    report = sw_delivery.pack_and_go(Model(), target, fallback_policy=fallback_policy)

    assert report["success"] is False
    assert report["status"] == "failed"
    assert report["stage"] == "save"
    assert report["error_code"] == "SW_PACK_AND_GO_TIMEOUT"
    assert report["timeout_seconds"] == sw_delivery.PACK_WORKER_TIMEOUT_SECONDS
    assert report["worker_cleanup"]["terminated"] is True
    assert report["retryable"] is False
    assert report["manual_review_required"] is True
    assert report["fallback_used"] is False
    assert report["outputs"] == []
    assert report["produced_count"] == 0
    assert report["manifest"] is None
    assert "SW_PACK_AND_GO_TIMEOUT" in report["fallback_errors"][-1]
    assert (target / "partial.sldprt").read_bytes() == b"incomplete"
    json.dumps(report)


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (1, b"", b"worker failed", "worker failed"),
        (0, b"not json", b"", "不可解析"),
        (0, b'{"error": "COM unavailable"}', b"", "COM unavailable"),
    ],
)
def test_worker_failure_diagnostics_unchanged(tmp_path, monkeypatch, returncode, stdout, stderr, message):
    worker = FakeWorker(returncode=returncode)
    install_worker(monkeypatch, worker, stdout=stdout, stderr=stderr)

    with pytest.raises(RuntimeError, match=message):
        sw_delivery._comtypes_pack_and_go("assembly.sldasm", tmp_path, {}, **WORKER_OPTIONS)

    assert worker.kills == 0


def test_hung_python_child_is_reaped_without_touching_other_processes(tmp_path, monkeypatch):
    """真实 Python 子进程补充验证，不依赖 Windows 或 SolidWorks。"""
    original_popen = subprocess.Popen
    children = []

    def popen(_command, **kwargs):
        child = original_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.delenv("CADSTUDIO_PACK_WORKER", raising=False)
    monkeypatch.setattr(sw_delivery.subprocess, "Popen", popen)
    monkeypatch.setattr(sw_delivery, "PACK_WORKER_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        with pytest.raises(sw_delivery.PackAndGoWorkerTimeout) as raised:
            sw_delivery._comtypes_pack_and_go("assembly.sldasm", tmp_path, {}, **WORKER_OPTIONS)
        assert time.monotonic() - started < 10
        assert raised.value.worker_cleanup["terminated"] is True
        assert len(children) == 1
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
