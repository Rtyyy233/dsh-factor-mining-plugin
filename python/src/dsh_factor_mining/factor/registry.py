# coding=utf-8
"""Dual-library factor registry primitives (portable, no FactorEnv dependency)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def cs_rank_corr(F1, F2, min_obs=30):
    """Time-averaged cross-sectional rank correlation between two (T,N) matrices."""
    r1 = pd.DataFrame(np.asarray(F1, dtype=np.float64)).rank(axis=1, pct=True)
    r2 = pd.DataFrame(np.asarray(F2, dtype=np.float64)).rank(axis=1, pct=True)
    T = r1.shape[0]
    cs = []
    for t in range(T):
        m = np.isfinite(r1.iloc[t].values) & np.isfinite(r2.iloc[t].values)
        if m.sum() < min_obs:
            continue
        x = r1.iloc[t].values[m]
        y = r2.iloc[t].values[m]
        if x.std() == 0 or y.std() == 0:
            continue
        cs.append(np.corrcoef(x, y)[0, 1])
    return float(np.mean(cs)) if cs else np.nan


def curate_strong_subset(F_dict, ic_ir_dict, corr_threshold=0.7):
    """Greedily keep one strongest factor per highly-correlated cluster."""
    ranked = sorted(F_dict.keys(), key=lambda n: -ic_ir_dict.get(n, -np.inf))
    active = []
    for name in ranked:
        dup = False
        for aname in active:
            corr = cs_rank_corr(F_dict[name], F_dict[aname])
            if corr is not None and not np.isnan(corr) and abs(corr) >= corr_threshold:
                dup = True
                break
        if not dup:
            active.append(name)
    return active


def should_admit(new_F, new_ic_ir, active_F, active_ic_ir, corr_threshold=0.7):
    if not active_F:
        return {"admit": True, "reason": "首因子（强因子库为空），无条件准入",
                "corr_max": None, "most_similar": None}

    corr_max = None
    most_similar = None
    for name, F in active_F.items():
        c = cs_rank_corr(new_F, F)
        if c is None or np.isnan(c):
            continue
        if corr_max is None or abs(c) > abs(corr_max):
            corr_max = c
            most_similar = name

    if corr_max is None or abs(corr_max) < corr_threshold:
        return {"admit": True,
                "reason": f"独立（与最相似 {most_similar} 相关 {corr_max:+.3f} < {corr_threshold}），带来新信息",
                "corr_max": corr_max, "most_similar": most_similar}

    same_ic_ir = active_ic_ir.get(most_similar, -np.inf)
    if new_ic_ir > same_ic_ir:
        return {"admit": True,
                "reason": f"强换弱（与 {most_similar} 同源 corr={corr_max:+.3f}，但 IC_IR {new_ic_ir:+.3f} > {same_ic_ir:+.3f}）",
                "corr_max": corr_max, "most_similar": most_similar}
    return {"admit": False,
            "reason": f"同源弱因子（与 {most_similar} 同源 corr={corr_max:+.3f}，IC_IR {new_ic_ir:+.3f} ≤ {same_ic_ir:+.3f}）",
            "corr_max": corr_max, "most_similar": most_similar}


def should_admit_full(new_F, new_ic_ir, active_F, active_ic, raw_F, corr_threshold=0.7, raw_threshold=0.99):
    corr_raw = None
    most_similar_raw = None
    for name, F in raw_F.items():
        c = cs_rank_corr(new_F, F)
        if c is None or np.isnan(c):
            continue
        if corr_raw is None or abs(c) > abs(corr_raw):
            corr_raw = c
            most_similar_raw = name
    if corr_raw is not None and abs(corr_raw) >= raw_threshold:
        return {"admit": False,
                "reason": f"近重复/重推导（与原始库 {most_similar_raw} 相关 {corr_raw:+.3f} >= {raw_threshold}）",
                "corr_raw": corr_raw, "most_similar_raw": most_similar_raw,
                "corr_max": None, "most_similar": None}

    r = should_admit(new_F, new_ic_ir, active_F, active_ic, corr_threshold)
    r["corr_raw"] = corr_raw
    r["most_similar_raw"] = most_similar_raw
    return r
