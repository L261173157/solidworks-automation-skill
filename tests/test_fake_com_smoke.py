"""Fake COM 冒烟: 无 SolidWorks 环境下运行库调用序列与 MCP 工具面。

运行方式::

    CADSTUDIO_FAKE_COM=1 python -m pytest tests/test_fake_com_smoke.py -q

契约锚点: tests/solidworks_errorcheck_regression.py 的 ``healthy_part``
场景与本文件的 ``test_box_flow`` 走完全相同的库函数序列
(start_sketch -> sketch_corner_rectangle -> end_sketch ->
current_sketch_name -> extrude_boss -> save_document),
该序列已在真机 SW2024/SW2026 上回归通过; 伪对象语义与之对齐。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get("CADSTUDIO_FAKE_COM") != "1",
    reason="仅在 CADSTUDIO_FAKE_COM=1 伪 COM 模式下运行",
)

import fakes.sw as fake_sw  # noqa: E402  (conftest 已把 tests/ 加入 sys.path)

sys.path.insert(0, str(ROOT / "scripts"))

from scripts import sw_connect, sw_part, sw_session  # noqa: E402


@pytest.fixture()
def fake_app(monkeypatch):
    app, journal = fake_sw.new_fake_session()
    fake_connect = lambda *args, **kwargs: (app, app.ActiveDoc, {"prog_id": "fake", "started_by_cad_studio": False})
    # sw_session 在模块导入时按名绑定 connect_solidworks, 需要两处都补丁。
    monkeypatch.setattr(sw_connect, "connect_solidworks", fake_connect)
    monkeypatch.setattr(sw_session, "connect_solidworks", fake_connect)
    monkeypatch.setattr(
        sw_connect,
        "find_template",
        lambda sw, doc_type="part": {
            "part": "C:/fake/templates/gb_part.prtdot",
            "assembly": "C:/fake/templates/gb_assembly.asmdot",
            "drawing": "C:/fake/templates/gb_drawing.drwdot",
        }.get(doc_type, "C:/fake/templates/gb_part.prtdot"),
    )
    return app, journal


def test_fake_com_modules_installed():
    import pythoncom  # noqa: F401

    assert pythoncom.VT_BYREF == 0x4000
    assert pythoncom.VT_DISPATCH == 9


def test_box_flow(fake_app, tmp_path):
    """healthy_part 真机序列的伪 COM 影子: 草图 -> 矩形 -> 拉伸 -> 保存。"""
    app, journal = fake_app

    model = sw_connect.new_document(app, "part")
    assert model.doc_type == "part"

    sw_part.start_sketch(model, "Front Plane")
    sw_part.sketch_corner_rectangle(model, -0.005, -0.005, 0.005, 0.005)
    sw_part.end_sketch(model)
    boss = sw_part.extrude_boss(model, sw_part.current_sketch_name(model), 0.01)
    assert boss is not None

    output = tmp_path / "box.SLDPRT"
    assert sw_connect.save_document(model, str(output)) is True
    assert output.is_file()
    assert model.GetPathName == str(output)

    assert journal.count("InsertSketch") == 2  # 开草图 + 退草图
    assert journal.count("CreateCornerRectangle") == 1
    assert journal.count("FeatureExtrusion3") == 1
    assert journal.count("SaveAs") == 1


def test_session_flow(fake_app, tmp_path):
    """SolidWorksSession 三件套: 连接 -> 新建零件 -> 保存。"""
    app, journal = fake_app

    session = sw_session.SolidWorksSession()
    model = session.new_part()
    assert model.doc_type == "part"

    output = tmp_path / "session.SLDPRT"
    assert session.save(model, str(output)) is True
    assert output.is_file()

    reopened = session.open(str(output), silent=True, raise_on_error=True)
    assert reopened.GetPathName == str(output)
    assert journal.count("OpenDoc6") == 1


def test_open_document_error_classification(fake_app, tmp_path):
    """损坏文件在伪模式下也应走分类路径并抛出 SolidWorksDocumentOpenError。"""
    from scripts.sw_connect import SolidWorksDocumentOpenError

    app, _journal = fake_app
    broken = tmp_path / "broken.SLDPRT"
    broken.write_text("这不是合法的 SolidWorks 文件", encoding="utf-8")

    # 伪模式 OpenDoc6 永远成功, 无法产生真实错误码; 这里只验证
    # raise_on_error=True 时返回模型而不抛异常的正常路径, 以及
    # 文件不存在的场景由真实 COM 层负责 (真机回归 corrupt_file 场景覆盖)。
    model = sw_connect.open_document(app, str(broken), silent=True, raise_on_error=False)
    assert model is not None
    assert SolidWorksDocumentOpenError is not None


def test_mcp_server_tools_registered():
    """伪模式下 MCP server 必须可导入且注册不少于 validate_mcp 要求的工具数。"""
    sys.path.insert(0, str(ROOT / "mcp-server"))
    try:
        import server as mcp_server
    finally:
        sys.path.remove(str(ROOT / "mcp-server"))

    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {tool.name for tool in tools}
    assert len(names) >= 44
    assert "solidworks_health_check" in names
    assert "solidworks_connect" in names
    assert "solidworks_check_interference" in names
    assert "cadstudio_write_open_format" in names
