"""PowerShell must not hide an earlier failing native validation command."""
from pathlib import Path


def test_multicommand_release_steps_propagate_each_native_exit_code():
    workflow = Path(__file__).resolve().parents[1] / '.github/workflows/windows-release.yml'
    lines = workflow.read_text(encoding='utf-8').splitlines()
    commands = ('python -m pytest', 'python scripts/validate_', 'python scripts/release_check',
                'npm test', 'npm run build', 'cargo fmt', 'cargo test', 'cargo clippy',
                'node ai_team/', 'powershell -NoProfile -File ai_team/')
    checked = 0
    for index, line in enumerate(lines):
        if line.startswith('          ') and line.strip().startswith(commands):
            assert lines[index + 1].strip() == 'if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }'
            checked += 1
    assert checked == 11
