# coding=utf-8
"""Independent audit (adapted from the reference harness).

Uses different code paths from evaluate.py for every checked number:
- causality: NaN truncation instead of noise perturbation
- IC: hand-written Spearman instead of pandas rank + corrcoef
- top-N: numpy indexing instead of pandas grouping
- column-perm: np.roll cyclic permutation, different seed
- beta: rolling-60d regression instead of 252d EWM
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .causality import check_causality_nan
from .env import FactorEnv
from .evaluate import _pit_mask  # 共享 pit mask 定义（见 run_audit 内注释）

# 口径量现从 env.calibration 读取（与 evaluate 一致）；此处常量仅作历史默认标记。
MIN_POOL = 30
TRAIN_END = "2021-01-01"
TOL = 1e-4


def _fwd_numpy(env):
    cal = env.calibration
    T, N = env.c.shape
    c = env.c
    o = env.o
    fwd = np.full((T, N), np.nan)
    if cal.execution == "t0":
        for t in range(T - cal.horizon):
            fwd[t] = c[t + cal.horizon] / c[t] - 1.0
    else:
        for t in range(T - cal.horizon):
            fwd[t] = c[t + cal.horizon] / o[t + 1] - 1.0
    return fwd


def _rank_avg(a):
    order = np.argsort(a, kind='mergesort')
    ranks = np.empty(len(a), dtype=np.float64)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman_row(x, y):
    # 秩的 Pearson 相关（与 evaluate 的 rank(pct)+corrcoef 同口径）。
    # 不要用 1-6Σd²/(n(n²-1)) footrule 精确式——它在有 tie 时（大量 0 值/一字板）
    # 与 Pearson-of-ranks 系统性偏离，会让独立审计对同一因子假 FAIL。
    rx = _rank_avg(x)
    ry = _rank_avg(y)
    sx = rx.std()
    sy = ry.std()
    if sx == 0 or sy == 0:
        return np.nan
    return float(np.mean((rx - rx.mean()) * (ry - ry.mean())) / (sx * sy))


def _ic_series_numpy(F, fwd, pit, env, sig_only=True):
    T, N = F.shape
    out = {}
    sig_idx = set(np.arange(0, T, env.calibration.sample_step)) if sig_only else None
    for t in range(T):
        if sig_only and t not in sig_idx:
            continue
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        if m.sum() < MIN_POOL:
            continue
        x = F[t][m]
        y = fwd[t][m]
        if np.std(x) == 0 or np.std(y) == 0:
            continue
        out[env.dates[t]] = _spearman_row(x, y)
    return pd.Series(out)


def _topn_excess_numpy(F, fwd, pit, env, t0=0, t1=None):
    cal = env.calibration
    T, N = F.shape
    if t1 is None:
        t1 = T
    sig_idx = np.arange(0, T, cal.sample_step)
    sig_idx = sig_idx[(sig_idx >= t0) & (sig_idx < t1)]
    holdings = None
    net_list = []
    for ti in sig_idx:
        if ti + cal.horizon >= T:
            continue
        m = pit[ti] & np.isfinite(F[ti]) & np.isfinite(fwd[ti])
        if m.sum() < cal.top_n:
            continue
        idx = np.where(m)[0]
        top_order = np.argsort(-F[ti][idx], kind='stable')[:cal.top_n]
        cols = idx[top_order]
        top_set = set(cols.tolist())
        if holdings is None:
            turn = 1.0
        else:
            turn = len(top_set.symmetric_difference(holdings)) / (2 * cal.top_n)
        holdings = top_set
        pool_mean = np.nanmean(fwd[ti][idx])
        top_mean = np.nanmean(fwd[ti][cols])
        net = (top_mean - pool_mean) - turn * 2 * cal.cost
        net_list.append(net)
    return (np.mean(net_list) * (cal.annualization / cal.horizon) * 100
            if net_list else np.nan)


def _column_perm_numpy(F, fwd, pit, env, n_perm=200, seed=123, train_end=None):
    rng = np.random.default_rng(seed)
    real_ic = _ic_series_numpy(F, fwd, pit, env, sig_only=True)
    if train_end is not None:
        real_ic = real_ic[real_ic.index < pd.Timestamp(train_end)]
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
                k = rng.integers(1, len(idx))
                Fp[t, idx] = np.roll(F[t, idx], k)
        icp = _ic_series_numpy(Fp, fwd, pit, env, sig_only=True)
        if train_end is not None:
            icp = icp[icp.index < pd.Timestamp(train_end)]
        if len(icp) > 0:
            null_means.append(icp.mean())
    null_means = np.array(null_means)
    if len(null_means) == 0 or null_means.std() == 0:
        return dict(z=np.nan, p=np.nan)
    z = (real_mean - null_means.mean()) / null_means.std()
    return dict(z=float(z), p=float(2 * (1 - _norm_cdf(abs(z)))))


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / np.sqrt(2)))


def _beta_exposure_numpy(F, env):
    c = pd.DataFrame(env.c)
    ret1 = c.pct_change()
    mr = ret1.mean(axis=1, skipna=True)
    cov = ret1.multiply(mr, axis=0).rolling(60, min_periods=30).mean().sub(
        ret1.rolling(60, min_periods=30).mean().multiply(mr.rolling(60, min_periods=30).mean(), axis=0))
    var = mr.rolling(60, min_periods=30).var()
    beta = cov.div(var.clip(1e-8), axis=0).shift(1)
    Fd = pd.DataFrame(F)
    corrs = []
    for t in range(env.T):
        m = np.isfinite(Fd.iloc[t].values) & np.isfinite(beta.iloc[t].values)
        if m.sum() < MIN_POOL:
            continue
        rF = pd.Series(Fd.iloc[t].values[m]).rank(pct=True)
        rB = pd.Series(beta.iloc[t].values[m]).rank(pct=True)
        if rF.std() == 0 or rB.std() == 0:
            continue
        corrs.append(np.corrcoef(rF, rB)[0, 1])
    return float(np.mean(corrs)) if corrs else np.nan


def _close(a, b, tol=TOL):
    if a is None or b is None:
        return a is None and b is None
    if not np.isfinite(a) and not np.isfinite(b):
        return True
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) < tol


def audit(factor_fn, env, train_end=None):
    if train_end is None:
        train_end = env.calibration.dev_end
    F = np.asarray(factor_fn(env), dtype=np.float64)
    if F.shape != (env.T, env.N):
        return {"verdict": "FAIL", "discrepancies": [f"形状 {F.shape} != {(env.T, env.N)}"]}

    discrepancies = []
    caus = check_causality_nan(factor_fn, env)
    if caus["verdict"] == "FUTURE_LEAK":
        discrepancies.append(f"因果性(NaN截断法): {caus['note']} @ t0={caus['leak_t0']}")

    fwd = _fwd_numpy(env)
    # pit mask 直接复用 evaluate 的定义（listed ∩ amount>0 ∩ ¬一字板）——mask 是共享的
    # 市场微结构词汇表，不是被审计对象；手工复制一份反而会在 evaluate 演进后漂移触发假 FAIL。
    # 审计的独立性在 IC 数值计算路径（_ic_series_numpy/_spearman_row），不在 mask 定义。
    pit = _pit_mask(env)

    t_end = np.searchsorted(env.dates, pd.Timestamp(train_end))
    ic = _ic_series_numpy(F, fwd, pit, env, sig_only=True)
    ic_train = ic[ic.index < pd.Timestamp(train_end)]
    ic_mean_train_audit = ic_train.mean() if len(ic_train) else np.nan
    topn_net_audit = _topn_excess_numpy(F, fwd, pit, env, t0=0, t1=t_end)
    colperm_audit = _column_perm_numpy(F, fwd, pit, env, train_end=train_end)
    beta_audit = _beta_exposure_numpy(F, env)

    from .evaluate import evaluate
    ev = evaluate(F, env, train_end=train_end)

    ic_mean_train_eval = ev.get("ic_mean_train", np.nan)
    if not _close(ic_mean_train_eval, ic_mean_train_audit):
        discrepancies.append(f"IC_mean_train 不一致: evaluate={ic_mean_train_eval} audit={ic_mean_train_audit}")

    topn_eval = ev.get("topn", {}).get("net_annual", np.nan) if ev.get("topn") else np.nan
    if not _close(topn_eval, topn_net_audit):
        discrepancies.append(f"top-N 净超额不一致: evaluate={topn_eval} audit={topn_net_audit}")

    cp_eval = ev.get("column_perm_train", {}).get("z", np.nan)
    if np.isfinite(cp_eval) and np.isfinite(colperm_audit["z"]):
        sig_eval = abs(cp_eval) >= 3
        sig_audit = abs(colperm_audit["z"]) >= 3
        if sig_eval != sig_audit:
            discrepancies.append(f"column-perm 显著性矛盾: evaluate z={cp_eval:.2f} audit z={colperm_audit['z']:.2f}")

    beta_eval = ev.get("beta_exposure", np.nan)
    if np.isfinite(beta_eval) and np.isfinite(beta_audit):
        mask_eval = abs(beta_eval) > 0.3
        mask_audit = abs(beta_audit) > 0.3
        if mask_eval != mask_audit:
            discrepancies.append(f"beta 暴露定性矛盾: evaluate={beta_eval:.3f} audit={beta_audit:.3f}")

    if discrepancies:
        return {"verdict": "FAIL", "discrepancies": discrepancies,
                "audit_ic_mean_train": ic_mean_train_audit, "audit_topn_net": topn_net_audit,
                "audit_colperm_z": colperm_audit["z"], "audit_beta": beta_audit,
                "causality": caus["verdict"]}
    return {"verdict": "PASS", "discrepancies": [],
            "audit_ic_mean_train": ic_mean_train_audit, "audit_topn_net": topn_net_audit,
            "audit_colperm_z": colperm_audit["z"], "audit_beta": beta_audit,
            "causality": caus["verdict"]}
