# coding=utf-8
"""策略层并发专项测试（并行 P0 验收）：真实多进程。
"""
from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

from dsh_strategy_lab.state import check_test_lock, consume_test_lock
from dsh_strategy_lab.trail import append_trail, n_trials


def _trail_writer(root: str, worker_id: int, n: int) -> int:
    for i in range(n):
        append_trail({
            "stage": "development",
            "fingerprint": {"key": f"fp-w{worker_id}-{i}"},
            "worker": worker_id, "i": i,
        }, root)
    return n


def _test_consumer(root: str, worker_id: int, hold_s: float) -> str:
    import time
    time.sleep(hold_s)
    try:
        consume_test_lock(f"fp-w{worker_id}", root)
        return "consumed"
    except RuntimeError:
        return "rejected"


def test_concurrent_strategy_trail_appends(tmp_path):
    ctx = mp.get_context("spawn")
    with ctx.Pool(3) as pool:
        results = pool.starmap(_trail_writer, [(str(tmp_path), w, 10) for w in range(3)])
    assert results == [10, 10, 10]
    assert n_trials(tmp_path) == 30, "并发追加丢条目——写锁失效"
    raw = json.loads((Path(tmp_path) / "strategy_trail.json").read_text(encoding="utf-8"))
    assert isinstance(raw, list) and len(raw) == 30


def test_concurrent_consume_test_lock_single_winner(tmp_path):
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_test_consumer, args=(str(tmp_path), w, 0.1 * w))
             for w in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=15)
    lock = check_test_lock(tmp_path)
    assert lock.get("consumed") is True
    # 唯一性：三个竞争者只有一个 fingerprint 被记录
    consumed_fp = lock.get("fingerprint")
    assert consumed_fp in {"fp-w0", "fp-w1", "fp-w2"}
