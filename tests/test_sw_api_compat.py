"""运行时自适应与破坏性保护单元测试 (伪对象, 无需 SolidWorks)。"""
import hashlib
import json
from pathlib import Path

import pytest

import scripts.sw_api_compat as sw_api_compat
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


# --- default_cache_path 与缓存容错 ---


def test_default_cache_path_prefers_env_override(monkeypatch, tmp_path):
    """@brief 环境变量覆盖优先于默认目录。"""
    monkeypatch.setenv(sw_api_compat.CACHE_ENV_VAR, str(tmp_path / "override.json"))
    assert sw_api_compat.default_cache_path() == Path(tmp_path) / "override.json"


def test_default_cache_path_uses_localappdata(monkeypatch, tmp_path):
    """@brief 无覆盖时使用 LOCALAPPDATA。"""
    monkeypatch.delenv(sw_api_compat.CACHE_ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert sw_api_compat.default_cache_path() == tmp_path / "CADStudio" / "api_compat.json"


def test_default_cache_path_falls_back_to_home(monkeypatch):
    """@brief LOCALAPPDATA 缺失时退回用户主目录。"""
    monkeypatch.delenv(sw_api_compat.CACHE_ENV_VAR, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    expected = Path.home() / "AppData" / "Local" / "CADStudio" / "api_compat.json"
    assert sw_api_compat.default_cache_path() == expected


def test_save_cache_failure_is_silent(tmp_path):
    """@brief 缓存目录被普通文件占位时写入失败但不抛异常。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("occupied", encoding="utf-8")
    sw_api_compat._save_cache(blocker / "nested" / "api_compat.json", {"k": "v"})
    assert blocker.read_text(encoding="utf-8") == "occupied"


def test_resolve_variant_survives_unwritable_cache(tmp_path):
    """@brief 缓存写失败不影响变体解析结果。"""
    host = _VariantHost()
    blocker = tmp_path / "blocker"
    blocker.write_text("occupied", encoding="utf-8")
    name, result = resolve_variant(
        host,
        {"v2": lambda: host.v2()},
        cache_key="x",
        cache_path=blocker / "nested" / "api_compat.json",
    )
    assert name == "v2"
    assert result == "ok-from-v2"


def test_revision_probe_failure_keeps_unknown_revision(tmp_path):
    """@brief 版本号读取失败时按 unknown 桶缓存。"""

    class _BrokenApp:
        @property
        def RevisionNumber(self):
            raise RuntimeError("COM 会话已断开")

    host = _VariantHost()
    cache = tmp_path / "api_compat.json"
    name, result = resolve_variant(
        host,
        {"v2": lambda: host.v2()},
        cache_key="part.revolve",
        sw_app=_BrokenApp(),
        cache_path=cache,
    )
    assert name == "v2" and result == "ok-from-v2"
    cache_data = json.loads(cache.read_text(encoding="utf-8"))
    assert cache_data["unknown"]["part.revolve"] == "v2"


def test_variant_raising_generic_error_is_reported(tmp_path):
    """@brief COM 层以外的异常同样记入失败汇总。"""

    class _GenericFailHost:
        def m1(self):
            raise ValueError("0x80010108 对象已断开")

        def m2(self):
            raise ValueError("0x80010108 对象已断开")

    host = _GenericFailHost()

    with pytest.raises(RuntimeError) as excinfo:
        resolve_variant(
            host,
            {"m1": lambda: host.m1(), "m2": lambda: host.m2()},
            cache_key="k",
            cache_path=tmp_path / "c.json",
        )
    message = str(excinfo.value)
    assert "ValueError" in message and "m1" in message and "m2" in message


def test_stale_cache_entry_falls_back_to_full_scan(tmp_path):
    """@brief 缓存指向的变体失效时全量扫描并回写缓存。"""
    host = _VariantHost()
    cache = tmp_path / "api_compat.json"
    cache.write_text(json.dumps({"32.5.0": {"part.extrude": "v1"}}), encoding="utf-8")
    app = FakeSwApp(new_fake_session()[1], "32.5.0")
    name, result = resolve_variant(
        host,
        {"v1": lambda: host.v1(), "v2": lambda: host.v2()},
        cache_key="part.extrude",
        sw_app=app,
        cache_path=cache,
    )
    assert name == "v2"
    assert result == "ok-from-v2"
    assert host.calls == ["v1", "v1", "v2"]  # 缓存命中失败一次 + 全量扫描重试一次
    cache_data = json.loads(cache.read_text(encoding="utf-8"))
    assert cache_data["32.5.0"]["part.extrude"] == "v2"
