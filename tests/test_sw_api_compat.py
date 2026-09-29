"""运行时自适应与破坏性保护单元测试 (伪对象, 无需 SolidWorks)。"""
import hashlib
import json

import pytest

from scripts.sw_api_compat import resolve_variant
from scripts.sw_connect import save_document

from fakes.sw import FakeModelDoc, FakeSwApp, new_fake_session


class _VariantHost:
    def __init__(self):
        self.calls = []

    def v1(self, *args):
        self.calls.append("v1")
        raise TypeError("模拟 SW2025 的 v1 兼容问题")

    def v2(self, *args):
        self.calls.append("v2")
        return "ok-from-v2"


def test_resolve_variant_falls_back_and_caches(tmp_path):
    host = _VariantHost()
    cache = tmp_path / "api_compat.json"
    name, result = resolve_variant(
        host,
        {"v1": lambda: host.v1(), "v2": lambda: host.v2()},
        cache_key="part.extrude",
        cache_path=cache,
    )
    assert name == "v2"
    assert result == "ok-from-v2"
    cache_data = json.loads(cache.read_text(encoding="utf-8"))
    assert cache_data["unknown"]["part.extrude"] == "v2"


def test_resolve_variant_uses_cache_first(tmp_path):
    host = _VariantHost()
    cache = tmp_path / "api_compat.json"
    cache.write_text(json.dumps({"32.5.0": {"part.extrude": "v2"}}), encoding="utf-8")
    app = FakeSwApp(new_fake_session()[1], "32.5.0")
    name, result = resolve_variant(
        host,
        {"v1": lambda: host.v1(), "v2": lambda: host.v2()},
        cache_key="part.extrude",
        sw_app=app,
        cache_path=cache,
    )
    assert name == "v2"
    assert host.calls == ["v2"]  # 缓存命中, 未尝试 v1


def test_resolve_variant_all_fail_raises(tmp_path):
    host = _VariantHost()

    def _bad():
        raise TypeError("bad")

    with pytest.raises(RuntimeError) as excinfo:
        resolve_variant(host, {"m1": _bad, "m2": _bad}, cache_key="x", cache_path=tmp_path / "c.json")
    assert "m1" in str(excinfo.value) and "m2" in str(excinfo.value)


def test_resolve_variant_missing_method_skipped(tmp_path):
    host = _VariantHost()
    name, result = resolve_variant(
        host,
        {"missing": lambda: 1 / 0, "v2": lambda: host.v2()},
        cache_key="x",
        cache_path=tmp_path / "c.json",
    )
    assert name == "v2"


# --- save_document 覆盖保护与自动备份 ---


def _fresh_model():
    app, _journal = new_fake_session()
    return app.NewDocument("C:/fake/templates/gb_part.prtdot", 0, 0, 0)


def test_save_to_fresh_path_allowed(tmp_path):
    model = _fresh_model()
    target = tmp_path / "new.SLDPRT"
    assert save_document(model, str(target)) is True
    assert target.is_file()


def test_save_over_existing_without_overwrite_rejected(tmp_path):
    model = _fresh_model()
    target = tmp_path / "existing.SLDPRT"
    target.write_bytes(b"OLD")
    with pytest.raises(FileExistsError) as excinfo:
        save_document(model, str(target))
    assert "SW_SAVE_TARGET_EXISTS" in str(excinfo.value)
    assert target.read_bytes() == b"OLD"  # 原文件未被破坏


def test_same_path_resave_exempt_from_overwrite_gate(tmp_path):
    model = _fresh_model()
    target = tmp_path / "resave.SLDPRT"
    assert save_document(model, str(target)) is True
    model.GetPathName = str(target)
    assert save_document(model, str(target)) is True  # 原地重存同一路径不需要 overwrite


def test_overwrite_creates_hash_manifest_backup(tmp_path):
    model = _fresh_model()
    target = tmp_path / "doc.SLDPRT"
    old_content = b"OLD-DOCUMENT-BYTES"
    target.write_bytes(old_content)
    assert save_document(model, str(target), overwrite=True, backup=True) is True
    backup_dir = tmp_path / ".cadstudio_backups"
    backups = list(backup_dir.glob("doc.*.SLDPRT"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == old_content
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["sha256"] == hashlib.sha256(old_content).hexdigest()
    assert manifest[0]["original"] == str(target)


def test_overwrite_without_backup_skips_backup_dir(tmp_path):
    model = _fresh_model()
    target = tmp_path / "nobackup.SLDPRT"
    target.write_bytes(b"OLD")
    assert save_document(model, str(target), overwrite=True, backup=False) is True
    assert not (tmp_path / ".cadstudio_backups").exists()
