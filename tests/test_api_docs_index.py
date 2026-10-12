"""API 签名索引单元测试: 纯查询逻辑 (真机生成走 solidworks_api_index 回归)。"""
import importlib.util
import json
import runpy
import sys
import types
from pathlib import Path

import pytest

from scripts import api_docs_index
from scripts.api_docs_index import CURATED_NOTES, load_index, lookup


SYNTHETIC_INDEX = {
    "schemaVersion": "1.0",
    "interfaces": {
        "ISldWorks": {
            "AddMate5": {"kind": "method", "params": 15},
            "ActivateDoc3": {"kind": "method", "params": 1},
        },
        "IModelDoc2": {
            "Extension": {"kind": "propget", "params": 0},
        },
        "IComponent2": {
            "GetCorresponding": {"kind": "method", "params": 2},
        },
    },
    "curated_notes": CURATED_NOTES,
}


def test_lookup_by_member_name():
    hits = lookup("AddMate5", index=SYNTHETIC_INDEX)
    assert len(hits) == 1
    assert hits[0]["interface"] == "ISldWorks"
    assert hits[0]["params"] == 15
    assert "by-ref" in (hits[0]["note"] or "")


def test_lookup_by_interface_name_returns_all_members():
    hits = lookup("icomponent2", index=SYNTHETIC_INDEX)
    assert any(hit["member"] == "GetCorresponding" for hit in hits)


def test_lookup_case_insensitive_and_limit():
    hits = lookup("mate", index=SYNTHETIC_INDEX, limit=1)
    assert len(hits) == 1


def test_lookup_empty_query_returns_empty():
    assert lookup("", index=SYNTHETIC_INDEX) == []


def test_load_index_missing_file_returns_curated_only(tmp_path):
    index = load_index(tmp_path / "missing.json")
    assert index["interfaces"] == {}
    assert index["curated_notes"] == CURATED_NOTES


def test_curated_notes_cover_hand_verified_facts():
    assert "ISldWorks.AddMate5" in CURATED_NOTES
    assert "15" in CURATED_NOTES["ISldWorks.AddMate5"]


# ---------------------------------------------------------------------------
# 补全覆盖率: 类型库枚举 / 索引构建 / 文件生成 / main 入口 / 导入回退分支
# ---------------------------------------------------------------------------


class FakeFuncDesc:
    """@brief 模拟 pywin32 FUNCDESC 位置元组: [0]=memid [2]=参数 [4]=invkind。"""

    def __init__(self, memid, params, invkind):
        self._slots = {0: memid, 2: params, 4: invkind}

    def __getitem__(self, index):
        return self._slots[index]


class FakeTypeInfo:
    """@brief func 项为 (memid, invkind, params, names) 元组或 Exception。"""

    def __init__(self, attr, funcs):
        self._attr = attr
        self._funcs = funcs

    def GetTypeAttr(self):
        if isinstance(self._attr, Exception):
            raise self._attr
        return self._attr

    def GetFuncDesc(self, index):
        item = self._funcs[index]
        if isinstance(item, Exception):
            raise item
        memid, invkind, params, _names = item
        return FakeFuncDesc(memid, params, invkind)

    def GetNames(self, memid):
        for item in self._funcs:
            if not isinstance(item, Exception) and item[0] == memid:
                return item[3]
        return ()


class FakeTypeLib:
    """@brief 每项为 (接口名或 Exception, FakeTypeInfo)。"""

    def __init__(self, infos):
        self._infos = infos

    def GetTypeInfoCount(self):
        return len(self._infos)

    def GetDocumentation(self, index):
        name = self._infos[index][0]
        if isinstance(name, Exception):
            raise name
        return (name, None, 0, None)

    def GetTypeInfo(self, index):
        return self._infos[index][1]


def _fake_pythoncom(library):
    return types.SimpleNamespace(LoadTypeLib=lambda path: library)


def test_enumerate_typelib_extracts_whitelisted_member_facts():
    empty_attr = (0, 0, 0, 0, 0, 0, 0, 0, 0)
    lib = FakeTypeLib([
        (RuntimeError("文档读取失败"), None),  # GetDocumentation 异常 → 跳过
        ("IBogus", FakeTypeInfo((0, 0, 0, 0, 0, 0, 1, 0, 0), [])),  # 非白名单 → 跳过
        ("ISldWorks", FakeTypeInfo(RuntimeError("attr 失败"), [])),  # GetTypeAttr 异常 → 跳过
        ("ISketch", FakeTypeInfo(empty_attr, [])),  # 零成员接口 → 不入索引
        ("ISldWorks", FakeTypeInfo((0, 0, 0, 0, 0, 0, 6, 0, 0), [
            RuntimeError("funcdesc 失败"),  # GetFuncDesc 异常 → 跳过
            (1, 1, (1, 1), ("AddMate5",)),  # method 2 参
            (2, 3, None, ("Invoke2",)),  # 未知 invkind → invkind3; params None → 0 参
            (3, 2, (), ()),  # GetNames 为空 → 空名跳过
            (1, 1, tuple(range(15)), ("AddMate5",)),  # 同名重载 15 参 → 覆盖 2 参
            (1, 2, (), ("AddMate5",)),  # 同名 0 参 → 保留参数最多的形态
        ])),
    ])

    result = api_docs_index._enumerate_typelib(_fake_pythoncom(lib), Path("fake/sldworks.tlb"))

    assert set(result) == {"ISldWorks"}
    assert result["ISldWorks"]["AddMate5"] == {"kind": "method", "params": 15}
    assert result["ISldWorks"]["Invoke2"] == {"kind": "invkind3", "params": 0}


def test_build_index_reports_structure_and_counts(monkeypatch, tmp_path):
    lib = FakeTypeLib([
        ("ISldWorks", FakeTypeInfo((0, 0, 0, 0, 0, 0, 1, 0, 0), [
            (1, 1, (1,), ("LoadFile4",)),
        ])),
    ])
    tlb = tmp_path / "sldworks.tlb"
    tlb.write_bytes(b"typelib")
    monkeypatch.setattr(
        api_docs_index, "import_com_dependencies",
        lambda *args, **kwargs: (_fake_pythoncom(lib), None, None),
    )

    index = api_docs_index.build_index(tlb, sw_revision="32.5.0")

    assert index["schemaVersion"] == "1.0"
    assert index["typelib"] == str(tlb)
    assert index["sw_revision"] == "32.5.0"
    assert index["interface_count"] == 1
    assert index["member_count"] == 1
    assert index["interfaces"]["ISldWorks"]["LoadFile4"] == {"kind": "method", "params": 1}
    assert index["curated_notes"] is CURATED_NOTES
    assert index["limitations"]


def test_build_index_without_typelib_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(
        api_docs_index, "import_com_dependencies", lambda *args, **kwargs: (object(), None, None)
    )
    monkeypatch.setattr(api_docs_index, "_find_sldworks_typelib", lambda: None)

    with pytest.raises(RuntimeError, match="未找到 sldworks.tlb"):
        api_docs_index.build_index()


def test_find_sldworks_typelib_delegates_to_probe(monkeypatch):
    sentinel = Path("C:/SW/sldworks.tlb")

    def fake_probe(patterns):
        assert patterns is api_docs_index.TYPELIB_PATTERNS
        return sentinel

    monkeypatch.setattr(api_docs_index, "_find_typelib", fake_probe)
    assert api_docs_index._find_sldworks_typelib() == sentinel


def test_generate_index_file_writes_explicit_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        api_docs_index, "build_index",
        lambda **kwargs: {"schemaVersion": "1.0", "interfaces": {"ISldWorks": {}}},
    )
    target = tmp_path / "nested" / "api_index.json"

    result = api_docs_index.generate_index_file(target, sw_revision="32.5.0")

    assert result == target
    assert json.loads(target.read_text(encoding="utf-8"))["schemaVersion"] == "1.0"


def test_generate_index_file_defaults_to_reference_data_path(monkeypatch, tmp_path):
    default_target = tmp_path / "data" / "api_index.json"
    monkeypatch.setattr(api_docs_index, "build_index", lambda **kwargs: {"interfaces": {}})
    monkeypatch.setattr(api_docs_index, "default_index_path", lambda: default_target)

    result = api_docs_index.generate_index_file()

    assert result == default_target
    assert default_target.is_file()


def test_load_index_reads_existing_file(tmp_path):
    path = tmp_path / "api_index.json"
    path.write_text(
        json.dumps({"schemaVersion": "1.0", "interfaces": {"ISldWorks": {}}}), encoding="utf-8"
    )

    index = load_index(path)

    assert index["interfaces"] == {"ISldWorks": {}}


def test_load_index_defaults_to_reference_data_path(monkeypatch, tmp_path):
    monkeypatch.setattr(api_docs_index, "default_index_path", lambda: tmp_path / "missing.json")

    index = load_index()

    assert index["interfaces"] == {}
    assert index["curated_notes"] is CURATED_NOTES


def test_lookup_without_index_loads_default_index(monkeypatch):
    monkeypatch.setattr(api_docs_index, "load_index", lambda index_path=None: SYNTHETIC_INDEX)

    hits = lookup("addmate5")

    assert len(hits) == 1
    assert hits[0]["note"] == CURATED_NOTES["ISldWorks.AddMate5"]


def test_lookup_ignores_blank_and_none_queries():
    assert lookup(None, index=SYNTHETIC_INDEX) == []
    assert lookup("   ", index=SYNTHETIC_INDEX) == []


def test_main_lookup_prints_hits(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["api_docs_index.py", "--lookup", "addmate5", "--limit", "2"])
    monkeypatch.setattr(api_docs_index, "load_index", lambda index_path=None: SYNTHETIC_INDEX)

    assert api_docs_index.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["interface"] == "ISldWorks"


def test_main_generate_with_explicit_output_and_revision(monkeypatch, tmp_path, capsys):
    target = tmp_path / "api_index.json"
    seen = {}

    def fake_generate(output_path, sw_revision=None):
        seen["output"] = output_path
        seen["revision"] = sw_revision
        return output_path

    monkeypatch.setattr(api_docs_index, "generate_index_file", fake_generate)
    monkeypatch.setattr(
        api_docs_index, "load_index",
        lambda index_path=None: {"interface_count": 3, "member_count": 11},
    )
    monkeypatch.setattr(
        sys, "argv", ["api_docs_index.py", "--output", str(target), "--revision", "32.5.0"]
    )

    assert api_docs_index.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"status": "pass", "output": str(target), "interface_count": 3, "member_count": 11}
    assert seen == {"output": Path(target), "revision": "32.5.0"}


def test_main_generate_defaults_output_and_revision(monkeypatch, capsys):
    seen = {}

    def fake_generate(output_path, sw_revision=None):
        seen["output"] = output_path
        seen["revision"] = sw_revision
        return Path("C:/temp/api_index.json")

    monkeypatch.setattr(api_docs_index, "generate_index_file", fake_generate)
    monkeypatch.setattr(
        api_docs_index, "load_index",
        lambda index_path=None: {"interface_count": 0, "member_count": 0},
    )
    monkeypatch.setattr(sys, "argv", ["api_docs_index.py"])

    assert api_docs_index.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pass"
    assert seen == {"output": None, "revision": None}


@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # runpy 复执行已导入模块时的固有提示
def test_module_main_entrypoint_runs_lookup(monkeypatch, capsys):
    """@brief ``python scripts/api_docs_index.py --lookup`` 入口只做离线查询。"""
    monkeypatch.setattr(sys, "argv", ["api_docs_index.py", "--lookup", "AddMate5", "--limit", "1"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("scripts.api_docs_index", run_name="__main__")

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list)


def test_fallback_import_path_without_package(monkeypatch):
    """@brief 直接以文件方式导入时, 相对导入失败须回退到顶级模块导入。"""
    module_path = Path(api_docs_index.__file__)
    monkeypatch.syspath_prepend(str(module_path.parent))
    spec = importlib.util.spec_from_file_location("_api_docs_index_fallback", module_path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_api_docs_index_fallback", module)
    spec.loader.exec_module(module)

    assert module.lookup("addmate5", index=SYNTHETIC_INDEX)
    assert module._find_sldworks_typelib.__module__ == "_api_docs_index_fallback"
