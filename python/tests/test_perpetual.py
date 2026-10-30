# coding=utf-8
"""永续验收性质测试（2026-10-02 总任务书 A.3）。

公理：除死线与用户指令外无任何终态机会。锁死三件：
1. check_termination 任何输入下永不返回 finalize/fail_streak（枚举边界值）
2. loop.state 值域 ⊆ {running, must_rotate}（may_stop 退役）
3. _pick_strategy 永不返回 None（注入器永远有指令可推）
"""
from __future__ import annotations

import itertools
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, _pick_strategy  # noqa: E402
from dsh_factor_mining.state import check_termination  # noqa: E402


def _make_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 600, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


# ---- 1. check_termination 永续性质：枚举边界值 ----

def test_check_termination_never_silent_terminal():
    """finalize/fail_streak 字面量、max_fail_streak 边界、组合输入——
    kind 永不落在 {finalize, fail_streak}。"""
    arc_vals = [0, 49, 50, 51, 10**6]
    fail_vals = [0, 1, 9, 10, 11, 10**6]     # max_fail_streak=10 边界两侧
    cluster_vals = [0, 199, 200, 201, 10**6]  # max_cluster_trials=200 边界两侧
    fin_vals = [False, True]
    for arc, fail, cluster, fin in itertools.product(
            arc_vals, fail_vals, cluster_vals, fin_vals):
        term = check_termination({
            "arc_rounds": arc, "global_fail_streak": fail,
            "cluster_trials": cluster, "n_trials": 9999,
            "finalized": fin, "candidate_pool": [{}] * 8,
        })
        assert term["kind"] not in ("finalize", "fail_streak"), (
            arc, fail, cluster, fin, term)
        if term["stop"]:
            assert term["kind"] == "direction_budget", term


def test_check_termination_finalized_is_informational():
    """finalized=True → 不停（工具级 test 锁在别处；状态页告警由 loop 出）。"""
    term = check_termination({"finalized": True, "arc_rounds": 0,
                              "cluster_trials": 0, "global_fail_streak": 0})
    assert term["stop"] is False, term
    assert "test 已烧" in term["reason"], term["reason"]


def test_check_termination_fail_streak_is_informational():
    """global_fail_streak 超任意值 → 不停（max_fail_streak 停点语义已删，
    record 保留为信息字段）。"""
    term = check_termination({"global_fail_streak": 10**6, "arc_rounds": 0,
                              "cluster_trials": 0})
    assert term["stop"] is False, term
    assert "信息性" in term["reason"], term["reason"]


# ---- 2. loop.state 值域钉死 ----

def test_loop_state_domain(tmp_path):
    """finalized=True + 满池 + arc 触顶等组合输入下，loop.state ∈
    {running, must_rotate}；test 烧毁走状态页告警（loop.test_burned），
    不走停点。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "test_lock.json").write_text(json.dumps(
        {"consumed": True}), encoding="utf-8")
    (state / "mining_state.json").write_text(json.dumps({
        "finalized": True, "max_rounds": 1, "arc_rounds": 5,
        "max_cluster_trials": 1,
        "candidate_pool": [{}] * 8, "global_fail_streak": 99,
        "fam_conv_window": 0}), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["state"] in ("running", "must_rotate"), loop["state"]
    assert loop["test_burned"] is not None, "test 烧毁必须走状态页告警"
    assert "数据集轮换" in loop["test_burned"], loop["test_burned"]
    # 永续：strategy 永不 None（must_rotate 下强制 rotate/literature）
    assert loop["strategy"] is not None, loop


def test_loop_strategy_never_none(tmp_path):
    """冷启动（空账本）loop 也有 strategy——注入器永远有指令可推。"""
    b = _make_bridge(tmp_path)
    loop = b._loop_directive()
    assert loop["strategy"] is not None, loop
    assert loop["state"] == "running", loop


# ---- 3. _pick_strategy 永不 None（性质） ----

def test_pick_strategy_never_none():
    base = dict(streak=0, pending_rejected=None, frozen=False, plateau=False,
                pass_unadmitted=0, accepted_n=0, agent_rounds=0, n_trials=0)
    for kind in ("finalize", "fail_streak", "direction_budget", "convergence",
                 None, "whatever"):
        assert _pick_strategy(**base, stop_kind=kind) is not None, kind


def test_test_burned_claim_window(tmp_path):
    """test 的 claim→finalize 窗口（锁文件已在、consumed 未置）不算已烧；
    consumed=True 才告警。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "test_lock.json").write_text(json.dumps(
        {"consumed": False, "claim": "w1"}), encoding="utf-8")
    loop = b._loop_directive()
    assert "test_burned" not in loop, "claim 窗口不得误亮已烧告警"
    (state / "test_lock.json").write_text(json.dumps(
        {"consumed": True}), encoding="utf-8")
    loop2 = b._loop_directive()
    assert loop2.get("test_burned") is not None
