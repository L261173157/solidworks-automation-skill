"""运行时 API 自适应: 按版本缓存的方法变体解析。

背景: SolidWorks 各版本的类型库签名存在差异 (如 SW2025 的
FeatureExtrusion3 兼容问题), 手写回退链分散在各调用点且无缓存。本模块提供:

- ``resolve_variant``: 按顺序尝试候选方法变体 (名字可解析 + 适配器调用成功),
  把胜出的名字按 (SolidWorks 版本, 调用点) 缓存到
  ``%LOCALAPPDATA%/CADStudio/api_compat.json``, 后续调用优先走缓存;
- 失败语义透明: 全部变体失败时抛 RuntimeError, 汇总每个变体的错误。

缓存是尽力而为的加速手段, 损坏/缺失自动退化为全量扫描。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

CACHE_ENV_VAR = "CADSTUDIO_API_COMPAT_CACHE"


def default_cache_path() -> Path:
    override = os.environ.get(CACHE_ENV_VAR)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CADStudio" / "api_compat.json"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 缓存损坏/缺失视为空
        return {}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp"
        )
        try:
            json.dump(cache, handle, ensure_ascii=False, indent=1)
        finally:
            handle.close()
        os.replace(handle.name, path)
    except Exception:  # noqa: BLE001 - 缓存写失败不影响调用
        pass


def resolve_variant(
    obj: Any,
    variants: dict[str, Callable[[], Any]],
    *,
    cache_key: str,
    sw_app: Any = None,
    cache_path: Path | None = None,
) -> tuple[str, Any]:
    """解析并调用方法变体, 返回 (胜出方法名, 结果)。

    variants: {方法名: 适配器 (无参 callable, 内部完成实际调用)}。
    适配器抛 AttributeError/TypeError/com_error 视为该变体不可用, 继续下一个。
    """
    errors: list[str] = []
    target = cache_path or default_cache_path()
    revision = "unknown"
    try:
        if sw_app is not None:
            revision = str(sw_app.RevisionNumber)
    except Exception:  # noqa: BLE001
        pass

    cache = _load_cache(target)
    cached_name = (cache.get(revision) or {}).get(cache_key)

    def _attempt(name: str) -> Any:
        adapter = variants[name]
        try:
            return adapter()
        except (AttributeError, TypeError, OSError) as exc:
            errors.append(f"{name}: {exc.__class__.__name__}: {str(exc)[:120]}")
            raise
        except Exception as exc:  # com_error 等 COM 层错误同样视为变体失败
            errors.append(f"{name}: {exc.__class__.__name__}: {str(exc)[:120]}")
            raise

    if cached_name and cached_name in variants:
        try:
            return cached_name, _attempt(cached_name)
        except Exception:  # noqa: BLE001 - 缓存失真, 落回全量扫描
            pass

    for name in variants:
        if not hasattr(obj, name):
            errors.append(f"{name}: 方法不可解析")
            continue
        try:
            result = _attempt(name)
        except Exception:  # noqa: BLE001 - 继续下一个变体
            continue
        bucket = cache.setdefault(revision, {})
        bucket[cache_key] = name
        _save_cache(target, cache)
        return name, result

    raise RuntimeError(
        f"API 变体全部失败 (cache_key={cache_key}, revision={revision}): " + "; ".join(errors)
    )
