"""SolidWorksSession 会话门面 API 的无 COM 离线单元测试。

所有底层 COM 边界 (connect_solidworks / new_document / open_document /
save_document / get_com_member / close_owned_solidworks / 具体导出函数) 均以
monkeypatch 替换, 只验证门面自身的路由、参数透传与错误分支。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from scripts import sw_session


class FakeSw:
    """@brief 最小 SolidWorks 应用桩: 活动文档 + CloseDoc 调用记录。"""

    def __init__(self):
        self.active = None
        self.closed_titles = []

    @property
    def ActiveDoc(self):
        return self.active

    def CloseDoc(self, title):
        self.closed_titles.append(title)
        self.active = None


class StrictActiveDocSw(FakeSw):
    """@brief 访问 ActiveDoc 即失败, 用于证明该路径未被触达。"""

    @property
    def ActiveDoc(self):
        raise AssertionError("不应访问 ActiveDoc")


class FakeDoc:
    def __init__(self, title):
        self.title = title


def make_session(monkeypatch, sw, model=None, connection_info=None):
    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return sw, model, connection_info or {"started_by_cad_studio": False}

    monkeypatch.setattr(sw_session, "connect_solidworks", fake_connect)
    session = sw_session.SolidWorksSession()
    session.connect_kwargs = seen
    return session


def test_session_init_and_factory_forward_arguments(monkeypatch):
    sw = FakeSw()
    doc = FakeDoc("p1")
    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return sw, doc, {"started_by_cad_studio": True}

    monkeypatch.setattr(sw_session, "connect_solidworks", fake_connect)
    session = sw_session.SolidWorksSession(version=2024, wait_seconds=9, visible=False)

    assert isinstance(session, sw_session.SolidWorksSession)
    assert seen == {"version": 2024, "wait_seconds": 9, "visible": False, "return_metadata": True}
    assert session.sw is sw
    assert session.model is doc
    assert session.connection_info == {"started_by_cad_studio": True}

    factory_seen = {}

    def factory_connect(**kwargs):
        factory_seen.update(kwargs)
        return sw, doc, {"started_by_cad_studio": False}

    monkeypatch.setattr(sw_session, "connect_solidworks", factory_connect)
    created = sw_session.session(visible=False)

    assert isinstance(created, sw_session.SolidWorksSession)
    assert factory_seen == {"version": None, "wait_seconds": 5, "visible": False, "return_metadata": True}


def test_active_doc_property_syncs_model(monkeypatch):
    sw = FakeSw()
    doc = FakeDoc("active-1")
    session = make_session(monkeypatch, sw, model=None)
    sw.active = doc

    assert session.model is None
    assert session.active_doc is doc
    assert session.model is doc


def test_new_routes_doc_types_and_templates(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)
    seen = []

    def fake_new_document(sw_arg, doc_type="part", template_path=None):
        seen.append((sw_arg, doc_type, template_path))
        return FakeDoc(doc_type)

    monkeypatch.setattr(sw_session, "new_document", fake_new_document)

    assembly = session.new("assembly", template_path=r"C:\tpl.asm")
    part = session.new_part()
    default = session.new()
    assembly2 = session.new_assembly()
    drawing = session.new_drawing()

    assert seen == [
        (sw, "assembly", r"C:\tpl.asm"),
        (sw, "part", None),
        (sw, "part", None),
        (sw, "assembly", None),
        (sw, "drawing", None),
    ]
    assert [doc.title for doc in (assembly, part, default, assembly2, drawing)] == [
        "assembly",
        "part",
        "part",
        "assembly",
        "drawing",
    ]
    assert session.model is drawing


def test_open_forwards_options_and_updates_model(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)
    seen = []

    def fake_open_document(sw_arg, file_path, read_only=False, silent=False, raise_on_error=True):
        seen.append((sw_arg, file_path, read_only, silent, raise_on_error))
        return FakeDoc(Path(file_path).name)

    monkeypatch.setattr(sw_session, "open_document", fake_open_document)

    doc = session.open(r"C:\temp\part.sldprt")
    silent_doc = session.open(r"C:\temp\part.sldprt", read_only=True, silent=True, raise_on_error=False)

    assert seen == [
        (sw, r"C:\temp\part.sldprt", False, False, True),
        (sw, r"C:\temp\part.sldprt", True, True, False),
    ]
    assert doc.title == "part.sldprt"
    assert session.model is silent_doc


def test_save_prefers_given_model_without_touching_active_doc(monkeypatch):
    sw = StrictActiveDocSw()
    session = make_session(monkeypatch, sw)
    model = FakeDoc("given")
    seen = []

    def fake_save_document(model_arg, file_path=None):
        seen.append((model_arg, file_path))
        return True

    monkeypatch.setattr(sw_session, "save_document", fake_save_document)

    assert session.save(model, r"C:\out\part.sldprt") is True
    assert seen == [(model, r"C:\out\part.sldprt")]


def test_save_falls_back_to_active_doc(monkeypatch):
    sw = FakeSw()
    active = FakeDoc("active")
    sw.active = active
    session = make_session(monkeypatch, sw)
    seen = []

    monkeypatch.setattr(
        sw_session, "save_document", lambda model_arg, file_path=None: seen.append((model_arg, file_path)) or "saved"
    )

    assert session.save() == "saved"
    assert seen == [(active, None)]


def test_save_without_any_document_raises(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)

    with pytest.raises(RuntimeError, match="当前没有可保存的活动文档"):
        session.save()


def test_export_requires_a_document(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)

    with pytest.raises(RuntimeError, match="当前没有可导出的活动文档"):
        session.export()


def test_export_requires_output_path(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)
    model = FakeDoc("m")

    with pytest.raises(ValueError, match="必须提供 output_path"):
        session.export(model, None)
    with pytest.raises(ValueError, match="必须提供 output_path"):
        session.export(model, "")


def test_export_rejects_unsupported_format(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)
    model = FakeDoc("m")

    with pytest.raises(ValueError, match="暂不支持导出格式: .xyz"):
        session.export(model, r"C:\temp\out.xyz")


def test_export_infers_format_from_suffix(monkeypatch, tmp_path):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)
    model = FakeDoc("m")
    seen = []

    def fake_export_step(model_arg, output_path):
        seen.append((model_arg, output_path))
        return "step-ok"

    monkeypatch.setitem(sw_session.EXPORTERS, ".step", fake_export_step)
    output = tmp_path / "deep" / "dir" / "out.step"

    assert session.export(model, output) == "step-ok"
    assert seen == [(model, str(output))]
    assert output.parent.is_dir()


def test_export_format_ext_overrides_suffix_and_passes_kwargs(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)
    model = FakeDoc("m")
    seen = []

    def fake_export_stl(model_arg, output_path, quality="fine"):
        seen.append((model_arg, output_path, quality))

    monkeypatch.setitem(sw_session.EXPORTERS, ".stl", fake_export_stl)

    session.export(model, r"C:\temp\out.step", format_ext="stl", quality="coarse")

    # 输出路径保持原样, 仅格式解析走 format_ext; 无点扩展名自动补点。
    assert seen == [(model, r"C:\temp\out.step", "coarse")]


def test_close_by_model_title_resets_current_model(monkeypatch):
    sw = FakeSw()
    model = FakeDoc("part-1")
    session = make_session(monkeypatch, sw, model=model)

    def fake_get_com_member(obj, name, *args):
        assert name == "GetTitle"
        return "part-1" if obj is model else "other"

    monkeypatch.setattr(sw_session, "get_com_member", fake_get_com_member)

    assert session.close(model) is True
    assert sw.closed_titles == ["part-1"]
    # 关闭的是当前 model, 因此同步为关闭后的活动文档 (None)。
    assert session.model is None


def test_close_without_any_document_returns_false(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)

    assert session.close() is False
    assert sw.closed_titles == []


def test_close_falls_back_to_active_doc_title(monkeypatch):
    sw = FakeSw()
    active = FakeDoc("active-1")
    sw.active = active
    session = make_session(monkeypatch, sw)

    monkeypatch.setattr(sw_session, "get_com_member", lambda obj, name, *args: obj.title)

    assert session.close() is True
    assert sw.closed_titles == ["active-1"]
    # 当前 model 为空, 不做同步。
    assert session.model is None


def test_close_with_title_keeps_model_when_titles_differ(monkeypatch):
    sw = FakeSw()
    model = FakeDoc("still-open")
    session = make_session(monkeypatch, sw, model=model)

    monkeypatch.setattr(sw_session, "get_com_member", lambda obj, name, *args: obj.title)

    assert session.close(title="known") is True
    assert sw.closed_titles == ["known"]
    assert session.model is model


def test_close_title_with_disconnected_model_assumes_current(monkeypatch):
    """@brief GetTitle 抛 COM 异常时按“正在关闭当前文档”兜底处理。"""
    sw = FakeSw()
    model = FakeDoc("ghost")
    session = make_session(monkeypatch, sw, model=model)

    def raising_get_com_member(obj, name, *args):
        raise RuntimeError("找不到成员")

    monkeypatch.setattr(sw_session, "get_com_member", raising_get_com_member)

    assert session.close(title="ghost") is True
    assert sw.closed_titles == ["ghost"]
    assert session.model is None


def test_close_with_title_and_no_current_model(monkeypatch):
    sw = FakeSw()
    session = make_session(monkeypatch, sw)

    assert session.close(title="known") is True
    assert sw.closed_titles == ["known"]
    assert session.model is None


def test_quit_owned_instance_forwards_ownership_flag(monkeypatch):
    sw = FakeSw()
    calls = []

    def fake_close_owned(sw_arg, started_by_cad_studio):
        calls.append((sw_arg, started_by_cad_studio))
        return "closed"

    monkeypatch.setattr(sw_session, "close_owned_solidworks", fake_close_owned)
    session = make_session(monkeypatch, sw, connection_info={"started_by_cad_studio": True})
    assert session.quit_owned_instance() == "closed"
    assert calls == [(sw, True)]

    attached = make_session(monkeypatch, sw, connection_info={"started_by_cad_studio": False})
    assert attached.quit_owned_instance() == "closed"
    assert calls == [(sw, True), (sw, False)]


def test_fallback_import_path_without_package(monkeypatch):
    """@brief 以文件方式导入时, 相对导入失败须回退到顶级模块导入。"""
    module_path = Path(sw_session.__file__)
    monkeypatch.syspath_prepend(str(module_path.parent))
    spec = importlib.util.spec_from_file_location("_sw_session_fallback", module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_sw_session_fallback", module)
    spec.loader.exec_module(module)

    assert module.EXPORTERS.keys() == sw_session.EXPORTERS.keys()
    assert module.session is not sw_session.session
