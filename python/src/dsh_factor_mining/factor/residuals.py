# coding=utf-8
"""残差结构（调度器票 2c，PLAN-scheduler-task-brief-2026-09-29 §五）。

对已入册因子正交化后的残差度量——「剥掉账本已知维度后还剩什么」。
正交门（book_ranks.check_candidate）是冗余检查（值相关报告），本模块
是残差度量（把已知因子投影掉再量剩余信号）——同一账本的两个正交视角。

度量进 U 输入清单（默认权重 0，D2 拍板：2c 视 U 项贡献再定）：
- resid_ic：残差因子对前向收益的秩 IC（已知维度之外的信号强度）
- resid_autocorr：残差截面值的时序自相关（结构的规则性/周期性）

正交化在秩空间做（与正交门同空间）：逐日把 F 的截面秩对在册因子的
截面秩矩阵做最小二乘回归，取残差。无在册矩阵 → 全部度量 None（诚实
不可算，不硬算）。
"""
from __future__ import annotations

import numpy as np

from .regime import MIN_POOL, _pit_mask, _pct_rank, forward_returns


def _rank_matrix(F: np.ndarray) -> np.ndarray:
    """逐日截面百分位秩（NaN 保留——回归日按有效列对齐）。"""
    out = np.full_like(np.asarray(F, dtype=np.float64), np.nan)
    for t in range(out.shape[0]):
        row = F[t]
        ok = ~np.isnan(row)
        if ok.sum() < MIN_POOL:
            continue
        out[t, ok] = _pct_rank(row[ok])
    return out


def orthogonal_residual(F: np.ndarray, book: list[np.ndarray]) -> np.ndarray:
    """秩空间正交化：F_rank 逐日对 book_rank 矩阵 OLS 回归取残差。

    book: 在册因子的 (T,N) 值矩阵列表（日期轴与 F 对齐是调用方契约）。
    逐日回归只在该日 book 有效列 ≥2 且 F 有效列足够时执行；否则该日
    残差 = F 秩原值（诚实退化：该日无账本基线可比）。"""
    Fr = _rank_matrix(F)
    books = [_rank_matrix(b) for b in book]
    out = Fr.copy()
    for t in range(Fr.shape[0]):
        x = Fr[t]
        okf = ~np.isnan(x)
        cols = []
        for br in books:
            b = br[t]
            if (~np.isnan(b)).sum() >= 2:
                cols.append(b)
        if not cols or okf.sum() < MIN_POOL:
            continue
        B = np.column_stack(cols)
        mask = ~np.isnan(B).any(axis=1) & okf
        if mask.sum() < MIN_POOL:
            continue
        A = np.column_stack([B[mask], np.ones(mask.sum())])
        coef, *_ = np.linalg.lstsq(A, x[mask], rcond=None)
        resid = x[mask] - A @ coef
        out[t, mask] = resid
        out[t, ~mask] = np.nan  # 拟不进基线的列置 NaN（不冒充残差 0）
    return out


def residual_ic(F_resid: np.ndarray, env, horizon: int) -> float | None:
    """残差因子对前向收益的逐日秩 IC 均值（同 regime 模块口径）。"""
    fwd = forward_returns(env, horizon)
    pit = _pit_mask(env)
    ics = []
    for t in range(min(F_resid.shape[0], fwd.shape[0])):
        x, y = F_resid[t], fwd[t]
        ok = pit[t] & ~np.isnan(x) & ~np.isnan(y)
        if ok.sum() < MIN_POOL:
            continue
        xr, yr = _pct_rank(x[ok]), _pct_rank(y[ok])
        if xr.std() < 1e-12 or yr.std() < 1e-12:
            continue
        ics.append(float(np.corrcoef(xr, yr)[0, 1]))
    if len(ics) < 10:
        return None
    return float(np.mean(ics))


def residual_autocorr(F_resid: np.ndarray, lag: int = 1) -> float | None:
    """残差截面值的时序自相关：逐日截面均值序列的 lag 自相关。

    因子值本身有截面结构，自相关量在「日级汇总信号」上做（与
    tail.rank_autocorr 的换手口径互补）。"""
    counts = (~np.isnan(F_resid)).sum(axis=1)
    row_means = np.full(F_resid.shape[0], np.nan)
    valid = counts > 0
    if valid.any():
        with np.errstate(invalid="ignore"):
            row_means[valid] = np.nanmean(F_resid[valid], axis=1)
    s = row_means[~np.isnan(row_means)]
    if len(s) < 20:
        return None
    a, b = s[:-lag] - s[:-lag].mean(), s[lag:] - s[lag:].mean()
    denom = (a.std() * b.std())
    if denom < 1e-12:
        return None
    return float((a @ b) / (len(a) * denom))


def residual_metrics(F: np.ndarray, env, horizon: int,
                     book: list[np.ndarray] | None) -> dict:
    """U 输入清单的残差度量包（调度器消费口径）。

    book 为空/None → {"resid_ic": None, ..., "note": "无在册基线"}。"""
    if not book:
        return {"resid_ic": None, "resid_autocorr": None,
                "note": "无在册基线——残差度量不可算"}
    resid = orthogonal_residual(F, book)
    return {
        "resid_ic": residual_ic(resid, env, horizon),
        "resid_autocorr": residual_autocorr(resid),
        "n_book": len(book),
    }
