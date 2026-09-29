"""API 签名索引单元测试: 纯查询逻辑 (真机生成走 solidworks_api_index 回归)。"""
import json

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
