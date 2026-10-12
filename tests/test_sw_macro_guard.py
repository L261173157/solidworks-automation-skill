"""SolidWorks VBA 宏生成防护层的无 COM、无网络单元测试。"""
from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

import pytest

from scripts import sw_macro_guard

ROOT = Path(__file__).resolve().parents[1]

VALID_MACRO = """Option Explicit
Dim swApp As SldWorks.SldWorks
Dim swModel As SldWorks.ModelDoc2

Sub main()
    Set swApp = Application.SldWorks
End Sub
"""

MACRO_WITHOUT_SLDWORKS = "Dim swModel As ModelDoc2\n\nSub main()\nEnd Sub"
MACRO_WITHOUT_MODELDOC2 = "Dim swApp As Object\nSet swApp = Application.SldWorks\n\nSub main()\nEnd Sub"
MACRO_WITHOUT_SUB = "SldWorks.SldWorks\nModelDoc2\nEnd Sub"
MACRO_WITHOUT_END_SUB = "SldWorks.SldWorks\nModelDoc2\nSub main()"


class RecordingStream:
    """@brief 记录 reconfigure 调用的伪输出流。"""

    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class PlainStream:
    """@brief 不支持 reconfigure 的旧式输出流。"""


def test_configure_stdio_utf8_handles_both_stream_kinds(monkeypatch):
    """@brief 支持 reconfigure 的流必须被配置，旧流必须被安全跳过。"""
    stdout, stderr = RecordingStream(), PlainStream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    sw_macro_guard._configure_stdio_utf8()

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.parametrize(
    "model_name,expected",
    [
        (None, "generic"),
        ("", "generic"),
        ("   ", "generic"),
        ("gpt-4.1", "gpt"),
        ("OpenAI o3-mini", "gpt"),
        ("o4-preview", "gpt"),
        ("o1", "gpt"),
        ("moonshot-kimi-v2", "kimi"),
        ("Kimi", "kimi"),
        ("claude-sonnet", "claude"),
        ("anthropic-api", "claude"),
        ("deepseek-chat", "generic"),
    ],
)
def test_normalize_model_family(model_name, expected):
    assert sw_macro_guard.normalize_model_family(model_name) == expected


def test_build_prompt_defaults_to_strict_for_non_gpt_families():
    """@brief 未显式指定 strict 时，kimi/claude/generic 一律使用强格式提示词。"""
    prompt = sw_macro_guard.build_prompt("  做一个方块  ")

    assert prompt.startswith(sw_macro_guard.STRICT_FORMAT_PROMPT)
    assert "做一个方块" in prompt
    assert prompt.endswith("\n")


def test_build_prompt_uses_plain_prompt_for_gpt_family():
    prompt = sw_macro_guard.build_prompt("做一个方块", model_name="gpt-4.1")

    assert prompt.startswith(sw_macro_guard.GPT_PROMPT)
    assert "做一个方块" in prompt


def test_build_prompt_respects_explicit_strict_flags():
    strict = sw_macro_guard.build_prompt("需求", model_name="gpt-4.1", strict=True)
    relaxed = sw_macro_guard.build_prompt("需求", model_name="claude", strict=False)

    assert strict.startswith(sw_macro_guard.STRICT_FORMAT_PROMPT)
    assert relaxed.startswith(sw_macro_guard.GPT_PROMPT)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", ""),
        (None, ""),
        ("```vb\nSub main()\nEnd Sub\n```", "Sub main()\nEnd Sub"),
        # 产品正则 (?:vb|vba|visualbasic) 交替项先匹配 vb, 因此 vba 围栏只消耗 vb,
        # 剩余的 a 会落入代码体 (已记录的产品行为, 不在测试中修正)。
        ("```VBA\nA = 1\n```", "A\nA = 1"),
        ("```visualbasic\nX = 2\n```", "X = 2"),
        ("```\nbare fence\n```", "bare fence"),
        ("说明\n```vb\nA = 1\n```\n尾巴", "A = 1"),
        ("no fence here", "no fence here"),
        ("   padded code   ", "padded code"),
    ],
)
def test_extract_vba_code(text, expected):
    assert sw_macro_guard.extract_vba_code(text) == expected


def test_contains_any_pattern_matches_and_misses():
    assert sw_macro_guard._contains_any_pattern("SldWorks.SldWorks", sw_macro_guard.CORE_OBJECT_PATTERNS) is True
    assert sw_macro_guard._contains_any_pattern("swApp only", sw_macro_guard.CORE_OBJECT_PATTERNS) is True
    assert sw_macro_guard._contains_any_pattern("nothing here", sw_macro_guard.CORE_OBJECT_PATTERNS) is False


def test_validate_vba_macro_accepts_complete_macro():
    result = sw_macro_guard.validate_vba_macro(VALID_MACRO)

    assert result.ok is True
    assert result.issues == []


def test_validate_vba_macro_reports_every_issue_for_blank_code():
    result = sw_macro_guard.validate_vba_macro("   \n")

    assert result.ok is False
    assert result.issues == [
        "代码为空",
        "缺少 SolidWorks 核心对象 SldWorks",
        "缺少 ModelDoc2 文档对象",
        "缺少 Sub 入口",
        "缺少 End Sub",
    ]


@pytest.mark.parametrize(
    "code,expected_issue",
    [
        (MACRO_WITHOUT_SLDWORKS, "缺少 SolidWorks 核心对象 SldWorks"),
        (MACRO_WITHOUT_MODELDOC2, "缺少 ModelDoc2 文档对象"),
        (MACRO_WITHOUT_SUB, "缺少 Sub 入口"),
        (MACRO_WITHOUT_END_SUB, "缺少 End Sub"),
        (VALID_MACRO + "\n' ...", "包含占位符或未完成代码"),
        (VALID_MACRO + "\n' todo 后续补齐", "包含占位符或未完成代码"),
    ],
)
def test_validate_vba_macro_reports_single_issue(code, expected_issue):
    result = sw_macro_guard.validate_vba_macro(code)

    assert result.issues == [expected_issue]


def test_validate_vba_macro_flags_placeholder_in_partial_code():
    result = sw_macro_guard.validate_vba_macro("...")

    assert result.ok is False
    assert result.issues == [
        "缺少 SolidWorks 核心对象 SldWorks",
        "缺少 ModelDoc2 文档对象",
        "缺少 Sub 入口",
        "缺少 End Sub",
        "包含占位符或未完成代码",
    ]


@pytest.mark.parametrize(
    "request_text,expected",
    [
        ("做一个立方体", "cube"),
        (" machining a BOX ", "cube"),
        ("方块", "cube"),
        ("圆柱体", "cylinder"),
        ("拉伸凸台", "extrude"),
        ("先画草图", "sketch"),
        ("随便聊聊就好", None),
    ],
)
def test_select_template_key(request_text, expected):
    assert sw_macro_guard.select_template_key(request_text) == expected


def test_fallback_macro_for_request_returns_template_or_none():
    assert sw_macro_guard.fallback_macro_for_request("来个圆柱") == sw_macro_guard.VBA_TEMPLATES["cylinder"]
    assert sw_macro_guard.fallback_macro_for_request("没有任何关键词") is None


def test_all_builtin_templates_pass_validation():
    for key, template in sw_macro_guard.VBA_TEMPLATES.items():
        result = sw_macro_guard.validate_vba_macro(template)
        assert result.ok, (key, result.issues)


def test_generate_macro_returns_valid_model_output_on_first_attempt():
    calls = []

    def model_call(prompt, timeout_seconds):
        calls.append((prompt, timeout_seconds))
        return f"```vb\n{VALID_MACRO}\n```"

    result = sw_macro_guard.generate_macro_with_guard(
        "做一个方块", model_call, model_name="gpt-4.1", timeout_seconds=7, max_retries=2,
    )

    assert result.source == "model"
    assert result.attempts == 1
    assert result.code == VALID_MACRO.strip()
    assert result.validation.ok is True
    assert calls == [(sw_macro_guard.build_prompt("做一个方块", model_name="gpt-4.1"), 7)]


def test_generate_macro_retries_after_timeout_then_succeeds():
    prompts = []

    def flaky_model(prompt, _timeout_seconds):
        prompts.append(prompt)
        if len(prompts) == 1:
            raise TimeoutError("上游模型超时")
        return VALID_MACRO

    result = sw_macro_guard.generate_macro_with_guard("画个草图", flaky_model, model_name="kimi", max_retries=2)

    assert result.source == "model"
    assert result.attempts == 2
    assert result.validation.ok is True
    assert "上一轮输出未通过自动校验" in prompts[1]
    assert "当前校验问题：\n- 模型请求超时" in prompts[1]


def test_generate_macro_falls_back_to_local_template_after_failed_attempts():
    prompts = []

    def always_garbage(prompt, _timeout_seconds):
        prompts.append(prompt)
        return "抱歉，我无法完成该任务。"

    result = sw_macro_guard.generate_macro_with_guard("做一个立方体", always_garbage, max_retries=2)

    assert result.source == "local_template"
    assert result.attempts == 3
    assert result.code == sw_macro_guard.VBA_TEMPLATES["cube"]
    assert result.validation.ok is True
    assert len(prompts) == 3
    assert "上一轮输出未通过自动校验" in prompts[1]
    assert "当前校验问题：\n- 缺少 SolidWorks 核心对象 SldWorks" in prompts[1]
    assert "缺少 End Sub" in prompts[2]


def test_generate_macro_skips_issue_section_when_validation_reports_no_issues(monkeypatch):
    """@brief 校验器不返回任何问题时，重试提示词不得追加空的校验问题小节。"""

    def garbage(_prompt, _timeout_seconds):
        return "垃圾输出"

    seen = {}

    def model_call(prompt, _timeout_seconds):
        seen["prompt"] = prompt
        return "垃圾输出"

    monkeypatch.setattr(
        sw_macro_guard,
        "validate_vba_macro",
        lambda code: sw_macro_guard.MacroValidationResult(ok=False, issues=[]),
    )

    result = sw_macro_guard.generate_macro_with_guard("画个草图", model_call, max_retries=0)

    assert "当前校验问题" not in seen["prompt"]
    assert result.source == "local_template"
    assert result.attempts == 1
    assert result.validation.ok is False
    assert result.validation.issues == []


def test_generate_macro_raises_when_no_template_matches():
    def garbage(_prompt, _timeout_seconds):
        return "no macro here"

    with pytest.raises(ValueError) as excinfo:
        sw_macro_guard.generate_macro_with_guard("随便聊聊", garbage, max_retries=1)

    message = str(excinfo.value)
    assert "模型输出未通过校验，且没有匹配到本地 VBA 兜底模板" in message
    assert "缺少 End Sub" in message


def test_write_macro_file_creates_nested_dirs_and_returns_resolved_path(tmp_path):
    target = tmp_path / "nested" / "deep" / "macro.swb"

    returned = sw_macro_guard.write_macro_file(VALID_MACRO, str(target))

    assert Path(returned) == target.resolve()
    assert Path(returned).read_text(encoding="utf-8") == VALID_MACRO


def test_write_macro_file_expands_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    returned = sw_macro_guard.write_macro_file("X = 1", "~/macro.swb")

    assert Path(returned) == (tmp_path / "macro.swb").resolve()
    assert (tmp_path / "macro.swb").read_text(encoding="utf-8") == "X = 1"


def test_cli_prompt_prints_prompt_for_selected_model(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "prompt", "--model", "kimi", "做一个方块"])

    assert sw_macro_guard.main() == 0

    out = capsys.readouterr().out
    assert sw_macro_guard.STRICT_FORMAT_PROMPT in out
    assert "做一个方块" in out


def test_cli_prompt_default_model_uses_generic_family(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "prompt", "测试需求"])

    assert sw_macro_guard.main() == 0

    out = capsys.readouterr().out
    assert sw_macro_guard.STRICT_FORMAT_PROMPT in out


def test_cli_prompt_gpt_model_prints_plain_prompt(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "prompt", "--model", "gpt-5", "测试需求"])

    assert sw_macro_guard.main() == 0

    assert capsys.readouterr().out.startswith(sw_macro_guard.GPT_PROMPT)


def test_cli_validate_accepts_valid_file(tmp_path, monkeypatch, capsys):
    file = tmp_path / "macro.swb"
    file.write_text(VALID_MACRO, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "validate", str(file)])

    assert sw_macro_guard.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "issues": []}


def test_cli_validate_rejects_invalid_file_with_exit_code_one(tmp_path, monkeypatch, capsys):
    file = tmp_path / "bad.swb"
    file.write_text("只有注释", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "validate", str(file)])

    assert sw_macro_guard.main() == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "缺少 Sub 入口" in payload["issues"]


def test_cli_template_prints_code_without_output_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "template", "来个圆柱"])

    assert sw_macro_guard.main() == 0

    assert capsys.readouterr().out.strip() == sw_macro_guard.VBA_TEMPLATES["cylinder"]


def test_cli_template_writes_output_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "out" / "cylinder.swb"
    monkeypatch.setattr(
        sys, "argv",
        ["sw_macro_guard.py", "template", "来个圆柱", "--output", str(target)],
    )

    assert sw_macro_guard.main() == 0

    printed = capsys.readouterr().out.strip()
    assert Path(printed) == target.resolve()
    assert target.read_text(encoding="utf-8") == sw_macro_guard.VBA_TEMPLATES["cylinder"]


def test_cli_template_without_match_returns_one(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "template", "没有任何关键词"])

    assert sw_macro_guard.main() == 1

    assert "未匹配到本地模板" in capsys.readouterr().out


def test_cli_unknown_command_returns_one(monkeypatch):
    """@brief 子命令之外的字面量必须落到兜底返回 1。"""
    monkeypatch.setattr(
        sw_macro_guard, "_parse_args",
        lambda: argparse.Namespace(command="mystery"),
    )

    assert sw_macro_guard.main() == 1


def test_main_block_runs_prompt_command_via_runpy(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py", "prompt", "做个方块"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "sw_macro_guard.py"), run_name="__main__")

    assert excinfo.value.code == 0
    assert "做个方块" in capsys.readouterr().out


def test_main_block_argparse_error_exits_with_code_two(monkeypatch):
    """@brief 缺少子命令时 argparse 必须以退出码 2 结束。"""
    monkeypatch.setattr(sys, "argv", ["sw_macro_guard.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(ROOT / "scripts" / "sw_macro_guard.py"), run_name="__main__")

    assert excinfo.value.code == 2
