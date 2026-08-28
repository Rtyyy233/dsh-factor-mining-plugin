# coding=utf-8
"""换手率定价与 net 口径回归（2026-08-28 规划书 PLAN-turnover-net-basis）。

锁定：
1. 换手算术：已知相邻 top-K 集合（全换/半换/不动）→ turn 序列精确，
   首期 1.0（对称差 / 2K，单边口径）
2. net 恒等式：net_mean = gross_mean − 2·cost·turn_avg（_top_n_excess
   同式：单边成本 × 双边 × 单边换手）；c* = gross/(2·turn)（bps）
3. rank_autocorr：与朴素实现一致 + 向量化性能（事故正名的验收标准）
4. 双报阶段：net_basis=False 判毛 / True 判 net / 缺 net 字段回毛标
   legacy——门公式不变，只换输入
5. null 左移：随机因子 net_spread_ir 分布显著低于毛版（随机树高换手，
   WS1 net 重校的前提证据）
6. 成本进键：cost_model_version 变更 → 尾账本新键；同键重评 = 更新
7. 自动接线：tail_metrics 产出全字段（turn/net/c*/rank_autocorr/
   cost_used/cost_model_version），evaluate 响应自动携带
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.tail import (  # noqa: E402
    rank_autocorr, spread_ir_statistic, spread_turn_stats, tail_metrics)
from dsh_factor_mining.factor.tailgate import tail_admission, tail_ledger  # noqa: E402


# ---- 1. 换手算术：构造已知相邻 top 集合 ----

def _known_tops_panel(blocks):
    """F/fwd/pit 合成面板：采样行按 blocks 切换 top-2 集合。
    blocks = [((0,1), n_rows), ...]；N=10、k_frac=0.2 → k=2。"""
    T, N = sum(n for _, n in blocks) * 5, 10
    F = np.zeros((T, N))
    row = 0
    for tops, n_rows in blocks:
        for _ in range(n_rows):
            for j in range(N):
                F[row * 5, j] = (200.0 if j in tops else 100.0) + j
            row += 1
    rng = np.random.default_rng(7)
    fwd = rng.normal(0.0, 0.01, (T, N))
    pit = np.ones((T, N), dtype=bool)
    return F, fwd, pit


def test_turnover_arithmetic_known_sets():
    """rows: {0,1}×10 → {1,2}×10（半换）→ {1,2}×10（不动）。
    turn = [1.0] + [0]×9 + [0.5] + [0]×9 → avg = 1.5/20 = 0.075。"""
    F, fwd, pit = _known_tops_panel([((0, 1), 10), ((1, 2), 10)])
    out = spread_turn_stats(F, fwd, pit, 5, 0.2)
    assert out["turn_avg"] is not None
    assert abs(out["turn_avg"] - 0.075) < 1e-12, out


def test_turnover_first_period_is_one():
    """单块不动名单：turn = [1.0, 0, 0, ...] → avg = 1/n_rows。"""
    F, fwd, pit = _known_tops_panel([((0, 1), 20)])
    out = spread_turn_stats(F, fwd, pit, 5, 0.2)
    assert abs(out["turn_avg"] - 1.0 / 20) < 1e-12, out


# ---- 2. net 恒等式与 c* ----

def test_net_identity_and_break_even():
    """net_mean ≡ gross_mean − 2·cost·turn_avg（生产 _top_n_excess 同式）；
    c* = gross_mean/(2·turn_avg)（单边 bps）。"""
    rng = np.random.default_rng(21)
    T, N = 600, 40
    F = rng.normal(0, 1, (T, N))
    fwd = rng.normal(0, 0.01, (T, N))
    pit = np.ones((T, N), dtype=bool)
    cost = 0.001
    out = spread_turn_stats(F, fwd, pit, 5, 0.2, cost=cost)
    assert out["gross_mean"] is not None and out["turn_avg"] > 0
    assert abs(out["net_mean"] - (out["gross_mean"] - 2 * cost * out["turn_avg"])) < 1e-12
    assert abs(out["break_even_cost"]
               - out["gross_mean"] / (2 * out["turn_avg"]) * 1e4) < 0.11
    # 成本加重 → net_spread_ir 单调不升（同一 F/fwd，cost 越大净 IR 越低）
    out0 = spread_turn_stats(F, fwd, pit, 5, 0.2, cost=0.0)
    out1 = spread_turn_stats(F, fwd, pit, 5, 0.2, cost=0.01)
    assert out1["net_spread_ir"] < out0["net_spread_ir"]


def test_break_even_none_when_no_gross():
    """毛均 ≤ 0 → c* 缺省不炸（如实不可比，不造负 bps 误导漏斗）。"""
    rng = np.random.default_rng(5)
    T, N = 300, 30
    F = rng.normal(0, 1, (T, N))
    fwd = np.zeros((T, N))  # 全零 fwd → spread ≡ 0 → gross_mean = 0
    pit = np.ones((T, N), dtype=bool)
    out = spread_turn_stats(F, fwd, pit, 5, 0.2, cost=0.001)
    assert out["break_even_cost"] is None
    # 零信号 + 随机换手：net 序列 = −2·cost·turn（有方差）→ 负 IR
    # ——随机 churn 的成本拖累是诚实数字，不是 None
    assert out["net_spread_ir"] is not None and out["net_spread_ir"] < 0


def test_spread_ir_statistic_backward_compat():
    """cost=None 毛口径行为不变；cost=0 与毛口径 net==gross 逐位一致。"""
    rng = np.random.default_rng(9)
    T, N = 400, 30
    F = rng.normal(0, 1, (T, N))
    fwd = rng.normal(0, 0.01, (T, N))
    pit = np.ones((T, N), dtype=bool)
    legacy = spread_ir_statistic(F, fwd, pit, 5)
    full = spread_turn_stats(F, fwd, pit, 5, 0.2)
    assert legacy == full["spread_ir"]
    net0 = spread_turn_stats(F, fwd, pit, 5, 0.2, cost=0.0)
    assert net0["net_spread_ir"] == full["spread_ir"]


# ---- 3. rank_autocorr：正确性 + 性能 ----

def test_rank_autocorr_matches_naive():
    rng = np.random.default_rng(33)
    T, N = 260, 25
    F = rng.normal(0, 1, (T, N))
    pit = np.ones((T, N), dtype=bool)
    rows = np.arange(0, T, 5)
    lag = 5
    naive = []
    for t in rows:
        t2 = t + lag
        if t2 >= T:
            break
        a = pd.Series(F[t]).rank().values
        b = pd.Series(F[t2]).rank().values
        if a.std() > 0 and b.std() > 0:
            naive.append(float(np.corrcoef(a, b)[0, 1]))
    got = rank_autocorr(F, pit, rows, lag)
    assert got is not None
    assert abs(got - float(np.mean(naive))) < 1e-10


def test_rank_autocorr_perf_vectorized():
    """向量化性能验收（rank autocorrelation 超时事故的正名）：
    2000×300 面板、400 个秩对 < 100ms。"""
    rng = np.random.default_rng(1)
    T, N = 2000, 300
    F = rng.normal(0, 1, (T, N))
    pit = np.ones((T, N), dtype=bool)
    rows = np.arange(0, T, 5)
    t0 = time.perf_counter()
    out = rank_autocorr(F, pit, rows, 5)
    dt = time.perf_counter() - t0
    assert out is not None and abs(out) < 0.5  # iid → ≈0
    assert dt < 0.1, f"rank_autocorr 耗时 {dt*1000:.0f}ms（应向量化 <100ms）"


# ---- 4. 双报阶段：net_basis 开关 ----

def _admission_block(spread_ir, net_spread_ir):
    return {
        "region": "train", "k_frac": 0.2, "spread_ir": spread_ir,
        "spread_moments": None,
        "net_spread_ir": net_spread_ir, "net_spread_moments": None,
        "turn_tail": 0.4, "break_even_cost": 30.0,
        "cost_used": 10.0, "cost_model_version": "flat:v1",
        "topn": {"placebo_z": 5.0, "placebo_draws": 12, "periods": 100},
    }


def test_net_basis_switch_gates_on_net():
    """同块毛过净不过：False → accepted（判毛）；True → 拒（判 net）。
    门公式不变，只换输入序列。"""
    blk = _admission_block(spread_ir=0.5, net_spread_ir=0.05)
    off = tail_admission(blk, [], 0.5, placebo_z_gate=5.0, placebo_m=100,
                         net_basis=False)
    on = tail_admission(blk, [], 0.5, placebo_z_gate=5.0, placebo_m=100,
                        net_basis=True)
    assert off["accepted"] is True
    assert off["diag"]["basis"] == "gross"
    assert on["accepted"] is False
    assert on["diag"]["basis"] == "net"
    assert "net_spread_ir" in on["reason"]
    assert on["diag"]["cost_model_version"] == "flat:v1"
    assert on["diag"]["break_even_cost"] == 30.0


def test_net_basis_legacy_fallback():
    """旧条目无 net 字段 + net_basis=True → 回毛口径 + legacy 标注
    （降级不拒判——与 s0 降级同纪律）。"""
    blk = _admission_block(spread_ir=0.5, net_spread_ir=None)
    r = tail_admission(blk, [], 0.5, placebo_z_gate=5.0, placebo_m=100,
                       net_basis=True)
    assert r["accepted"] is True
    assert r["diag"]["basis"] == "legacy"


# ---- 5. null 左移：随机因子 net 分布低于毛版 ----

def test_random_factor_null_shifts_left_under_cost():
    rng = np.random.default_rng(77)
    T, N = 1200, 40
    pit = np.ones((T, N), dtype=bool)
    gross_irs, net_irs, turns = [], [], []
    for s in range(15):
        r = np.random.default_rng(100 + s)
        F = r.normal(0, 1, (T, N))
        fwd = rng.normal(0, 0.01, (T, N))  # 零信号 fwd：纯 null
        out = spread_turn_stats(F, fwd, pit, 5, 0.2, cost=0.001)
        if out["spread_ir"] is not None:
            gross_irs.append(out["spread_ir"])
        if out.get("net_spread_ir") is not None:
            net_irs.append(out["net_spread_ir"])
        turns.append(out["turn_avg"])
    # 随机因子换手极高（独立 top-K 集，理论 ≈0.8）
    assert float(np.mean(turns)) > 0.5, turns
    # net 分布显著左移（WS1 net 重校的前提证据；不左移就不该切基）
    assert float(np.mean(net_irs)) < float(np.mean(gross_irs)) - 0.1, (
        gross_irs, net_irs)


# ---- 6. 成本进键：尾账本去重语义 ----

def test_ledger_key_includes_cost_model_version():
    trail = [
        {"stage": "development", "source_hash": "h1", "horizon": 5,
         "tail": {"k_frac": 0.2, "spread_ir": 0.4,
                  "cost_model_version": "flat:v1"}},
        {"stage": "development", "source_hash": "h1", "horizon": 5,
         "tail": {"k_frac": 0.2, "spread_ir": 0.41,
                  "cost_model_version": "flat:v1"}},   # 同键 = 更新
        {"stage": "development", "source_hash": "h1", "horizon": 5,
         "tail": {"k_frac": 0.2, "spread_ir": 0.42}},  # 无版本 = legacy 新键
        {"stage": "development", "source_hash": "h1", "horizon": 5,
         "tail": {"k_frac": 0.2, "spread_ir": 0.43,
                  "cost_model_version": "flat:v2"}},   # 版本变 = 新键重计
    ]
    rows = tail_ledger(trail)
    assert len(rows) == 3
    versions = sorted(r.get("cost_model_version") or "legacy" for r in rows)
    assert versions == ["flat:v1", "flat:v2", "legacy"]
    flat_v1 = [r for r in rows
               if r.get("cost_model_version") == "flat:v1"][0]
    assert flat_v1["spread_ir"] == 0.41  # 更新不新增


# ---- 7. 自动接线：tail_metrics 全字段 + evaluate 响应携带 ----

def _make_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(11)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    mu = np.where(np.arange(N) < 20, 0.004, -0.004)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(mu[i], 0.01, T))
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
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high",
                                "low": "low", "close": "close",
                                "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


MOMENTUM_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""

NOISY_TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    rng = np.random.default_rng(3)
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
    base = np.broadcast_to(tilt, env.c.shape).astype(float)
    return base + rng.normal(0.0, 3.0, env.c.shape)
"""


def test_tail_metrics_full_fields_and_slow_fast_gap(tmp_path):
    """新字段全产出；慢因子（momentum，秩稳定）换手显著低于快因子
    （tilt+大噪声，top 集每期重洗）。"""
    b = _make_bridge(tmp_path)
    env = b.envs["primary"]
    fn = b._compile_factor(MOMENTUM_SOURCE)
    tm = tail_metrics(fn(env), env, placebo_draws=5)
    for k in ("turn_tail", "net_spread_ir", "net_spread_moments",
              "break_even_cost", "rank_autocorr", "cost_used",
              "cost_model_version"):
        assert k in tm, k
    assert tm["cost_used"] == 10.0
    assert tm["cost_model_version"] == "flat:v1"
    assert tm["net_spread_ir"] is not None
    fn2 = b._compile_factor(NOISY_TILT_SOURCE)
    tm2 = tail_metrics(fn2(env), env, placebo_draws=5)
    assert tm2["turn_tail"] > tm["turn_tail"] > 0
    assert tm["rank_autocorr"] > tm2["rank_autocorr"]


def test_evaluate_response_carries_net_fields(tmp_path):
    """自动接线：evaluate 响应的 tail 块带换手定价字段（自动计算 =
    自动计数——账本据此立 net 键）。"""
    b = _make_bridge(tmp_path)
    r = b.dispatch("factor.evaluate",
                   {"envId": "primary", "source": MOMENTUM_SOURCE})
    tail = r["tail"]
    assert tail.get("cost_model_version") == "flat:v1"
    assert tail.get("cost_used") == 10.0
    assert "net_spread_ir" in tail and "turn_tail" in tail
    assert "break_even_cost" in tail and "rank_autocorr" in tail
