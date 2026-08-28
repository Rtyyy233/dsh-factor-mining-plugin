# coding=utf-8
"""策略状态目录与原子 IO（.strategy-lab/，与 .factor-mining/ 平级；
生产因子状态只读，零迁移——因子侧文件只 cross_check 读，从不写）。

沿因子层铁律：原子写（tmp + os.replace，中途崩溃不留半截 JSON）；
registry 改动前备份；账本唯一出水口（性能数字只从 evaluate CLI 响应
出，每次评估自动记账——计数不依赖自觉）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dsh_factor_mining.filelock import state_write_lock

DEFAULT_STATE_ROOT = Path.cwd() / ".strategy-lab"

FILES = {
    "trail": "strategy_trail.json",
    "registry": "strategy_registry.json",
    "test_lock": "test_lock.json",
}


def resolve_state_root(root=None) -> Path:
    if root:
        return Path(root)
    env = os.environ.get("DSH_STRATEGY_LAB_STATE_ROOT")
    if env:
        return Path(env)
    return DEFAULT_STATE_ROOT


def path_for(kind: str, root=None) -> Path:
    r = resolve_state_root(root)
    r.mkdir(parents=True, exist_ok=True)
    return r / FILES[kind]


def atomic_write_json(path: Path, payload, indent: int = 2) -> None:
    """原子写 JSON（tmp + os.replace）——半截文件会把 read 端兜底成
    空列表，等于整个账本静默丢失。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent),
                   encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json_list(path: Path) -> list:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:        # noqa: BLE001 — 兼容首次运行/损坏兜底为空
        return []


def read_trail(root=None) -> list:
    return read_json_list(path_for("trail", root))


def append_trail_entry(entry: dict, root=None) -> dict:
    """账本追加（trail.py 负责 dedup 语义；此处只做原子写）。
    写锁内读改写（并行 P0）：多进程并发 CLI 下防丢条目。"""
    p = path_for("trail", root)
    with state_write_lock(p.parent):
        entries = read_json_list(p)
        entries.append(entry)
        atomic_write_json(p, entries)
    return entry


def read_registry(root=None) -> list:
    return read_json_list(path_for("registry", root))


def write_registry_with_backup(entries: list, root=None) -> Path:
    """registry 事务写：改动前打时间戳备份（沿因子 registry 铁律——
    准入账本可追溯，误写可回滚）。写锁内备份+写（并行 P0）。"""
    p = path_for("registry", root)
    with state_write_lock(p.parent):
        if p.exists():
            bak = p.with_name(f"{FILES['registry']}.{time.strftime('%Y%m%dT%H%M%S')}.bak")
            bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        atomic_write_json(p, entries)
    return p


def append_registry_entry(entry: dict, root=None) -> list:
    """registry 锁内读-追加-写（并行 P5 压测补丁）：读在锁外的
    「read → append → write_registry_with_backup」有丢更新窗口——
    压力测试实测 3 进程 × 8 轮丢一半条目。追加一律走本助手。"""
    p = path_for("registry", root)
    with state_write_lock(p.parent):
        entries = read_json_list(p)
        entries.append(entry)
        if p.exists():
            bak = p.with_name(f"{FILES['registry']}.{time.strftime('%Y%m%dT%H%M%S')}.bak")
            bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
        atomic_write_json(p, entries)
    return entries


def check_factor_refs(factor_refs: list, factor_state_root=None) -> dict:
    """跨层封口（规划 §6）：factor_refs 必须全部在因子 registry 且
    accepted——策略消耗的 alpha 已过自己的门，因子试验不重复计价，
    各层自守 α。

    ref 形如 {"source_hash": ..., "horizon": ...}（账本互引键，规划
    §1）。因子侧只读。返回 {"ok": bool, "missing": [...],
    "not_accepted": [...]}。"""
    from dsh_factor_mining.state import read_registry as read_factor_registry

    if not factor_refs:
        return {"ok": False, "missing": [], "not_accepted": [],
                "note": "factor_refs 为空——策略必须从已准入因子构造"}
    try:
        freg = read_factor_registry(factor_state_root)
    except Exception as e:    # noqa: BLE001
        return {"ok": False, "missing": list(factor_refs),
                "not_accepted": [],
                "note": f"因子 registry 不可读（{type(e).__name__}）"}
    accepted = {e.get("source_hash") for e in freg
                if isinstance(e, dict) and e.get("accepted") is True}
    missing, not_accepted = [], []
    for ref in factor_refs:
        h = ref.get("source_hash") if isinstance(ref, dict) else None
        if h is None or h not in {e.get("source_hash") for e in freg
                                  if isinstance(e, dict)}:
            missing.append(ref)
        elif h not in accepted:
            not_accepted.append(ref)
    ok = not missing and not not_accepted
    out = {"ok": ok, "missing": missing, "not_accepted": not_accepted}
    if not ok:
        out["note"] = ("factor_refs 必须全部在因子 registry 且 accepted——"
                       "策略只消费已准入 alpha（跨层多重检验封口）")
    return out


def consume_test_lock(fingerprint_key: str | None, root=None) -> dict:
    """test 区一次性消费（尾线 WS3 同款纪律）：test 是最终消耗品，
    永不进 deflation；已消费再评估 = fail-closed。
    写锁内 check-and-set（并行 P0）：worker 评估期间（调用前）另一进程
    无法同时通过检查——消费点本身原子。"""
    import dsh_strategy_lab as _sl

    p = path_for("test_lock", root)
    with state_write_lock(p.parent):
        if p.exists():
            try:
                lock = json.loads(p.read_text(encoding="utf-8"))
            except Exception:    # noqa: BLE001
                lock = {}
            if lock.get("consumed"):
                raise RuntimeError(
                    "test 已被消费（test_lock.json）。test 是最终消耗品，"
                    "禁止反复评估调参。")
        lock_payload = {
            "consumed": True,
            "consumed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "fingerprint": fingerprint_key,
            "engine_version": _sl.__version__,
            "note": "test 已消费一次，禁止再次评估；永不进 deflation",
        }
        atomic_write_json(p, lock_payload)
    return lock_payload


def check_test_lock(root=None) -> dict:
    """只读查询（status 用）：是否已消费 + 上下文。"""
    p = path_for("test_lock", root)
    if not p.exists():
        return {"consumed": False}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:        # noqa: BLE001
        return {"consumed": False, "note": "test_lock 损坏（视为未消费）"}
