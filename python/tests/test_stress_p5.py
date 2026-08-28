# coding=utf-8
"""P5 并发压力测试：多进程对同一 stateRoot 混合操作（trail + 池 + registry +
mining_state），限时内完成、账本条数 = 尝试数、全部 JSON 有效、无死锁。
"""
from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path


def _mixed_worker(root: str, worker_id: int, rounds: int) -> dict:
    import numpy as np

    from dsh_factor_mining.factor.memory_pool import MemoryPool
    from dsh_factor_mining.state import (
        append_json_entry,
        append_registry_entry,
    )

    rng = np.random.default_rng(worker_id)
    trail_wrote = pool_ok = reg_wrote = 0
    for r in range(rounds):
        # 1) trail 追加（读改写全文件——最易丢条目的路径）
        append_json_entry("trail", {"w": worker_id, "r": r}, root)
        trail_wrote += 1
        # 2) 池入池（锁内重读双池 + 指纹 npy 落盘）
        pool = MemoryPool(root)
        F = rng.normal(size=(60, 40))
        out = pool.offer_active(F, list(range(0, 60, 2)),
                                f"def factor(env):\n    pass  # stress {worker_id}-{r}\n",
                                name=f"stress_{worker_id}_{r}",
                                ic_ir=float(rng.normal(1.0, 0.1)))
        pool_ok += 1 if out.get("admitted") else 0
        # 3) registry 锁内读-追加-写（生产追加路径的公共助手）
        append_registry_entry({"name": f"s_{worker_id}_{r}",
                               "w": worker_id, "r": r}, root)
        reg_wrote += 1
    return {"trail": trail_wrote, "pool": pool_ok, "reg": reg_wrote}


def test_mixed_concurrent_stress_no_loss_no_deadlock(tmp_path):
    # 死锁保险丝在函数内取时——模块级常量会在全量套件的收集-执行间隔里失效
    terminate = time.monotonic() + 120
    root = str(tmp_path)
    n_procs, rounds = 3, 8
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_procs) as pool:
        async_res = pool.starmap_async(_mixed_worker,
                                       [(root, w, rounds) for w in range(n_procs)])
        res = async_res.get(timeout=110)  # 死锁保险丝
    assert time.monotonic() < terminate

    # 账本完整性：条数 = 尝试数（零丢失）
    trail = read_json_list_ = json.loads(
        (Path(root) / "trail.json").read_text(encoding="utf-8"))
    assert len(trail) == n_procs * rounds, f"trail 丢条目: {len(trail)}"
    keys = {(e["w"], e["r"]) for e in trail}
    assert len(keys) == n_procs * rounds

    # registry 完整性
    reg = json.loads((Path(root) / "registry.json").read_text(encoding="utf-8"))
    assert len(reg) == n_procs * rounds, f"registry 丢条目: {len(reg)}"

    # 池文件有效且条目对（容量 64 > 24 全入）
    pool_active = json.loads((Path(root) / "pool" / "active.json")
                             .read_text(encoding="utf-8"))
    assert isinstance(pool_active, list) and len(pool_active) == n_procs * rounds
    # 指纹文件与条目成对
    fp_dir = Path(root) / "pool" / "fp"
    assert all((fp_dir / f"{e['key']}.npy").exists() for e in pool_active)

    # 返回计数一致
    assert sum(r["trail"] for r in res) == n_procs * rounds
    assert sum(r["reg"] for r in res) == n_procs * rounds


def test_bridge_instances_registry(tmp_path):
    """多 bridge 共存登记（D5）：两个实例登记、死实例被清理。"""
    from dsh_factor_mining.bridge import _register_bridge_instance

    _register_bridge_instance(str(tmp_path))
    _register_bridge_instance(str(tmp_path))  # 幂等（同 pid 刷新）
    p = tmp_path / ".bridge-instances.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    import os

    assert str(os.getpid()) in data
    # 死 pid 清理：塞一个不存在的 pid 再登记
    data["99999999"] = "2026-01-01T00:00:00"
    p.write_text(json.dumps(data), encoding="utf-8")
    _register_bridge_instance(str(tmp_path))
    data2 = json.loads(p.read_text(encoding="utf-8"))
    assert "99999999" not in data2
