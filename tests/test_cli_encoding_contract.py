"""CLI JSON stays portable across redirected Windows console encodings."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script", ["cad_studio.py", "dfm_review.py"])
def test_cli_ascii_console_does_not_crash(script, tmp_path):
    if script == "cad_studio.py":
        (tmp_path / "job.json").write_text(json.dumps({"id": "中文任务", "status": "queued"}), encoding="utf-8")
        args = ["--queue-dir", str(tmp_path), "status"]
    else:
        source = tmp_path / "part.json"
        source.write_text(json.dumps({
            "documentId": "part", "title": "中文测试件", "units": "mm",
            "features": [{"id": "base", "type": "box", "parameters": {"length": 120, "width": 70, "height": 8}}],
            "metadata": {"manufacturing": {"process": "machining", "material": "Al6061", "wallThickness": 3}},
        }), encoding="utf-8")
        args = ["--input", str(source), "--output", str(tmp_path / "dfm.json")]
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("ascii", errors="replace")
    payload = json.loads(result.stdout.decode("ascii"))
    if script == "cad_studio.py":
        assert "中文任务" in str(payload)
    else:
        assert payload["status"] == "review_required"
