# coding=utf-8
"""ts 评估层专项单测（2026-11-07 任务书 §八 ①④⑥）。

覆盖：
- 合成 AR/漂移信号已知 IR 容差（乘积序列口径 z(信号)×前向收益）
- 前向对齐与不重叠采样（步长=h；G-limit 剔除）
- ts_null 纯噪声 p 均匀性（KS）
- 诊断同形：evaluate_ts 键集 ⊇ discipline/verify 消费面
- PIT 滚动 z：无未来信息（截断因果性）+ 全窗标准化机械偏不复现
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from dsh_factor_mining.factor.env import Calibration, TsEnv
from dsh_factor_mining.factor.evaluate_ts import (
    TS_Z_WINDOW,
    evaluate_ts,
    evaluate_walk_forward_ts,
    limit_hit_bars,
    position_sim,
    series_ir,
    ts_block_perm_null,
    ts_date_shift_placebo,
    ts_forward_returns,
    ts_product_series,
)


def _mk_env(T=1200, seed=7, drift_cycle=300.0, sigma=0.008,
            dev_end="2022-06-01", limit_pct=0.06) -> TsEnv:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2018-01-02", periods=T)
    mu = 0.006 * np.sin(2.0 * np.pi * np.arange(T) / drift_cycle)
    ret = mu + rng.normal(0.0, sigma, T)
    c = 3500.0 * np.cumprod(1.0 + ret)
    o = c * (1.0 + rng.normal(0.0, 0.002, T))
    v = rng.lognormal(9.0, 0.4, T)
    oi = rng.lognormal(12.0, 0.3, T)
    cal = Calibration(horizon=20, dev_end=dev_end, sel_end="2023-06-01")
    return TsEnv(o, np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998,
                 c, v, oi, dates, "rb", limit_pct=limit_pct, calibration=cal)


def _mom(env, k=60):
    return (pd.Series(env.c) / pd.Series(env.c).shift(k) - 1.0).values


# ---------------------------------------------------------------------------
# ① 已知 IR 容差 + ② 前向对齐/不重叠
# ---------------------------------------------------------------------------

def test_forward_return_alignment_t1():
    """t1 执行：ic_t 用的前向收益 = c[t+h]/o[t+1]-1（逐点对齐）。"""
    env = _mk_env(T=300, seed=3)
    fwd = ts_forward_returns(env, 20)
    t = 50
    expect = env.c[t + 20] / env.o[t + 1] - 1.0
    assert math.isclose(float(fwd[t]), float(expect), rel_tol=1e-12)
    assert np.isnan(fwd[-1]) and np.isnan(fwd[-20])


def test_product_series_non_overlapping_step():
    """不重叠采样：采样步长 = max(ic_sample_every, h)=h；索引间隔 ≥ h。"""
    env = _mk_env(T=600, seed=5)
    s = ts_product_series(_mom(env), env, horizon=20)
    idx = np.asarray([env.dates.index(d) for d in s.index])
    assert len(s) >= 10
    assert np.all(np.diff(idx) >= 20)


def test_known_ir_sign_and_magnitude_on_drift_process():
    """慢变漂移过程上动量 IR 显著为正（量级容差：IR·√n = t ≥ 3）。"""
    env = _mk_env(seed=11)
    s = ts_product_series(_mom(env), env, horizon=20)
    ir = series_ir(s)
    assert ir > 0
    assert ir * math.sqrt(len(s)) >= 3.0


def test_zero_signal_ir_near_zero_on_random_walk():
    """纯随机游走：动量 IR 的 t 统计量不显著（|t| < 3，多种子聚合）。"""
    ts = []
    for seed in range(30):
        env = _mk_env(seed=1000 + seed, drift_cycle=1e9, sigma=0.012)
        s = ts_product_series(_mom(env), env, horizon=20)
        ir = series_ir(s)
        if np.isfinite(ir):
            ts.append(ir * math.sqrt(len(s)))
    assert abs(float(np.mean(ts))) < 3.0     # 机械偏已除（PIT 滚动 z）


def test_g_limit_masks_limit_bars():
    """G-limit：前向窗含停板 bar 的观测被剔除（构造停板日）。"""
    env = _mk_env(T=400, seed=9, limit_pct=0.05)
    # 注入停板：把两天收益推到 ±6%
    c = env.c.copy()
    c[150] = c[149] * 1.061
    c[151] = c[150] * 1.061
    env2 = TsEnv(env.o, env.h, env.l, c, env.v, env.oi, env.dates,
                 "rb", limit_pct=0.05, calibration=env.calibration)
    hit = limit_hit_bars(env2)
    assert hit[150] and hit[151] and not hit[10]
    s_full = ts_product_series(_mom(env), env, horizon=20)
    s_lim = ts_product_series(_mom(env2), env2, horizon=20)
    # 含停板窗的采样行被剔除 → 序列变短（停板在 150-151，落进若干前向窗）
    assert len(s_lim) <= len(s_full)
    dates_idx = {env.dates.index(d) for d in s_full.index}
    for t in range(130, 152, 20):           # 覆盖停板窗的采样行应缺席
        assert t not in dates_idx or t not in {env.dates.index(d) for d in s_lim.index}


# ---------------------------------------------------------------------------
# ④ ts_null 纯噪声 p 均匀（KS）
# ---------------------------------------------------------------------------

def test_ts_null_p_uniform_on_noise():
    """纯噪声信号的 ts_null p 值近似均匀（KS，p>0.01）。"""
    env = _mk_env(seed=21, drift_cycle=1e9, sigma=0.012)
    ps = []
    for i in range(30):
        rng = np.random.default_rng(500 + i)
        F = rng.normal(0.0, 1.0, env.T)
        r = ts_block_perm_null(F, env, n_perm=100, seed=500 + i)
        if isinstance(r.get("p"), (int, float)) and math.isfinite(r["p"]):
            ps.append(float(r["p"]))
    assert len(ps) >= 25
    # KS 检验 vs U(0,1)
    x = np.sort(np.asarray(ps))
    n = len(x)
    F0 = x
    D = max(np.max(F0 - np.arange(n) / n), np.max((np.arange(n) + 1) / n - F0))
    # D 临界（α=0.01，n=30）≈ 0.27
    assert D < 0.27, D


def test_ts_null_detects_real_structure():
    """真结构（漂移过程动量）→ |z|>3 且 p 极小。"""
    env = _mk_env(seed=11)
    r = ts_block_perm_null(_mom(env), env, n_perm=100, seed=42)
    assert abs(r["z"]) > 3.0 and r["p"] < 0.01


# ---------------------------------------------------------------------------
# ⑥ 诊断同形：键集 ⊇ 消费面
# ---------------------------------------------------------------------------

CONSUMER_KEYS = (
    "ic_mean_train", "ic_ir_train", "ic_n_train",          # submit 重算/receipt
    "column_perm_train",                                    # discipline verdict
    "deflated_train",                                       # 墙/deflated p/M3
    "yearly_consistency_train", "rolling_ic_stability_train",
    "decay_train", "date_shift", "beta_exposure", "topn",
    "carving", "train_sensitivity", "verdict", "red_flags",
    "horizon",
)


def test_diagnosis_shape_covers_consumers():
    env = _mk_env(seed=11)
    d = evaluate_ts(_mom(env), env, n_trials=1.0, pool_std=0.15,
                    bar_sigma=0.8, wall_mode="growing")
    for k in CONSUMER_KEYS:
        assert k in d, k
    assert d["column_perm_train"]["method"] == "ts_block_perm"
    assert d["beta_exposure"] is None
    assert isinstance(d["topn"], dict) and "gross_ir" in d["topn"]
    assert d["line"] == "ts_single" and d["instrument"] == "rb"
    # deflated 充分统计量齐（提交链重算要求）
    for k in ("sr_hat", "skew", "kurt", "n_obs"):
        assert d["deflated_train"].get(k) is not None, k


def test_date_shift_placebo_collapses():
    """date-shift：真信号 IR 随 shift 崩塌（shift≥20 后 |IR| 大幅下降）。"""
    env = _mk_env(seed=11)
    r = ts_date_shift_placebo(_mom(env), env, shifts=(5, 10, 21, 60))
    base = abs(r["unshifted_ir"])
    assert all(abs(r["shifted_ir"][k]) < base * 0.6 for k in ("21", "60"))


def test_position_sim_sign_hold():
    """position_sim：恒正信号=持多仓；入场付一次换手后 turn=0。"""
    env = _mk_env(seed=13)
    F = np.ones(env.T)
    ps = position_sim(F, env)
    assert ps is not None and ps["n_obs"] >= 10
    # 初次建仓付一次 |1-0|=1 的换手，此后恒仓 → turn_avg = 1/n_obs
    assert ps["turn_avg"] == pytest.approx(1.0 / ps["n_obs"], abs=2e-3)  # 内部 round 4 位
    # 净/毛只差入场成本（cost_bps×turn/1e4 的 IR 化影响微小）
    assert ps["gross_ir"] == pytest.approx(ps["net_ir"], abs=5e-3)


def test_pit_z_no_future_leakage():
    """PIT 滚动 z 截断因果性：扰动 F[t:] 不改变 z[:t]。"""
    env = _mk_env(T=400, seed=17)
    F = _mom(env, 30)
    s1 = ts_product_series(F, env, horizon=20)
    t_cut = env.T // 2
    F2 = F.copy()
    F2[t_cut:] = np.random.default_rng(99).normal(0, 1, env.T - t_cut)
    s2 = ts_product_series(F2, env, horizon=20)
    common = [d for d in s1.index if d < pd.Timestamp(env.dates[t_cut])]
    a1 = [s1[d] for d in common]
    a2 = [s2[d] for d in common]
    assert a1 and np.allclose(a1, a2, equal_nan=True)


def test_walk_forward_ts_runs_in_dev_window():
    env = _mk_env(seed=11)
    r = evaluate_walk_forward_ts(_mom(env), env, n_folds=4,
                                 t0_date="2021-06-01")
    assert "per_fold" in r and len(r["per_fold"]) == 4
    assert r["per_fold"][0]["t0"] >= "2021-06-01"
