# coding=utf-8
"""Evaluation engine (adapted from the reference harness; all paths are portable).

Numbers are produced with the same reference definitions so the P1 parity test
can compare this package against the original harness on synthetic matrices:
section IC / IC_IR, yearly consistency, column-perm null, beta exposure,
top-N excess, decay diagnostics, composite onion, batch deflate, walk-forward.

The only intentional differences:
- no machine-specific data paths;
- test_lock lives in a user-supplied state root (never in the package).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .registry import cs_rank_corr

H = 20            # 历史默认 horizon（现从 env.calibration.horizon 读取；此处仅作文档）
COST = 0.0010     # 历史默认成本（现从 env.calibration.cost 读取）
TOP_N = 10        # 历史默认组合宽度（现从 env.calibration.top_n 读取）
MIN_POOL = 30     # minimum cross-sectional names（结构性约束，非市场口径）
DEV_END = "2021-01-01"  # 历史默认 development end（现从 env.calibration.dev_end 读取）
SEL_END = "2024-01-01"  # 历史默认 selection end（现从 env.calibration.sel_end 读取）
TRAIN_END = DEV_END

DEFAULT_STATE_ROOT = Path.cwd() / ".factor-mining"


def _test_lock_path(state_root=None):
    root = Path(state_root) if state_root else Path(
        os.environ.get("DSH_FACTOR_MINER_STATE_ROOT", DEFAULT_STATE_ROOT)
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "test_lock.json"


def _forward_returns(env):
    """fwd[t] 按 env.calibration：
    - t1（默认）: 信号 T 收盘 -> T+1 开盘入场 -> T+H 收盘出场（close[t+H]/open[t+1]-1）
    - t0       : 信号 bar 收盘即入场 -> T+H 收盘出场（close[t+H]/close[t]-1）
    H 的单位 = bar（daily=交易日；minute=bar 数）。"""
    cal = env.calibration
    c = pd.DataFrame(env.c)
    o = pd.DataFrame(env.o)
    if cal.execution == "t0":
        return (c.shift(-cal.horizon) / c - 1.0).values
    return (c.shift(-cal.horizon) / o.shift(-1) - 1.0).values


def _pit_mask(env):
    m = env.listed.copy()
    if env.amount is not None:
        m = m & (env.amount > 0)
    if env.calibration.limit_up_down_mask:
        # 一字板近似（h==l）：涨/跌停无法成交，入场信号不可执行 —— 从可交易 mask 剔除。
        m = m & ~(env.h == env.l)
    return m


def _cross_sectional_ic(F, fwd, pit, env, sig_only=False):
    T, N = F.shape
    dates = env.dates
    out = {}
    Fr = np.empty_like(F)
    fwdr = np.empty_like(fwd)
    sig_idx = set(np.arange(0, T, env.calibration.sample_step)) if sig_only else None
    for t in range(T):
        if sig_only and t not in sig_idx:
            continue
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        if m.sum() < MIN_POOL:
            continue
        x = F[t][m]
        y = fwd[t][m]
        rx = pd.Series(x).rank(pct=True).values
        ry = pd.Series(y).rank(pct=True).values
        if rx.std() == 0 or ry.std() == 0:
            continue
        out[dates[t]] = np.corrcoef(rx, ry)[0, 1]
    return pd.Series(out)


def _ic_stats(ic_series):
    ic = ic_series.dropna()
    if len(ic) < 2:
        return dict(mean=np.nan, std=np.nan, ir=np.nan, n=len(ic))
    mean = ic.mean()
    std = ic.std(ddof=1)
    ir = mean / std if std > 0 else np.nan
    return dict(mean=float(mean), std=float(std), ir=float(ir), n=len(ic))


def _yearly_sign_consistency(ic_series):
    ic = ic_series.dropna()
    if len(ic) == 0:
        return dict(consistent=0.0, yearly={})
    yearly = {}
    for yr, g in ic.groupby(ic.index.year):
        yearly[int(yr)] = float(g.mean())
    overall_sign = np.sign(ic.mean())
    if overall_sign == 0:
        return dict(consistent=0.5, yearly=yearly)
    same = sum(1 for v in yearly.values() if np.sign(v) == overall_sign)
    return dict(consistent=same / len(yearly), yearly=yearly)


def _column_perm_test(F, fwd, pit, env, n_perm=200, seed=42, train_end=None, t0_date=None, t1_date=None,
                      real_ic=None):
    def _sl(ic):
        if t0_date is not None:
            ic = ic[ic.index >= pd.Timestamp(t0_date)]
        if t1_date is not None:
            ic = ic[ic.index < pd.Timestamp(t1_date)]
        elif train_end is not None:
            ic = ic[ic.index < pd.Timestamp(train_end)]
        return ic

    rng = np.random.default_rng(seed)
    # real_ic 可由调用方传入已过滤的序列（evaluate 已算过 train_sig，避免整段重复计算）
    real_ic = _sl(real_ic) if real_ic is not None else _sl(_cross_sectional_ic(F, fwd, pit, env, sig_only=True))
    real_mean = real_ic.mean()
    T, N = F.shape
    null_means = []
    for _ in range(n_perm):
        Fp = np.empty_like(F)
        for t in range(T):
            m = pit[t] & np.isfinite(F[t])
            idx = np.where(m)[0]
            if len(idx) < 2:
                Fp[t] = F[t]
            else:
                perm = idx.copy()
                rng.shuffle(perm)
                Fp[t, idx] = F[t, perm]
        icp = _sl(_cross_sectional_ic(Fp, fwd, pit, env, sig_only=True))
        if len(icp) > 0:
            null_means.append(icp.mean())
    null_means = np.array(null_means)
    if len(null_means) == 0 or null_means.std() == 0:
        return dict(z=np.nan, p=np.nan,
                    null_mean=float(null_means.mean()) if len(null_means) else np.nan)
    z = (real_mean - null_means.mean()) / null_means.std()
    p = math.erfc(abs(z) / math.sqrt(2))
    return dict(z=float(z), p=float(p), null_mean=float(null_means.mean()))


def _block_bootstrap(net_series, L=6, B=2000, seed=42):
    """块自助推断：均值是否显著异于 0。

    circular block bootstrap 的分布中心就是样本均值本身——拿「观测均值 − 自助均值」
    当 z 分子恒≈0（旧实现的结构性死检验）。正确形式：z = mu / se，
    se 用块自助标准误以吸收自相关。
    """
    x = np.asarray(net_series, dtype=np.float64)
    n = len(x)
    if n < 10 or x.std() == 0:
        return dict(z=np.nan, p=np.nan, null_mean=float(x.mean()), real_mean=float(x.mean()))
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / L))
    starts = rng.integers(0, n, size=(B, nb))
    off = np.arange(L)
    idx = (starts[:, :, None] + off[None, None, :]) % n
    xb = x[idx.reshape(B, -1)[:, :n]]
    stats = xb.mean(axis=1)
    se = stats.std(ddof=1)
    mu = x.mean()
    z = mu / se if se > 0 else np.nan
    p = math.erfc(abs(z) / math.sqrt(2)) if np.isfinite(z) else np.nan
    return dict(z=float(z), p=float(p), null_mean=float(stats.mean()), real_mean=float(mu))


def _rolling_ic_stability(ic_series, window=252):
    ic = ic_series.dropna()
    if len(ic) < window:
        return dict(stable=np.nan, n_windows=0)
    roll = ic.rolling(window, min_periods=window).mean().dropna()
    if len(roll) == 0:
        return dict(stable=np.nan, n_windows=0)
    overall_sign = np.sign(ic.mean())
    if overall_sign == 0:
        return dict(stable=0.5, n_windows=len(roll))
    same = float((np.sign(roll.values) == overall_sign).mean())
    return dict(stable=same, n_windows=len(roll))


def _decay_diagnostics(ic_series):
    ic = ic_series.dropna()
    n = len(ic)

    def _empty():
        return dict(n_total=n, K=0, n_per_interval=0, power="insufficient",
                    per_interval_ic=[], interval_slope=np.nan, first_last_delta=np.nan,
                    delta_z=np.nan, se_delta=np.nan, monotonic_rho=np.nan, sign_flip=False,
                    yearly_slope=np.nan, decay_direction="insufficient", decay_flag="insufficient")

    if n < 20:
        return _empty()

    MIN_PER_INTERVAL = 20
    MAX_INTERVALS = 8
    K = max(2, min(MAX_INTERVALS, n // MIN_PER_INTERVAL))
    edges = np.linspace(0, n, K + 1).astype(int)
    per_interval_ic = [float(np.mean(ic.iloc[edges[i]:edges[i + 1]].values)) for i in range(K)]
    pia = np.array(per_interval_ic)
    n_per = n // K

    x = np.arange(K, dtype=np.float64)
    if K >= 3 and np.std(x) > 0:
        interval_slope = float(np.polyfit(x, pia, 1)[0])
        rx = pd.Series(x).rank().values
        ry = pd.Series(pia).rank().values
        if np.std(rx) > 0 and np.std(ry) > 0:
            monotonic_rho = float(np.corrcoef(rx, ry)[0, 1])
        else:
            monotonic_rho = np.nan
    else:
        interval_slope = np.nan
        monotonic_rho = np.nan

    first_last_delta = float(pia[-1] - pia[0])
    sign_flip = bool(np.sign(pia[0]) != 0 and np.sign(pia[-1]) != 0
                     and np.sign(pia[0]) != np.sign(pia[-1]))

    ic_std = float(np.std(ic.values, ddof=1)) if n > 1 else 0.0
    se_delta = float(np.sqrt(2.0) * ic_std / np.sqrt(n_per)) if (n_per > 1 and ic_std > 0) else np.nan
    delta_z = float(first_last_delta / se_delta) if (np.isfinite(se_delta) and se_delta > 1e-12) else np.nan

    yearly = {}
    for yr, g in ic.groupby(ic.index.year):
        yearly[int(yr)] = float(g.mean())
    years = np.array(sorted(yearly.keys()), dtype=np.float64)
    vals = np.array([yearly[int(y)] for y in years], dtype=np.float64)
    if len(years) >= 3 and np.std(years) > 0:
        yearly_slope = float(np.polyfit(years, vals, 1)[0])
    else:
        yearly_slope = np.nan

    if n >= 120:
        power = "high"
    elif n >= 60:
        power = "medium"
    else:
        power = "low"

    if sign_flip:
        direction = "reversed"
    elif not np.isfinite(delta_z):
        direction = "low_power" if power == "low" else "flat"
    elif power == "high":
        if delta_z <= -2.0:
            direction = "decaying"
        elif delta_z >= 2.0:
            direction = "rising"
        else:
            direction = "stable"
    elif power == "medium":
        if delta_z <= -2.0:
            direction = "weak_decaying"
        elif delta_z >= 2.0:
            direction = "weak_rising"
        else:
            direction = "flat"
    else:
        direction = "low_power"

    return dict(n_total=n, K=K, n_per_interval=n_per, power=power,
                per_interval_ic=per_interval_ic, interval_slope=interval_slope,
                first_last_delta=first_last_delta, delta_z=delta_z, se_delta=se_delta,
                monotonic_rho=monotonic_rho, sign_flip=sign_flip,
                yearly_slope=yearly_slope, decay_direction=direction, decay_flag=direction)


def _max_corr_with_registered(F, env, registry_factors):
    if not registry_factors:
        return None
    Fd = pd.DataFrame(F)
    max_abs = 0.0
    for name, G in registry_factors.items():
        G = np.asarray(G, dtype=np.float64)
        if G.shape != F.shape:
            continue
        corrs = []
        for t in range(len(env.dates)):
            m = np.isfinite(Fd.iloc[t].values) & np.isfinite(G[t])
            if m.sum() < MIN_POOL:
                continue
            rF = pd.Series(Fd.iloc[t].values[m]).rank(pct=True)
            rG = pd.Series(G[t][m]).rank(pct=True)
            if rF.std() == 0 or rG.std() == 0:
                continue
            corrs.append(np.corrcoef(rF, rG)[0, 1])
        if corrs:
            max_abs = max(max_abs, abs(float(np.mean(corrs))))
    return float(max_abs)


def _beta_exposure(F, env):
    c = pd.DataFrame(env.c)
    ret1 = c.pct_change()
    mr = ret1.mean(axis=1, skipna=True)
    re_ema = ret1.ewm(span=252, min_periods=252).mean()
    pe_ema = mr.ewm(span=252, min_periods=252).mean()
    rp_ema = (ret1.multiply(mr, axis=0)).ewm(span=252, min_periods=252).mean()
    p2_ema = (mr ** 2).ewm(span=252, min_periods=252).mean()
    beta = rp_ema.sub(re_ema.multiply(pe_ema, axis=0)).div(p2_ema.sub(pe_ema ** 2).clip(1e-8), axis=0)
    beta_lag = beta.shift(1)
    Fd = pd.DataFrame(F)
    corrs = []
    for t in range(len(env.dates)):
        m = np.isfinite(Fd.iloc[t].values) & np.isfinite(beta_lag.iloc[t].values)
        if m.sum() < MIN_POOL:
            continue
        rF = pd.Series(Fd.iloc[t].values[m]).rank(pct=True)
        rB = pd.Series(beta_lag.iloc[t].values[m]).rank(pct=True)
        if rF.std() == 0 or rB.std() == 0:
            continue
        corrs.append(np.corrcoef(rF, rB)[0, 1])
    return float(np.mean(corrs)) if corrs else np.nan


def _top_n_excess(F, fwd, pit, env, top_n=None, cost=None, t0=0, t1=None):
    cal = env.calibration
    if top_n is None:
        top_n = cal.top_n
    if cost is None:
        cost = cal.cost
    hz = cal.horizon
    T, N = F.shape
    if t1 is None:
        t1 = T
    sig_idx = np.arange(0, T, cal.sample_step)
    sig_idx = sig_idx[(sig_idx >= t0) & (sig_idx < t1)]
    holdings = None
    rows = []
    for ti in sig_idx:
        if ti + hz >= T:
            continue
        m = pit[ti] & np.isfinite(F[ti]) & np.isfinite(fwd[ti])
        if m.sum() < top_n:
            continue
        order = np.argsort(-F[ti][m], kind='stable')[:top_n]
        cols = np.where(m)[0][order]
        top_set = set(cols.tolist())
        if holdings is None:
            turn = 1.0
        else:
            turn = len(top_set.symmetric_difference(holdings)) / (2 * top_n)
        holdings = top_set
        pool_mean = np.nanmean(fwd[ti][m])
        top_mean = np.nanmean(fwd[ti][cols])
        gross = top_mean - pool_mean
        net = gross - turn * 2 * cost
        rows.append((env.dates[ti], gross, net, turn))
    return pd.DataFrame(rows, columns=['date', 'gross', 'net', 'turn'])


def _norm_ppf(q: float) -> float:
    """标准正态分位数（stdlib 实现，无 scipy 依赖）。"""
    from statistics import NormalDist
    return NormalDist().inv_cdf(q)


def _deflated_sharpe_p(ic_series, n_trials: int = 1, pool_std: float | None = None) -> dict:
    """Deflated Sharpe Ratio 单因子 p（Bailey & López de Prado 2014，H.L.Z 精神）。

    对 IC 序列做偏度/峰度校正的 t 统计，并按「已试过 n_trials 个假设」的
    期望最大 null Sharpe 折减——同一因子试得越多，门槛自动越高。
    n_trials=1 时无多重检验惩罚（退化为校正 t 检验）。

    2026-08-18 生产审计修正（尺度 bug）：sr0 = √(2 ln N) 与 IC_IR（per-obs
    Sharpe）不同尺度——直接比较导致 N>1 时惩罚全灭（p=1.0）、N=1 时零惩罚。
    B-LP 标准式需乘池分布尺度：SR0 = √V[SR] · [(1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(N·e))]。
    pool_std = 已试假设 SR 分布的 std（bridge 从 trail_engine 实测 IC_IR 或
    null 地形分位数估计传入）。N>1 且无 pool_std 时拒绝给 p（不给不可信数字）。
    """
    ic = ic_series.dropna()
    n = len(ic)
    if n < 5 or ic.std(ddof=1) == 0:
        return {"p": None, "n_trials": int(n_trials), "n_obs": n, "note": "样本不足"}
    sr = float(ic.mean() / ic.std(ddof=1))
    g3 = float(((ic - ic.mean()) ** 3).mean() / max(ic.std(ddof=1) ** 3, 1e-18))
    g4 = float(((ic - ic.mean()) ** 4).mean() / max(ic.std(ddof=1) ** 4, 1e-18))
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    denom = max(denom, 1e-8)
    # 期望最大 null SR（N 次独立试验，B-LP 2014 式 4）：SR0 = √V · [(1-γ)Z(1-1/N)+γZ(1-1/(Ne))]
    # N=1 无惩罚；N>1 必须有 pool_std（池内 SR 分布尺度）——无尺度则拒绝给 p。
    if n_trials <= 1:
        sr0 = 0.0
        scale_note = "N=1 无多重检验惩罚"
    elif pool_std is not None and pool_std > 0:
        gamma = 0.5772156649015329  # Euler-Mascheroni
        z1 = _norm_ppf(1.0 - 1.0 / n_trials)
        z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
        sr0 = pool_std * ((1.0 - gamma) * z1 + gamma * z2)
        scale_note = f"pool_std={pool_std:.4f}（N={n_trials} 次试验的池分布缩放）"
    else:
        return {"p": None, "n_trials": int(n_trials), "n_obs": n, "sr_hat": sr,
                "skew": g3, "kurt": g4, "sr0": None,
                "note": "N>1 但缺 pool_std（池内 SR 分布尺度）——deflated p 不可信，拒绝给出。"
                        "需 trail_engine 实测 IC_IR 分布或 null 地形校准。"}
    t_stat = (abs(sr) - sr0) * (n - 1) ** 0.5 / denom ** 0.5
    p = 0.5 * math.erfc(t_stat / math.sqrt(2.0))  # 单尾 P(Z>t)：DSR 关心的是超出门槛的方向
    return {"p": float(p), "n_trials": int(n_trials), "n_obs": n,
            "sr_hat": sr, "sr0": sr0, "skew": g3, "kurt": g4,
            "pool_std": float(pool_std) if pool_std else None,
            "scale_note": scale_note}


def _train_sensitivity(ic_sig, env) -> dict:
    """启动点敏感性（诊断，不改变验收）：dev_end ±1/2/3 月扰动下 train 区结论稳定性。

    硬约束：只扰动 dev_end（侵入 selection 区可接受）；sel_end 绝不扰动——
    test 只能被消费一次，不得进入任何扰动扫描。
    """
    from ..discipline import red_flags_and_verdict  # noqa: F401 — 保持 evaluate 独立可测

    dev_end = pd.Timestamp(env.calibration.dev_end)
    sel_end = pd.Timestamp(env.calibration.sel_end)
    variants = []
    for months in (-3, -2, -1, 1, 2, 3):
        d = dev_end + pd.DateOffset(months=months)
        if d >= sel_end:
            d = sel_end - pd.Timedelta(days=1)
        if d <= ic_sig.index.min():
            continue
        sub = ic_sig[ic_sig.index < d]
        if len(sub) < 10:
            continue
        mean, std = float(sub.mean()), float(sub.std(ddof=1)) if len(sub) > 1 else float("nan")
        variants.append({"dev_end": str(d.date()), "offset_months": months,
                         "ic_mean": mean, "ic_ir": mean / std if std and std > 0 else None,
                         "n": len(sub)})
    if not variants:
        return {"variants": [], "stability": None, "verdict": "insufficient"}
    base = ic_sig[ic_sig.index < dev_end]
    base_ir = float(base.mean() / base.std(ddof=1)) if len(base) > 1 and base.std(ddof=1) > 0 else None
    irs = [v["ic_ir"] for v in variants if v["ic_ir"] is not None]
    same_sign = all(np.sign(x) == np.sign(base_ir) for x in irs) if base_ir else False
    rel_spread = ((max(irs) - min(irs)) / abs(base_ir)) if irs and base_ir else None
    if same_sign and (rel_spread is None or rel_spread < 0.5):
        verdict = "stable"
    elif same_sign:
        verdict = "magnitude_fragile"
    else:
        verdict = "sign_fragile"
    return {"variants": variants, "base_ic_ir": base_ir,
            "rel_spread": rel_spread, "verdict": verdict,
            "note": "只扰动 dev_end；sel_end/test 绝不进入扰动扫描"}


def evaluate(F, env, train_end=None, verbose=False, n_trials: int = 1,
             pool_std: float | None = None):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    if env.calibration.dev_end is None:
        raise ValueError(
            "三区分界未设置：calibration.dev_end/sel_end 为空（直调 API 需显式构造 "
            "Calibration(dev_end=..., sel_end=...)；config 路径会按数据自动划分）")
    train_end = train_end or env.calibration.dev_end

    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    if len(ic_sig) == 0:
        return dict(error="无有效截面（PIT mask 后标的不足 MIN_POOL）")

    t_end = np.searchsorted(env.dates, pd.Timestamp(train_end))
    train_sig = ic_sig[ic_sig.index < pd.Timestamp(train_end)]
    train_sig_stats = _ic_stats(train_sig)

    result = {
        "signal": None,
        "ic_mean_train": train_sig_stats["mean"],
        "ic_ir_train": train_sig_stats["ir"],
        "ic_n_train": train_sig_stats["n"],
        "yearly_consistency_train": _yearly_sign_consistency(train_sig),
        "column_perm_train": _column_perm_test(F, fwd, pit, env, train_end=train_end,
                                                real_ic=train_sig),
        "rolling_ic_stability_train": _rolling_ic_stability(train_sig),
        "decay_train": _decay_diagnostics(train_sig),
        "beta_exposure": _beta_exposure(F, env),
        "topn": None,
    }

    topn_train = _top_n_excess(F, fwd, pit, env, t0=0, t1=t_end)
    if len(topn_train) > 0:
        result["topn"] = _summarize_topn(topn_train, env)

    # 纪律层（discipline）：deflated p + 分界敏感性 + RED_FLAG + 结构化 verdict
    from ..discipline import red_flags_and_verdict
    result["deflated_train"] = _deflated_sharpe_p(train_sig, n_trials=n_trials,
                                                  pool_std=pool_std)
    result["train_sensitivity"] = _train_sensitivity(ic_sig, env)
    gv = red_flags_and_verdict(result, region="train")
    result["verdict"] = gv["verdict"]
    result["red_flags"] = gv["red_flags"]

    if verbose:
        _print_result(result)
    return result


def _summarize_topn(topn, env):
    cal = env.calibration
    # 交易每 sample_step 根 bar 发生一次（sig_idx 步长），年化倍数用它而非 horizon
    ann_factor = cal.annualization / cal.sample_step
    yearly_net = {}
    for yr, g in topn.groupby(topn['date'].dt.year):
        yearly_net[int(yr)] = float(g['net'].mean()) * ann_factor * 100
    return {
        "net_annual": float(topn['net'].mean()) * ann_factor * 100,
        "gross_annual": float(topn['gross'].mean()) * ann_factor * 100,
        "turn_avg": float(topn['turn'].mean()),
        "yearly_net": yearly_net,
        "block_bootstrap": _block_bootstrap(topn['net'].values),
    }


def _region_diagnostic(F, fwd, pit, env, region_ic, region_name, t0_date, t1_date):
    cp = _column_perm_test(F, fwd, pit, env, t0_date=t0_date, t1_date=t1_date)
    t0 = 0 if t0_date is None else np.searchsorted(env.dates, pd.Timestamp(t0_date))
    t1 = None if t1_date is None else np.searchsorted(env.dates, pd.Timestamp(t1_date))
    topn = _top_n_excess(F, fwd, pit, env, t0=t0, t1=t1)
    stats = _ic_stats(region_ic)
    result = {
        "region": region_name,
        "ic_mean": stats["mean"], "ic_ir": stats["ir"], "ic_n": stats["n"],
        "yearly_consistency": _yearly_sign_consistency(region_ic),
        "column_perm": cp,
        "rolling_ic_stability": _rolling_ic_stability(region_ic),
        "decay": _decay_diagnostics(region_ic),
        "beta_exposure": _beta_exposure(F, env),
        "topn": _summarize_topn(topn, env) if len(topn) > 0 else None,
    }
    from ..discipline import red_flags_and_verdict
    gv = red_flags_and_verdict(result, region=region_name)
    result["verdict"] = gv["verdict"]
    result["red_flags"] = gv["red_flags"]
    return result


def evaluate_selection(F, env, verbose=False):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    if env.calibration.dev_end is None or env.calibration.sel_end is None:
        raise ValueError("三区分界未设置：直调 API 需显式构造 Calibration(dev_end=..., sel_end=...)")
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    dev_end, sel_end = env.calibration.dev_end, env.calibration.sel_end
    sel_sig = ic_sig[(ic_sig.index >= pd.Timestamp(dev_end)) & (ic_sig.index < pd.Timestamp(sel_end))]
    if len(sel_sig) == 0:
        return dict(error="selection 区无有效截面")
    result = _region_diagnostic(F, fwd, pit, env, sel_sig, "selection", dev_end, sel_end)
    if verbose:
        _print_region_result(result)
    return result


def evaluate_test(F, env, verbose=False, state_root=None, source_hash=None,
                  fingerprint=None):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    if env.calibration.sel_end is None:
        raise ValueError("三区分界未设置：直调 API 需显式构造 Calibration(dev_end=..., sel_end=...)")
    lock_path = _test_lock_path(state_root)
    if lock_path.exists():
        try:
            with open(lock_path, encoding='utf-8') as f:
                lock = json.load(f)
        except Exception:
            lock = {}
        if lock.get("consumed", False):
            raise RuntimeError("test 已被消费（test_lock.json）。test 是最终消耗品，禁止反复评估调参。")
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    sel_end = env.calibration.sel_end
    test_sig = ic_sig[ic_sig.index >= pd.Timestamp(sel_end)]
    if len(test_sig) == 0:
        return dict(error="test 区无有效截面")
    result = _region_diagnostic(F, fwd, pit, env, test_sig, "test", sel_end, None)
    # 消费上下文（批次1a）：谁、什么口径、什么结果消费了这一次性的 test
    import datetime as _dt
    lock_payload = {
        "consumed": True,
        "note": "test 已消费一次，禁止再次评估",
        "consumed_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_hash": source_hash,
        "fingerprint": fingerprint,
        "engine_version": __import__("dsh_factor_mining", fromlist=["__version__"]).__version__,
        "diagnosis_summary": {"ic_ir": result.get("ic_ir"), "ic_mean": result.get("ic_mean"),
                              "ic_n": result.get("ic_n"), "verdict": result.get("verdict")},
        "calibration": {"dev_end": env.calibration.dev_end,
                        "sel_end": env.calibration.sel_end,
                        "horizon": env.calibration.horizon},
    }
    with open(lock_path, 'w', encoding='utf-8') as f:
        json.dump(lock_payload, f, ensure_ascii=False, indent=2)
    if verbose:
        _print_region_result(result)
    return result


def _print_region_result(r):
    print("=" * 60)
    print(f"[{r['region']}] IC {r['ic_mean']:+.4f} (IR {r['ic_ir']:+.3f})  n={r['ic_n']}")
    cp = r['column_perm']
    print(f"  column-perm z={cp['z']:+.2f} p={cp['p']:.3f}")
    dc = r['decay']
    print(f"  衰减: {dc['decay_direction']}  power={dc['power']}  n={dc['n_total']}")
    if r['topn']:
        print(f"  top-N 净超额 {r['topn']['net_annual']:+.1f}%/yr")
    print("=" * 60)


def corr_with_registered(F, env, registry_factors):
    return _max_corr_with_registered(F, env, registry_factors)


def evaluate_composite(composite_F, parts, env, train_end=None):
    train_end = train_end or env.calibration.dev_end
    comp = evaluate(composite_F, env, train_end=train_end)
    part_evals = {name: evaluate(G, env, train_end=train_end) for name, G in parts.items()}

    def _topnet(r):
        t = r.get('topn') if isinstance(r, dict) else None
        return t.get('net_annual', np.nan) if isinstance(t, dict) else np.nan

    L0 = {name: r.get('ic_ir_train', np.nan) for name, r in part_evals.items()}
    best_name = None
    best_icir = -np.inf
    best_net = -np.inf
    best_F = None
    for name, r in part_evals.items():
        icir = r.get('ic_ir_train', np.nan)
        net = _topnet(r)
        if np.isfinite(icir) and icir > best_icir:
            best_icir = icir
            best_name = name
            best_F = parts[name]
        if np.isfinite(net):
            best_net = max(best_net, net)

    comp_icir = comp.get('ic_ir_train', np.nan)
    comp_net = _topnet(comp)
    inc_icir = (comp_icir - best_icir) if np.isfinite(comp_icir) and np.isfinite(best_icir) else np.nan
    inc_net = (comp_net - best_net) if np.isfinite(comp_net) and np.isfinite(best_net) else np.nan
    corr_best = _max_corr_with_registered(composite_F, env, {best_name: best_F}) if best_F is not None else None

    return {
        "composite": comp,
        "parts": part_evals,
        "onion": {
            "L0_parts": L0,
            "L1_composite": comp_icir,
            "synthesis_gain": inc_icir,
        },
        "diagnosis": {
            "ic_ir_delta": inc_icir,
            "net_delta": inc_net,
            "best_part": best_name,
            "best_ic_ir": best_icir if np.isfinite(best_icir) else np.nan,
            "best_net_annual": best_net if np.isfinite(best_net) else np.nan,
            "corr_vs_best": corr_best,
        }
    }


def _sample_signal_days(F_dict, env):
    sig_idx = np.arange(0, env.T, env.calibration.sample_step)
    out = {}
    for n, F in F_dict.items():
        out[n] = np.asarray(F, dtype=np.float64)[sig_idx]
    return out


def evaluate_batch(F_dict, env, train_end=None):
    train_end = train_end or env.calibration.dev_end
    names = list(F_dict.keys())
    M = len(names)
    if M == 0:
        return {"factors": {}, "batch": {"M": 0}}

    factors = {n: evaluate(F_dict[n], env, train_end=train_end) for n in names}
    p_single = {}
    for n in names:
        cp = factors[n].get("column_perm_train") or {}
        p_single[n] = cp.get("p", np.nan)

    if M >= 2:
        Fs = _sample_signal_days(F_dict, env)
        corrs = []
        for i in range(M):
            for j in range(i + 1, M):
                c = cs_rank_corr(Fs[names[i]], Fs[names[j]])
                if np.isfinite(c):
                    corrs.append(abs(c))
        rho_bar = float(np.mean(corrs)) if corrs else 0.0
    else:
        rho_bar = 0.0

    N_eff = 1.0 + (M - 1) * (1.0 - rho_bar) if M >= 2 else 1.0

    deflated = {}
    for n in names:
        p = p_single[n]
        if np.isfinite(p):
            dp = 1.0 - (1.0 - p) ** N_eff
            deflated[n] = {"p_single": float(p), "deflated_p": float(dp), "survives": bool(dp < 0.05)}
        else:
            deflated[n] = {"p_single": None, "deflated_p": None, "survives": False}

    best_name = None
    best_ic_ir = -np.inf
    for n in names:
        ir = factors[n].get("ic_ir_train", np.nan)
        if np.isfinite(ir) and ir > best_ic_ir:
            best_ic_ir = ir
            best_name = n

    return {
        "factors": factors,
        "batch": {
            "M": M,
            "rho_bar": rho_bar,
            "N_eff": N_eff,
            "deflated": deflated,
            "best_name": best_name,
            "best_ic_ir": float(best_ic_ir) if np.isfinite(best_ic_ir) else None,
            "best_deflated_p": deflated[best_name]["deflated_p"] if best_name else None,
        }
    }


def passes_acceptance(result, z_threshold=3.0, beta_threshold=0.3, min_n=20, alpha=0.05):
    cp = result.get("column_perm_train") or {}
    z = cp.get("z", np.nan)
    beta = result.get("beta_exposure", np.nan)
    ic_n = result.get("ic_n_train", 0)

    if not np.isfinite(z):
        return False, "column-perm z 非有限"
    if abs(z) < z_threshold:
        return False, f"column-perm |z|={abs(z):.2f} < {z_threshold}（截面结构不显著）"
    if np.isfinite(beta) and abs(beta) >= beta_threshold:
        return False, f"beta 暴露 |{beta:.2f}| >= {beta_threshold}（beta 伪装嫌疑）"
    if ic_n < min_n:
        return False, f"样本不足 n={ic_n} < {min_n}"
    # 多重检验门控（2026-08-18 复核补全）：deflated p 是入册硬门——
    # 不显著 = 选择运气不可排除。此前 z≥3 的不显著因子照样 accepted=True。
    dp = result.get("deflated_train") or {}
    p, n_trials = dp.get("p"), dp.get("n_trials") or 1
    if p is None and isinstance(n_trials, (int, float)) and n_trials > 1:
        return False, (f"N={n_trials:.0f} 缺池分布基线（trail<10 且未 null 校准），"
                       "deflated p 不可算——先跑 null-calibration")
    if isinstance(p, (int, float)) and p > alpha:
        return False, (f"deflated p={p:.4f} > {alpha}（{n_trials:.0f} 次已试假设的"
                       "多重检验校正下不显著——选择运气不可排除）")
    return True, "pass"


def finalize_mining(candidate_F, env, verbose=True, state_root=None):
    if not candidate_F:
        return {"error": "候选池为空，无可验证因子"}

    batch = evaluate_batch(candidate_F, env, train_end=env.calibration.dev_end)
    best_name = batch["batch"]["best_name"]
    best_deflated_p = batch["batch"]["best_deflated_p"]

    selection = {}
    for n, F in candidate_F.items():
        r = evaluate_selection(F, env, verbose=False)
        selection[n] = r.get("ic_ir", np.nan)

    try:
        test_res = evaluate_test(candidate_F[best_name], env, verbose=False, state_root=state_root)
    except RuntimeError as e:
        test_res = {"error": str(e)}

    report = {
        "finalized_at_round": None,
        "best_candidate": best_name,
        "best_ic_ir_dev": batch["batch"]["best_ic_ir"],
        "best_deflated_p": best_deflated_p,
        "selection_ic_ir": selection,
        "batch": batch["batch"],
        "test": test_res,
    }
    if verbose:
        print("=" * 60)
        print(f"最终验证：最优候选 = {best_name}")
        print(f"  dev IC_IR = {report['best_ic_ir_dev']:+.3f}  deflated_p = {best_deflated_p}")
        print(f"  selection 区 IC_IR: " + " ".join(f"{n}:{v:+.3f}" for n, v in selection.items()))
        if isinstance(test_res, dict) and "error" not in test_res:
            print(f"  test IC_IR = {test_res.get('ic_ir', np.nan):+.3f}  衰减={test_res.get('decay', {}).get('decay_direction', '?')}")
            print(f"  test column-perm z = {test_res.get('column_perm', {}).get('z', np.nan):+.2f}")
        else:
            print(f"  test: {test_res}")
        print("=" * 60)
    return report


def evaluate_walk_forward(F, env, n_folds=5, t0_date=None, t1_date=None, verbose=False):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    t0_date = env.calibration.dev_end if t0_date is None else t0_date
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    if t0_date is not None:
        ic_sig = ic_sig[ic_sig.index >= pd.Timestamp(t0_date)]
    if t1_date is not None:
        ic_sig = ic_sig[ic_sig.index < pd.Timestamp(t1_date)]
    ic_sig = ic_sig.dropna()
    n = len(ic_sig)
    if n < n_folds * 2:
        return dict(error=f"区间样本不足（n={n} < {n_folds * 2}）")

    edges = np.linspace(0, n, n_folds + 1).astype(int)
    overall_mean = float(ic_sig.mean())
    sign = int(np.sign(overall_mean))
    per_fold = []
    same = 0
    for i in range(n_folds):
        seg = ic_sig.iloc[edges[i]:edges[i + 1]]
        st = _ic_stats(seg)
        per_fold.append({
            "t0": str(seg.index[0].date()), "t1": str(seg.index[-1].date()),
            "ic_mean": st["mean"], "ic_ir": st["ir"], "n": st["n"],
        })
        if np.sign(st["mean"]) == sign:
            same += 1

    result = {
        "n_folds": n_folds,
        "per_fold": per_fold,
        "per_fold_ic_ir": [f["ic_ir"] for f in per_fold],
        "per_fold_ic_mean": [f["ic_mean"] for f in per_fold],
        "fold_consistency": same / n_folds,
        "sign": sign,
        "overall_ic_mean": overall_mean,
    }
    if verbose:
        print("=" * 60)
        print(f"walk-forward 分块稳健性: {result['n_folds']} 块  跨块同号 {result['fold_consistency']*100:.0f}%  全样本 IC {result['overall_ic_mean']:+.4f}")
        for f in result['per_fold']:
            print(f"  {f['t0']}~{f['t1']}:  IC {f['ic_mean']:+.4f}  IR {f['ic_ir']:+.3f}  n={f['n']}")
        print("=" * 60)
    return result


def _print_result(r):
    print("=" * 60)
    print(f"IC: train {r['ic_mean_train']:+.4f} (IR {r['ic_ir_train']:+.3f})")
    yc = r['yearly_consistency_train']
    print(f"逐年符号一致性(train): {yc['consistent']*100:.0f}%")
    rl = r['rolling_ic_stability_train']
    if rl and np.isfinite(rl['stable']):
        print(f"滚动IC稳定性(train): {rl['stable']*100:.0f}% ({rl['n_windows']} 窗口)")
    dc = r.get('decay_train')
    if dc:
        intervals = ' '.join(f"{v:+.3f}" for v in dc.get('per_interval_ic', []))
        slope_str = f"{dc['yearly_slope']:+.4f}/yr" if np.isfinite(dc.get('yearly_slope')) else "n/a"
        print(f"衰减(train): {dc['decay_direction']}  power={dc['power']}  n={dc['n_total']}({dc['K']}区间×{dc['n_per_interval']})  Δ首末={dc['first_last_delta']:+.4f}  z={dc['delta_z']:+.2f}")
        if intervals:
            print(f"  区间IC: [{intervals}]  yearly_slope={slope_str}")
    cp = r['column_perm_train']
    print(f"column-perm(train): z={cp['z']:+.2f} p={cp['p']:.3f}")
    print(f"beta 暴露: {r['beta_exposure']:+.3f}  (|.|>0.3 警惕 beta 伪装)")
    if r['topn']:
        t = r['topn']
        print(f"top-N 超额(train): 净 {t['net_annual']:+.1f}%/yr  毛 {t['gross_annual']:+.1f}%/yr  换手 {t['turn_avg']:.2f}")
        yrs = ' '.join(f"{y}:{v:+.1f}" for y, v in t['yearly_net'].items())
        print(f"逐年净超额(%/yr): {yrs}")
        bb = t['block_bootstrap']
        print(f"block-bootstrap: z={bb['z']:+.2f} p={bb['p']:.3f}")
    print("=" * 60)
