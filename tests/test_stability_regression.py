"""@brief CAD Studio 连接生命周期稳定性模拟回归的离线测试。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from scripts import stability_regression
from scripts.stability_regression import FakeCadProcess, LifecycleHarness, run_regression


def test_twenty_lifecycle_iterations_have_no_orphans():
    result = run_regression(20)
    assert result["status"] == "pass"
    assert result["iterations"] == 20
    assert result["unique_processes"] == 20
    assert result["orphan_process"] is False


def test_run_regression_defaults_to_twenty_clean_iterations():
    result = run_regression()
    assert result["status"] == "pass"
    assert result["iterations"] == 20
    assert result["unique_processes"] == 20
    assert result["orphan_process"] is False
    assert result["cancelled"] is False


@pytest.mark.parametrize("iterations", [0, -1])
def test_run_regression_rejects_non_positive_iterations(iterations):
    with pytest.raises(ValueError, match="iterations 必须大于 0"):
        run_regression(iterations)


class OrphanHarness(LifecycleHarness):
    """@brief 模拟 close 退出进程但未释放进程引用的异常实现。"""

    def close(self) -> bool:
        if self.started_by_studio and self.process is not None:
            self.process.quit()
            return True
        return False


def test_run_regression_reports_fail_on_orphan_process(monkeypatch):
    monkeypatch.setattr(stability_regression, "LifecycleHarness", OrphanHarness)

    result = run_regression(3)

    assert result["status"] == "fail"
    assert result["iterations"] == 3
    assert result["unique_processes"] == 3
    assert result["orphan_process"] is True
    assert result["cancelled"] is False


def test_connect_creates_owned_process_and_reuses_running_instance():
    harness = LifecycleHarness()
    first = harness.connect()
    assert first.process_id == 1
    assert first.running is True
    assert harness.started_by_studio is True
    assert harness.connect() is first


def test_connect_replaces_dead_instance_with_new_owned_process():
    harness = LifecycleHarness()
    dead = harness.connect()
    dead.running = False
    replacement = harness.connect()
    assert replacement is not dead
    assert replacement.process_id == 2
    assert harness.started_by_studio is True


def test_connect_adopts_existing_process_without_ownership():
    harness = LifecycleHarness()
    external = FakeCadProcess(99)
    assert harness.connect(existing=external) is external
    assert harness.process is external
    assert harness.started_by_studio is False


def test_cancel_flags_state():
    harness = LifecycleHarness()
    assert harness.cancelled is False
    harness.cancel()
    assert harness.cancelled is True


def test_close_quits_owned_process_and_releases_it():
    harness = LifecycleHarness()
    process = harness.connect()
    assert harness.close() is True
    assert process.running is False
    assert harness.process is None
    assert harness.started_by_studio is False
    assert harness.close() is False


def test_close_refuses_adopted_process():
    harness = LifecycleHarness()
    external = FakeCadProcess(7)
    harness.connect(existing=external)
    assert harness.close() is False
    assert external.running is True
    assert harness.process is external


def test_close_refuses_when_process_already_gone():
    harness = LifecycleHarness()
    harness.connect()
    harness.process = None
    assert harness.close() is False


def test_quit_marks_process_not_running():
    process = FakeCadProcess(5)
    process.quit()
    assert process.process_id == 5
    assert process.running is False


def test_console_entry_prints_json_result(capsys):
    script = Path(stability_regression.__file__)
    runpy.run_path(str(script), run_name="__main__")

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert payload["iterations"] == 20
    assert payload["orphan_process"] is False
