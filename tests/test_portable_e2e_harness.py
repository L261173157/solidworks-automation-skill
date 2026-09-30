"""@brief 在无 Windows/WebView2 的机器上验证便携版测试的诊断和失败传播。"""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_portable_e2e_harness_regressions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the portable E2E harness tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "ai_team/release-portable-e2e.test.cjs"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
