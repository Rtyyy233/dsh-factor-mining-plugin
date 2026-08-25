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
    # max_candidates 2026-08-24 降级为信息性计数：候选池满不再是停点
    # （session.jsonl 事故：导入的 stateRoot 自带 8 入册因子，池满条件
    # 从第一刻为真，may_stop 永久打开，agent 33 turn 全停等手动 push）。
    "max_candidates": 3,
    # 机械停点（2026-08-24 用户决策；当晚二次修正：试验上限按「当前簇」
    # 计，不按终身计——trail 只增不减，终身上限=一次到顶永久停机）。
    # 簇 = 尾部同族链（|ρ|≥0.6 连续尾块），换方向即断链重置。
    "max_cluster_trials": 200,  # 当前方向连续同族试验上限（失控 backstop）
    "ic_conv_window": 60,       # IC_IR 收敛窗口（滑窗对滑窗，见 _ic_convergence）
    "ic_conv_delta": 0.05,      # 最近窗口最佳 |IC_IR| 超前一窗口最佳的幅度
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


def arc_rounds_bump(root: str | os.PathLike | None = None,
                    reset: bool = False) -> int:
    """方向段（arc）轮次计数（2026-08-25 arc 化：轮次上限从终身改为方向段相对）。

    - reset=False：+1（agent 写一条叙事 trail = 一轮，bridge._paths_append）
    - reset=True ：归 0（家族链断裂 = 机械换向事件，bridge._append_engine_trail_batch
      检测到新试验不接当前尾部链时调用——与 cluster_trials 断链重置同一事件）
    旧 mining_state 无 arc_rounds 字段 → 读作 0（部署即解锁触顶会话）。
    遗留 st["round"]（record_round 维护）仅信息性，不参与停点。"""
    st = read_mining_state(root)
    st["arc_rounds"] = 0 if reset else int(st.get("arc_rounds", 0)) + 1
    _atomic_write_json(_path("mining_state", root), st)
    return int(st["arc_rounds"])


def check_termination(state: dict[str, Any] | None = None, root: str | os.PathLike | None = None) -> dict[str, Any]:
    """合法停点只认机械判据（2026-08-24 用户决策；当晚二次修正：试验上限
    按「当前簇」计；2026-08-25 arc 化：轮次上限按「当前方向段」计——
    终身计数在 trail 只增不减的现实下 = 一次到顶永久停机）：

    - finalized（test 一次性锁的自然终态）→ kind=finalize
    - arc_rounds >= max_rounds（当前方向段叙事轮次上限）→ kind=direction_budget
    - cluster_trials >= max_cluster_trials（当前方向连续同族试验上限，
      state 需带 cluster_trials 键——由 bridge 侧用 family_streak 数学
      注入；换方向即断链重置）→ kind=direction_budget
    - fail >= max_fail_streak（跨方向全局收敛；当前无喂入方，恒 0 不触发）
      → kind=fail_streak

    kind 语义（注入器分流依据）：direction_budget = 方向预算耗尽，
    换向断链自动解除——引擎照挂 rotate/literature 策略、注入器照常推进；
    finalize / fail_streak = 真终态，注入器静默交还用户。
    候选池满不是停点（12/12 假穷尽实证）。终身 n_trials 不做停点——
    只用于 deflation 多重检验计价（统计上必须全量计数）。
    IC_IR 改善收敛是机械停点但在 bridge 侧计算（滑窗对滑窗，
    见 bridge._ic_convergence；kind=convergence，全局平台=换向救不了，
    静默终态），不经本函数。"""
    st = state if state is not None else read_mining_state(root)
    arc = int(st.get("arc_rounds", 0))
    fail = int(st.get("global_fail_streak", 0))
    cluster = int(st.get("cluster_trials", 0))
    n_trials = int(st.get("n_trials", 0))
    n_cand = len(st.get("candidate_pool", []))
    max_rounds = int(st.get("max_rounds", MINING_CONFIG["max_rounds"]))
    max_fail = int(st.get("max_fail_streak", MINING_CONFIG["max_fail_streak"]))
    max_cluster = int(st.get("max_cluster_trials",
                             MINING_CONFIG["max_cluster_trials"]))

    if st.get("finalized"):
        return {"stop": True, "kind": "finalize",
                "reason": "已 finalize（test 已消费，这批结束）", "state": st}
    if arc >= max_rounds:
        return {"stop": True, "kind": "direction_budget",
                "reason": (f"方向段轮次上限（arc_rounds {arc} >= {max_rounds}，"
                           "换向断链自动重置）"), "state": st}
    if cluster >= max_cluster:
        return {"stop": True, "kind": "direction_budget",
                "reason": (f"簇试验上限（当前方向连续 {cluster} >= "
                           f"{max_cluster}，换方向即断链重置）"), "state": st}
    if fail >= max_fail:
        return {"stop": True, "kind": "fail_streak",
                "reason": f"全局连续失败（{fail} >= {max_fail}，跨方向收敛）", "state": st}
    return {"stop": False, "kind": None,
            "reason": (f"继续（方向段轮次 {arc}/{max_rounds}，簇试验 {cluster}/{max_cluster}"
                       f"（终身 {n_trials} 只计价不停），失败 {fail}/{max_fail}，"
                       f"候选 {n_cand} 不计停）"),
            "state": st}
