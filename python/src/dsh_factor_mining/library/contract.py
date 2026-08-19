# coding=utf-8
"""User factor library contracts.

The plugin ships no factors.  A user library may be:
- python_module: importable module exposing list_known_factors / get_known_factor / query_known_factors
- json_registry: JSON array of {name, description, formula, ic_ir?}
- expression_list: JSON array of expression strings

The bridge asks the user for one of these in data config; absence is a legal
"empty library" state.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


class LibraryError(ValueError):
    pass


def load_python_module(path_or_name: str):
    """Load a user module by absolute path or import name."""
    p = Path(path_or_name)
    if p.exists():
        spec = importlib.util.spec_from_file_location("dsh_factor_mining_user_library", p)
        if spec is None or spec.loader is None:
            raise LibraryError(f"无法解析用户因子库模块: {path_or_name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    try:
        return importlib.import_module(path_or_name)
    except Exception as e:
        raise LibraryError(f"无法导入用户因子库 {path_or_name}: {e}") from e


def load_registry_json(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        raise LibraryError(f"因子注册表不存在: {path}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise LibraryError(f"因子注册表必须是 JSON 数组: {path}")
    return data


def load_expression_list(path: str) -> list[dict[str, Any]]:
    raw = load_registry_json(path)
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"name": item, "formula": item, "description": ""})
        elif isinstance(item, dict) and item.get("formula"):
            out.append(item)
        else:
            raise LibraryError(f"表达式清单条目无效: {item!r}")
    return out


def query_entries(entries: list[dict[str, Any]], query: str, top_k: int = 5) -> list[dict[str, Any]]:
    text = query.lower()
    scored = []
    for e in entries:
        hits = 0
        hay = " ".join(str(v) for v in e.values()).lower()
        for token in text.split():
            if token and token in hay:
                hits += 1
        name = str(e.get("name", "")).lower()
        if name and name in text:
            hits += 2
        if hits > 0:
            scored.append((hits, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:top_k]]


class UserLibrary:
    """Unified read-only view over a user factor library."""

    @staticmethod
    def _normalize_entries(raw: Any) -> list[dict[str, Any]]:
        """把用户库的 list_* 返回值规范成 dict 列表。

        python_module 契约只说「返回因子清单」，有的实现返回 list[str]（名字列表，
        如 known_factors.list_known_factors）——直接当 entries 用会让
        query_entries 的 e.values() 对 str 崩（-32603 实测事故）。
        str -> {"name": s}；dict 原样保留；其余丢弃。
        """
        out: list[dict[str, Any]] = []
        for item in (raw or []):
            if isinstance(item, str):
                out.append({"name": item, "description": ""})
            elif isinstance(item, dict):
                out.append(item)
        return out

    def __init__(self, spec: dict[str, Any] | None):
        self.spec = spec or {}
        self.entries: list[dict[str, Any]] = []
        self._module = None
        self._configured = bool(spec)
        if not self._configured:
            return
        kind = self.spec.get("type", self.spec.get("kind"))
        if kind == "python_module":
            self._module = load_python_module(self.spec.get("path") or self.spec.get("module") or "")
            self.entries = self._normalize_entries(self._module.list_known_factors())
        elif kind == "json_registry":
            self.entries = self._normalize_entries(load_registry_json(self.spec.get("path") or ""))
        elif kind == "expression_list":
            self.entries = load_expression_list(self.spec.get("path") or "")
        else:
            raise LibraryError(f"未知因子库类型: {kind}（支持 python_module/json_registry/expression_list）")

    @property
    def configured(self):
        return self._configured

    def list(self) -> list[dict[str, Any]]:
        return self.entries

    def query(self, text: str, top_k: int = 5) -> dict[str, Any]:
        if not self._configured:
            return {"configured": False, "hits": [], "note": "library_not_configured"}
        hits = query_entries(self.entries, text, top_k)
        if self._module is not None and hasattr(self._module, "query_known_factors"):
            try:
                module_hits = self._normalize_entries(self._module.query_known_factors(text))
                if module_hits:
                    hits = module_hits[:top_k]
            except Exception:
                pass
        return {"configured": True, "hits": hits}

    def get(self, name: str):
        if self._module is not None and hasattr(self._module, "get_known_factor"):
            return self._module.get_known_factor(name)
        for e in self.entries:
            if e.get("name") == name:
                return e
        raise LibraryError(f"因子库中不存在: {name}")
