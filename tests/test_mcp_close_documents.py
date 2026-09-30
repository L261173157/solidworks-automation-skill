"""关闭文档的伪 COM 回归：保存成功才关闭，显式丢弃必须包含脏文档。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "mcp-server") not in sys.path:
    sys.path.insert(0, str(ROOT / "mcp-server"))

import server  # noqa: E402


class FakeDocument:
    def __init__(self, title="part.SLDPRT", *, path=None, modified=True, visible=True, save=True):
        self.title = title
        self.path = f"C:/models/{title}" if path is None else path
        self.modified = modified
        self.Visible = visible
        self.save = save
        self.stays_dirty = False

    def GetTitle(self):
        return self.title

    def GetPathName(self):
        return self.path

    def GetSaveFlag(self):
        return self.modified


class FakeApplication:
    def __init__(self, documents):
        self.documents = list(documents)
        self.ActiveDoc = self.documents[0] if self.documents else None
        self.events = []
        self.close_error = None
        self.close_result = True
        self.leave_open = set()
        self.retain_in_memory = False
        self.fail_inspection_after_close = False

    def GetDocuments(self):
        if self.fail_inspection_after_close and any(event[0].startswith("close") for event in self.events):
            raise RuntimeError("COM disconnected after close")
        return tuple(self.documents) or None

    def CloseAllDocuments(self, include_unsaved):
        self.events.append(("close_all", include_unsaved))
        if self.close_error:
            raise RuntimeError(self.close_error)
        self.documents = [doc for doc in self.documents
                          if doc.title in self.leave_open or (doc.modified and not include_unsaved)]
        return self.close_result

    def CloseDoc(self, title):
        self.events.append(("close", title))
        if self.close_error:
            raise RuntimeError(self.close_error)
        for doc in list(self.documents):
            if doc.title in self.leave_open:
                continue
            if doc.title == title or not doc.Visible:
                if self.retain_in_memory and doc.title == title:
                    doc.Visible = False
                else:
                    self.documents.remove(doc)

    def save_document(self, doc):
        self.events.append(("save", doc.title))
        if isinstance(doc.save, Exception):
            raise doc.save
        if doc.save:
            doc.modified = doc.stays_dirty
        return doc.save


@pytest.fixture
def close_session(monkeypatch):
    """只替换 COM 边界；保留实际 MCP 包装、结果序列化和错误处理。"""
    def arrange(*documents):
        app = FakeApplication(documents)
        monkeypatch.setattr(server, "_load_automation_modules", lambda: None)
        monkeypatch.setattr(server, "pythoncom", None)
        monkeypatch.setattr(server, "connect_solidworks", lambda **_kwargs: (app, app.ActiveDoc), raising=False)
        monkeypatch.setattr(server, "save_document", app.save_document, raising=False)

        def member(obj, name):
            value = getattr(obj, name)
            return value() if callable(value) else value

        monkeypatch.setattr(server, "get_com_member", member, raising=False)
        return app

    return arrange


def call_close(**kwargs):
    return json.loads(server.solidworks_close_documents(server.SolidWorksCloseDocumentsInput(**kwargs)))


@pytest.mark.parametrize("close_all", [False, True])
@pytest.mark.parametrize("save_changes", [False, True])
def test_confirmation_does_not_save_or_close(close_session, close_all, save_changes):
    app = close_session(FakeDocument())
    result = call_close(close_all=close_all, save_changes=save_changes)
    assert result["status"] == "confirmation_required"
    assert result["discard_changes"] is (not save_changes)
    assert ("丢弃" in result["impact"]) is (not save_changes)
    assert app.events == []


@pytest.mark.parametrize("close_all", [False, True])
def test_save_happens_before_close(close_session, close_all):
    doc = FakeDocument()
    app = close_session(doc)
    result = call_close(close_all=close_all, save_changes=True, confirm=True)
    assert result["status"] == "ok"
    assert result["closed"] == ("all" if close_all else doc.title)
    assert app.events == [("save", doc.title), ("close_all", False) if close_all else ("close", doc.title)]
    assert result["remaining_documents"] == []
    assert not result["discard_changes"]


@pytest.mark.parametrize("close_all", [False, True])
@pytest.mark.parametrize("save_result", [False, RuntimeError("disk full")])
def test_failed_save_never_closes_any_documents(close_session, close_all, save_result):
    doc = FakeDocument(save=save_result)
    app = close_session(doc, FakeDocument("other.SLDPRT", modified=False))
    result = call_close(close_all=close_all, save_changes=True, confirm=True)
    assert result["status"] == "save_failed"
    assert result["closed"] == []
    assert result["failures"][0]["title"] == doc.title
    assert app.events == [("save", doc.title)]
    assert len(app.documents) == 2


@pytest.mark.parametrize("close_all", [False, True])
@pytest.mark.parametrize("modified", [False, True])
def test_unnamed_document_needs_save_as_even_when_clean(close_session, close_all, modified):
    doc = FakeDocument("Part1", path="", modified=modified)
    app = close_session(doc)
    result = call_close(close_all=close_all, save_changes=True, confirm=True)
    assert result["status"] == "save_as_required"
    assert result["documents"][0]["path"] == ""
    assert result["closed"] == []
    assert app.events == []


@pytest.mark.parametrize("close_all", [False, True])
def test_discard_closes_dirty_and_unnamed_documents_without_saving(close_session, close_all):
    doc = FakeDocument("Part1", path="")
    app = close_session(doc)
    result = call_close(close_all=close_all, save_changes=False, confirm=True)
    assert result["status"] == "ok"
    assert result["discard_changes"] is True
    assert result["saved_documents"] == []
    assert app.events == [("close_all", True) if close_all else ("close", doc.title)]
    assert app.documents == []


def test_close_all_saves_every_modified_document_and_skips_clean(close_session):
    docs = [FakeDocument("a.SLDPRT"), FakeDocument("b.SLDPRT", modified=False), FakeDocument("c.SLDPRT")]
    app = close_session(*docs)
    result = call_close(close_all=True, save_changes=True, confirm=True)
    assert result["status"] == "ok"
    assert app.events == [("save", "a.SLDPRT"), ("save", "c.SLDPRT"), ("close_all", False)]
    assert len(result["saved_documents"]) == 2


def test_close_all_preflights_unnamed_documents_before_any_save(close_session):
    app = close_session(FakeDocument(), FakeDocument("Part2", path=""))
    assert call_close(close_all=True, save_changes=True, confirm=True)["status"] == "save_as_required"
    assert app.events == []


def test_successful_save_that_stays_dirty_does_not_close(close_session):
    doc = FakeDocument()
    doc.stays_dirty = True
    app = close_session(doc)
    result = call_close(save_changes=True, confirm=True)
    assert result["status"] == "save_failed"
    assert result["failures"][0]["reason"] == "still_modified"
    assert app.events == [("save", doc.title)]


def test_saving_child_that_dirties_saved_parent_blocks_closure(close_session, monkeypatch):
    parent, child = FakeDocument("assembly.SLDASM"), FakeDocument()
    app = close_session(parent, child)
    save = app.save_document

    def save_and_dirty_parent(doc):
        result = save(doc)
        if doc is child:
            parent.modified = True
        return result

    monkeypatch.setattr(server, "save_document", save_and_dirty_parent)
    result = call_close(close_all=True, save_changes=True, confirm=True)
    assert result["status"] == "save_failed"
    assert result["failures"][0]["title"] == parent.title
    assert all(event[0] == "save" for event in app.events)


@pytest.mark.parametrize("close_all", [False, True])
def test_noop_close_reports_remaining_documents(close_session, close_all):
    doc = FakeDocument()
    app = close_session(doc)
    app.leave_open.add(doc.title)
    result = call_close(close_all=close_all, confirm=True)
    assert result["status"] == "close_failed"
    assert result["closed"] == []
    assert result["remaining_documents"][0]["title"] == doc.title


def test_partial_close_all_reports_actual_closed_and_remaining(close_session):
    first, second = FakeDocument("first.SLDPRT"), FakeDocument("second.SLDPRT")
    app = close_session(first, second)
    app.leave_open.add(second.title)
    app.close_result = False
    result = call_close(close_all=True, confirm=True)
    assert result["status"] == "close_failed"
    assert result["closed"] == [first.title]
    assert result["remaining_documents"][0]["title"] == second.title
    assert "returned false" in result["message"]


def test_false_close_all_return_is_not_reported_as_success(close_session):
    app = close_session(FakeDocument())
    app.close_result = False
    result = call_close(close_all=True, confirm=True)
    assert result["status"] == "close_failed"
    assert result["remaining_documents"] == []


@pytest.mark.parametrize("close_all", [False, True])
def test_com_close_exception_is_reported_without_claiming_success(close_session, close_all):
    app = close_session(FakeDocument())
    app.close_error = "COM busy"
    result = call_close(close_all=close_all, confirm=True)
    assert result["status"] == "close_failed"
    assert result["message"] == "COM busy"
    assert result["closed"] == []


def test_verification_failure_reports_unknown_outcome(close_session):
    app = close_session(FakeDocument())
    app.fail_inspection_after_close = True
    result = call_close(confirm=True)
    assert result["status"] == "close_unverified"
    assert result["closed"] is None
    assert result["remaining_documents"] is None


@pytest.mark.parametrize("modified,path", [(True, "C:/models/hidden.SLDPRT"), (False, "")])
def test_single_save_protects_other_hidden_unsaved_documents(close_session, modified, path):
    app = close_session(FakeDocument(), FakeDocument("hidden.SLDPRT", path=path, modified=modified, visible=False))
    result = call_close(save_changes=True, confirm=True)
    assert result["status"] == "blocked"
    assert result["reason"] == "hidden_documents_at_risk"
    assert app.events == []


def test_single_close_leaves_other_visible_dirty_document_alone(close_session):
    other = FakeDocument("other.SLDPRT")
    app = close_session(FakeDocument(), other)
    result = call_close(save_changes=True, confirm=True)
    assert result["status"] == "ok"
    assert app.documents == [other]
    assert other.modified
    assert ("save", other.title) not in app.events


def test_referenced_document_window_can_close_while_model_remains_loaded(close_session):
    app = close_session(FakeDocument())
    app.retain_in_memory = True
    result = call_close(save_changes=True, confirm=True)
    assert result["status"] == "ok"
    assert result["retained_in_memory"] is True


def test_close_all_empty_session_does_not_require_active_document(close_session):
    close_session()
    result = call_close(close_all=True, save_changes=True, confirm=True)
    assert result["status"] == "ok"
    assert result["remaining_documents"] == []


def test_property_form_com_members_work(close_session):
    doc = FakeDocument(modified=False)
    doc.GetTitle = doc.title
    doc.GetPathName = doc.path
    doc.GetSaveFlag = False
    app = close_session(doc)
    result = call_close(save_changes=True, confirm=True)
    assert result["status"] == "ok"
    assert app.events == [("close", doc.title)]


def test_pack_and_go_timeout_does_not_read_blocked_com_again(close_session, monkeypatch, tmp_path):
    close_session(FakeDocument())
    monkeypatch.setattr(server, "pack_and_go", lambda *_args, **_kwargs: {
        "status": "error", "error_code": "SW_PACK_AND_GO_TIMEOUT",
    }, raising=False)

    def forbidden_summary(_model):
        pytest.fail("A COM read after timeout could hang the MCP response")

    monkeypatch.setattr(server, "_model_summary", forbidden_summary)
    result = json.loads(server.solidworks_pack_and_go_tool(server.SolidWorksPackAndGoInput(output_dir=str(tmp_path))))
    assert result["error_code"] == "SW_PACK_AND_GO_TIMEOUT"
    assert "document" not in result


def test_saving_active_part_that_dirties_hidden_parent_blocks_closure(close_session, monkeypatch):
    child = FakeDocument()
    parent = FakeDocument("assembly.SLDASM", modified=False, visible=False)
    app = close_session(child, parent)
    save = app.save_document

    def save_and_dirty_parent(doc):
        result = save(doc)
        parent.modified = True
        return result

    monkeypatch.setattr(server, "save_document", save_and_dirty_parent)
    result = call_close(save_changes=True, confirm=True)
    assert result["status"] == "blocked"
    assert result["reason"] == "hidden_documents_at_risk"
    assert app.events == [("save", child.title)]
    assert len(app.documents) == 2


def test_single_discard_reports_collateral_hidden_document_closures(close_session):
    hidden = FakeDocument("hidden.SLDPRT", visible=False)
    app = close_session(FakeDocument(), hidden)
    result = call_close(confirm=True)
    assert result["status"] == "ok"
    assert result["also_closed_documents"][0]["title"] == hidden.title
    assert app.documents == []


def test_save_failure_reports_latest_remaining_dirty_flags(close_session):
    saved = FakeDocument("saved.SLDPRT")
    close_session(saved, FakeDocument("failed.SLDPRT", save=False))
    result = call_close(close_all=True, save_changes=True, confirm=True)
    assert result["status"] == "save_failed"
    assert result["remaining_documents"][0]["modified"] is False
    assert result["remaining_documents"][1]["modified"] is True


def test_close_all_save_mode_preserves_documents_dirtied_during_close(close_session, monkeypatch):
    doc = FakeDocument()
    app = close_session(doc)
    close_all = app.CloseAllDocuments

    def dirty_then_close(include_unsaved):
        doc.modified = True
        return close_all(include_unsaved)

    monkeypatch.setattr(app, "CloseAllDocuments", dirty_then_close)
    result = call_close(close_all=True, save_changes=True, confirm=True)
    assert result["status"] == "close_failed"
    assert result["closed"] == []
    assert result["remaining_documents"][0]["modified"] is True
    assert app.documents == [doc]
