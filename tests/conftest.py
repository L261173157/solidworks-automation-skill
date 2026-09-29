"""仓库级 pytest conftest。

环境变量 ``CADSTUDIO_FAKE_COM=1`` 时, 在任何库代码导入之前把
``pythoncom`` / ``win32com`` / ``pywintypes`` 替换为 ``tests/fakes`` 提供的
最小语义桩, 使无 SolidWorks 的机器 (含 windows-latest CI) 能够运行工具级
冒烟测试, 且永远不会触发 ``sw_preflight`` 缺依赖时的交互式安装确认
(该确认在 pytest 捕获 stdout 时直接崩溃)。

不设置该变量时本文件不做任何事, 真机行为与历史完全一致。
"""
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

if os.environ.get("CADSTUDIO_FAKE_COM") == "1":
    import fakes.com as _fake_com

    _fake_com.install()
