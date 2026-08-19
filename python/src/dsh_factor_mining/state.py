# coding=utf-8
"""User-owned mining state.

All state (trail, explored paths, search paths, registries, mining state)
lives under a user-supplied state root.  The package directory is never
written to and never contains user data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_STATE_ROOT = Path.cwd() / ".factor-mining"

FILES = {
    "trail": "trail.json",
    "explored": "explored_paths.json",
    "search_paths": "search_paths.json",
    "registry": "registry.json",
    "mining_state": "mining_state.json",
}

MINING_CONFIG = {
    "max_rounds": 50,
    "max_fail_streak": 10,
    "max_candidates": 3,
}


def resolve_state_root(root: str | os.PathLike | None = None) -> Path:
    if root:
        return Path(root)
    env = os.environ.get("DSH_FACTOR_MINER_STATE_ROOT")
    if env:
        return Path(env)
    return DEFAULT_STATE_ROOT


def _path(kind: str, root: str | os.PathLike | None = None) -> Path:
    root = resolve_state_root(root)
    root.mkdir(parents=True, exist_ok=True)
    return root / FILES[kind]


def _atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    """原子写 JSON（tmp + os.replace）：中途崩溃不留半截文件——半截 JSON 会让
    read 端兜底成空列表，等于整个文件静默丢失（有界池/轨迹不可这样丢）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json_list(kind: str, root: str | os.PathLike | None = None) -> list[Any]:
    path = _path(kind, root)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_json_entry(kind: str, entry: dict[str, Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    entries = read_json_list(kind, root)
    entries.append(entry)
    path = _path(kind, root)
    _atomic_write_json(path, entries)
    return {"kind": kind, "index": len(entries) - 1, "path": str(path)}


def write_json(kind: str, entries: list[Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    path = _path(kind, root)
    _atomic_write_json(path, entries)
    return {"kind": kind, "count": len(entries), "path": str(path)}


def append_trail(entry: dict[str, Any], root: str | os.PathLike | None = None):
    return append_json_entry("trail", entry, root)


def append_explored(entry: dict[str, Any], root: str | os.PathLike | None = None):
    return append_json_entry("explored", entry, root)


def append_search_path(entry: dict[str, Any], root: str | os.PathLike | None = None):
    return append_json_entry("search_paths", entry, root)


def read_registry(root: str | os.PathLike | None = None) -> list[Any]:
    return read_json_list("registry", root)


def write_registry(entries: list[Any], root: str | os.PathLike | None = None):
    return write_json("registry", entries, root)


def read_mining_state(root: str | os.PathLike | None = None) -> dict[str, Any]:
    path = _path("mining_state", root)
    default = dict(MINING_CONFIG, round=0, global_fail_streak=0, candidate_pool=[], finalized=False)
    if not path.exists():
        return default
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    for k, v in default.items():
        st.setdefault(k, v)
    return st


def reset_mining_state(root: str | os.PathLike | None = None) -> dict[str, Any]:
    default = dict(MINING_CONFIG, round=0, global_fail_streak=0, candidate_pool=[], finalized=False)
    path = _path("mining_state", root)
    _atomic_write_json(path, default)
    return default


def record_round(entry: dict[str, Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    st = read_mining_state(root)
    st["round"] = int(entry.get("round", st["round"] + 1))
    if entry.get("accepted"):
        st["global_fail_streak"] = 0
        st["candidate_pool"].append({
            "signal": entry.get("signal", ""),
            "ic_ir_train": entry.get("ic_ir_train"),
            "accepted_at_round": st["round"],
            "attribution": entry.get("attribution", ""),
        })
    else:
        st["global_fail_streak"] = int(st.get("global_fail_streak", 0)) + 1
    path = _path("mining_state", root)
    _atomic_write_json(path, st)
    return check_termination(st)


def check_termination(state: dict[str, Any] | None = None, root: str | os.PathLike | None = None) -> dict[str, Any]:
    st = state if state is not None else read_mining_state(root)
    round_n = int(st.get("round", 0))
    fail = int(st.get("global_fail_streak", 0))
    n_cand = len(st.get("candidate_pool", []))
    max_rounds = int(st.get("max_rounds", MINING_CONFIG["max_rounds"]))
    max_fail = int(st.get("max_fail_streak", MINING_CONFIG["max_fail_streak"]))
    max_cand = int(st.get("max_candidates", MINING_CONFIG["max_candidates"]))

    if st.get("finalized"):
        return {"stop": True, "reason": "已 finalize（test 已消费，这批结束）", "state": st}
    if round_n >= max_rounds:
        return {"stop": True, "reason": f"预算耗尽（round {round_n} >= {max_rounds}）", "state": st}
    if fail >= max_fail:
        return {"stop": True, "reason": f"全局连续失败（{fail} >= {max_fail}，跨方向收敛）", "state": st}
    if n_cand >= max_cand:
        return {"stop": True, "reason": f"候选池满（{n_cand} >= {max_cand}）", "state": st}
    return {"stop": False, "reason": f"继续（round {round_n}/{max_rounds}，失败 {fail}/{max_fail}，候选 {n_cand}/{max_cand}）", "state": st}
