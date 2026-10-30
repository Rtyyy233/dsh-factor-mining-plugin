# -*- coding: utf-8 -*-
"""残差结构（调度器票 2c）：对已入册正交化后的残差度量。"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.factor.regime import forward_returns  # 口径共享
from dsh_factor_mining.factor.residuals import (
    orthogonal_residual, residual_autocorr, residual_ic, residual_metrics)

T, N = 600, 50
H = 10


def _world(seed=7):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.01, (T, N))
    c = 100 * np.cumprod(1 + ret, axis=0)
    o = c * 1.001
    h, l = np.maximum(o, c) * 1.01, np.minimum(o, c) * 0.99
    v = np.abs(rng.normal(1e5, 1e4, (T, N)))

    class _E:
        pass
    e = _E()
    e.c, e.h, e.l = c, h, l
    fwd = forward_returns(e, H)
    # 在册因子 = 强横截面信号；候选 = 在册 + 独立中强信号
    book = fwd[:-H] * 0.8 + rng.normal(0, 0.004, (T - H, N)) * 0
    book = np.vstack([book, np.full((H, N), np.nan)])
    indep = np.vstack([fwd[H:] * 0.6 + rng.normal(0, 0.004, (T - H, N)),
                       np.full((H, N), np.nan)])
    cand = book * 1.0 + indep
    return e, book, indep, cand


def _daily_rank_corr(A, B):
    """逐日截面秩相关均值（与正交门同口径的检查量）。"""
    from dsh_factor_mining.factor.regime import _pit_mask, _pct_rank
    ics = []
    for t in range(min(len(A), len(B))):
        x, y = A[t], B[t]
        ok = ~np.isnan(x) & ~np.isnan(y)
        if ok.sum() < 30:
            continue
        xr, yr = _pct_rank(x[ok]), _pct_rank(y[ok])
        if xr.std() < 1e-12 or yr.std() < 1e-12:
            continue
        ics.append(float(np.corrcoef(xr, yr)[0, 1]))
    return float(np.mean(ics)) if ics else None


def test_orthogonal_residual_strips_book():
    """正交化口径：候选=在册+扰动 → 残差与在册因子的秩相关归零（剥干净）；
    纯在册复制 → 残差 IC 塌零（已知维度之外无信号）。"""
    e, book, indep, cand = _world()
    resid = orthogonal_residual(cand, [book])
    r_before = _daily_rank_corr(cand, book)
    r_after = _daily_rank_corr(resid, book)
    assert r_before is not None and r_after is not None
    assert abs(r_before) > 0.5, f"构造的候选与在册强相关: {r_before}"
    assert abs(r_after) < 0.1, f"残差必须把在册维度剥干净: {r_after}"
    # 纯在册复制（无独立分量）→ 残差能量塌缩（回归残差≈0）
    resid2 = orthogonal_residual(book * 1.5 + 0.0, [book])
    energy = float(np.nanstd(resid2)) / float(np.nanstd(book * 1.5))
    assert energy < 0.01, f"纯在册复制的残差能量必须塌缩: {energy}"


def test_residual_metrics_package():
    e, book, indep, cand = _world()
    m = residual_metrics(cand, e, H, book=[book])
    assert m["resid_ic"] is not None and m["n_book"] == 1
    assert "resid_autocorr" in m
    m_empty = residual_metrics(cand, e, H, book=None)
    assert m_empty["resid_ic"] is None and "不可算" in m_empty["note"], \
        "无在册基线 → 诚实不可算"


def test_residual_autocorr_detects_persistence():
    rng = np.random.default_rng(2)
    ar = np.zeros((T, N))
    ar[0] = rng.normal(0, 1, N)
    for t in range(1, T):
        ar[t] = 0.9 * ar[t - 1] + 0.1 * rng.normal(0, 1, N)
    white = rng.normal(0, 1, (T, N))
    assert residual_autocorr(ar) > 0.8
    assert abs(residual_autocorr(white)) < 0.3
