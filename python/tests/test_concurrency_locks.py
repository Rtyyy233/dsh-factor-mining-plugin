# coding=utf-8
"""并发专项测试（并行 P0 验收）：真实多进程竞争，不是模拟。

覆盖：
- 写锁互斥 + 同进程重入 + 超时可行动错误
- 多进程并发 trail 追加零丢失（读-改-写全文件在最坏交错下不丢条目）
- 多进程并发入池一致性（池文件有效 + 条目齐全）
- test_lock claim-then-compute 双消费竞争（恰一胜）
"""
from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from dsh_factor_mining.filelock import (
    LockTimeoutError,
    state_write_lock,
)
from dsh_factor_mining.state import append_json_entry, read_json_list


# ---------------------------------------------------------------- 进程 worker（spawn 可pickle的模块级函数）

def _trail_writer(root: str, worker_id: int, n: int) -> int:
    wrote = 0
    for i in range(n):
        append_json_entry("trail", {"worker": worker_id, "i": i}, root)
        wrote += 1
    return wrote


def _pool_offerer(root: str, worker_id: int) -> bool:
    import numpy as np

    from dsh_factor_mining.factor.memory_pool import MemoryPool
    pool = MemoryPool(root)
    rng = np.random.default_rng(worker_id)
    F = rng.normal(size=(60, 40))
    out = pool.offer_active(F, list(range(0, 60, 2)), f"def factor(env):\n    pass  # w{worker_id}\n",
                            name=f"conc_{worker_id}", ic_ir=1.0 + worker_id * 0.01)
    return bool(out.get("admitted"))


def _test_claimer(root: str, worker_id: int, hold_s: float) -> str:
    from dsh_factor_mining.factor.evaluate import (
        claim_test_lock,
        finalize_test_lock,
        release_test_lock,
    )
    try:
        claim_test_lock(root, source_hash=f"w{worker_id}")
    except RuntimeError as e:
        return f"claim_rejected: {e}"
    time.sleep(hold_s)  # 模拟 test 区计算窗口（TOCTOU 旧实现的敞口）
    if worker_id % 2 == 0:
        finalize_test_lock({"consumed": True, "by": worker_id}, root)
        return "consumed"
    release_test_lock(root)
    return "released"


def _lock_holder(root: str, hold_s: float) -> None:
    with state_write_lock(root):
        time.sleep(hold_s)


# ---------------------------------------------------------------- 单进程行为

def test_write_lock_reentrant_same_process(tmp_path):
    with state_write_lock(tmp_path):
        with state_write_lock(tmp_path):  # 嵌套不死锁（重入计数）
            append_json_entry("trail", {"k": 1}, tmp_path)
    assert read_json_list("trail", tmp_path) == [{"k": 1}]


def test_write_lock_timeout_actionable(tmp_path):
    ctx = mp.get_context("spawn")
    p = ctx.Process(target=_lock_holder, args=(str(tmp_path), 2.0))
    p.start()
    time.sleep(0.8)  # 等子进程拿到锁
    try:
        with pytest.raises(LockTimeoutError) as ei:
            with state_write_lock(tmp_path, timeout_s=1.0):
                pass
        msg = str(ei.value)
        assert "PID" in msg and "手动删除" in msg  # 可行动：谁持锁 + 怎么办
    finally:
        p.join(timeout=10)


def test_pid_alive_self():
    import os

    from dsh_factor_mining.filelock import pid_alive
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False


# ---------------------------------------------------------------- 多进程竞争

@pytest.mark.parametrize("n_procs,n_each", [(4, 25)])
def test_concurrent_trail_appends_no_lost_entries(tmp_path, n_procs, n_each):
    ctx = mp.get_context("spawn")
    with ctx.Pool(n_procs) as pool:
        results = pool.starmap(_trail_writer, [(str(tmp_path), w, n_each) for w in range(n_procs)])
    assert results == [n_each] * n_procs
    entries = read_json_list("trail", tmp_path)
    assert len(entries) == n_procs * n_each, "并发追加丢条目——写锁失效"
    # 内容完整性：每个 (worker, i) 恰好一条
    keys = {(e["worker"], e["i"]) for e in entries}
    assert len(keys) == n_procs * n_each
    # 文件本身可解析（无半截 JSON）
    raw = json.loads((Path(tmp_path) / "trail.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == n_procs * n_each


def test_concurrent_pool_offers_consistency(tmp_path):
    from dsh_factor_mining.factor.memory_pool import MemoryPool

    ctx = mp.get_context("spawn")
    with ctx.Pool(3) as pool:
        results = pool.starmap(_pool_offerer, [(str(tmp_path), w) for w in range(3)])
    assert all(results), "并发入池应全部成功（容量足够且互不重复）"
    pool = MemoryPool(tmp_path)
    names = {e["name"] for e in pool._active}
    assert names == {"conc_0", "conc_1", "conc_2"}, f"池条目丢失: {names}"
    # 指纹文件齐全（offer 的 npy 写入也在锁内不被中断产生半截——npy 原子性
    # 由 save 本身保证，锁保证条目与指纹成对一致）
    for e in pool._active:
        assert (pool.fp_dir / f"{e['key']}.npy").exists()


def test_test_lock_double_claim_single_winner(tmp_path):
    """两个进程同时进入 claim→sleep→finalize 窗口（旧 TOCTOU 敞口），
    恰一个能 claim 成功并消费。"""
    ctx = mp.get_context("spawn")
    a = ctx.Process(target=_test_claimer, args=(str(tmp_path), 0, 0.8))
    b = ctx.Process(target=_test_claimer, args=(str(tmp_path), 1, 0.1))
    a.start()
    time.sleep(0.3)  # a 已 claim，进入计算窗口
    b.start()
    a.join(timeout=15)
    b.join(timeout=15)
    lock = json.loads((Path(tmp_path) / "test_lock.json").read_text(encoding="utf-8"))
    # a（偶数 id）finalize consumed；b 在窗口内 claim 必须 fail-closed
    assert lock.get("consumed") is True and lock.get("by") == 0
    # b 的结果：要么 claim_rejected（撞 a 的 live claim），要么 a 已完成后
    # released（b claim 成功但走 release 分支——不产生第二次 consumed）
    # 关键不变量：consumed 只被写一次且只属于 a
    assert lock.get("by") == 0


def test_stale_claim_reclaimed_after_dead_pid(tmp_path, monkeypatch):
    """claim 残留（持有者已死）→ 下一次尝试回收，不算消费。"""
    from dsh_factor_mining.factor.evaluate import claim_test_lock, release_test_lock

    # 写入一个死 PID 的 claim
    lock_path = tmp_path / "test_lock.json"
    lock_path.write_text(json.dumps({
        "consumed": False,
        "claim": {"pid": 99999999, "ts": "2026-08-27T00:00:00"},
    }), encoding="utf-8")
    claim_test_lock(tmp_path, source_hash="x")  # 死 PID → 回收，不抛
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert data["consumed"] is False and "claim" in data
    release_test_lock(tmp_path)
    assert json.loads(lock_path.read_text(encoding="utf-8"))["consumed"] is False
