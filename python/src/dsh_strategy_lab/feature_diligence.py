# coding=utf-8
"""特征尽调体检（2026-08-28 用户要求：特征挑选除相关性/换手外，还看
衰减速度与信息稳健性）。

全部在 train 区（dev_end 前）计算——这是对**已入册**因子的消费侧
诊断，不触碰 selection/test 区（那两区各有纪律；本模块只读 env）。

量具：
- **decay_profile**：滞后 IC 剖面 IC(h)，h ∈ {1,5,10,20,60}——特征
  衰减速度的直接测量。half_life = |IC| 自峰值跌半的 log 线性插值点。
  为什么关键：特征衰减速度必须与 ML 标签 horizon 匹配——慢特征配短
  horizon 浪费信息，快特征配长 horizon 是噪声；换手率只是衰减的
  交易足迹（存在低换手但 alpha 已死的因子）。
- **robustness**：yearly_consistency（年度 IC 符号一致率）、
  rolling_pos_frac（滚动窗 IC 与总符号一致占比）、rolling_std、
  boot_z（block bootstrap 显著性）——WF 折间方差的上游预报。
"""
from __future__ import annotations

import numpy as np


def _rank(x):
    r = np.empty(len(x))
    r[np.argsort(x, kind="stable")] = np.arange(len(x))
    return r


def _ic_series(F, fwd, pit, t_end, min_n=30):
    """逐日截面 Spearman IC，行集 [0, t_end)。重叠窗口对均值估计无偏。"""
    F = np.asarray(F, dtype=np.float64)
    out_t, out_v = [], []
    for t in range(0, int(t_end)):
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        if int(m.sum()) < min_n:
            continue
        a, b = F[t][m], fwd[t][m]
        if a.std() == 0 or b.std() == 0:
            continue
        c = float(np.corrcoef(_rank(a), _rank(b))[0, 1])
        if np.isfinite(c):
            out_t.append(t)
            out_v.append(c)
    return np.array(out_t, dtype=int), np.array(out_v, dtype=float)


def _fwd_returns(env, h):
    c = np.asarray(env.c, dtype=np.float64)
    T = c.shape[0]
    fwd = np.full_like(c, np.nan)
    if 0 < h < T:
        fwd[:T - h] = c[h:] / c[:T - h] - 1.0
    return fwd


def _half_life(hs, ics):
    """|IC| 自峰值跌半的插值天数；全程不跌半 → None（记「慢于最大 h」）。"""
    import math

    pts = [(float(h), abs(float(v))) for h, v in zip(hs, ics)
           if v is not None and np.isfinite(v)]
    if len(pts) < 2:
        return None
    i_pk = int(np.argmax([p[1] for p in pts]))
    peak_h, peak_v = pts[i_pk]
    target = peak_v / 2.0
    for j in range(i_pk + 1, len(pts)):
        if pts[j][1] <= target:
            # log-h 线性插值（IC 对 log(h) 近似线性衰减是常用近似）
            h0, v0 = pts[j - 1]
            h1, v1 = pts[j]
            if v0 == v1:
                return round(h1, 1)
            frac = (v0 - target) / (v0 - v1)
            return round(math.exp(math.log(h0)
                                  + frac * (math.log(h1) - math.log(h0))), 1)
    return None


def battery(F, env, horizons=(1, 5, 10, 20, 60), h_ref=20,
            rolling_win=120, boot_draws=200, boot_block=20,
            seed=42) -> dict:
    """一个因子的体检卡：衰减剖面 + 稳健性（train 区）。

    h_ref = 稳健性量具的参考 horizon（默认 20，与生产主 horizon 对齐）。
    """
    import pandas as pd

    from dsh_factor_mining.factor.evaluate import _pit_mask

    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 形状 {F.shape} != {(env.T, env.N)}")
    dev_end = env.calibration.dev_end
    t_end = (int(np.searchsorted(env.dates, pd.Timestamp(dev_end)))
             if dev_end else int(env.T * 0.6))
    pit = _pit_mask(env)

    decay, decay_ics = {}, []
    for h in horizons:
        _, v = _ic_series(F, _fwd_returns(env, h), pit, t_end)
        val = round(float(np.mean(v)), 4) if len(v) else None
        decay[str(h)] = val
        decay_ics.append(val)

    ts, v = _ic_series(F, _fwd_returns(env, h_ref), pit, t_end)
    out = {"decay_ic": decay,
           "half_life_days": _half_life(horizons, decay_ics)}
    if len(v) < 60:
        out["n_ic_days"] = int(len(v))
        out["note"] = "train 区 IC 样本 <60——稳健性量具从缺"
        return out
    overall = float(np.mean(v))
    sgn = 1.0 if overall >= 0 else -1.0

    # 年度符号一致率
    years = {}
    for t, ic in zip(ts, v):
        years.setdefault(int(pd.Timestamp(env.dates[t]).year), []).append(ic)
    ym = [float(np.mean(x)) for x in years.values() if len(x) >= 20]
    out["yearly_consistency"] = (round(
        sum(1 for x in ym if x * sgn > 0) / len(ym), 3) if ym else None)
    out["n_years"] = len(ym)

    # 滚动窗一致率 + 波动
    k = int(rolling_win)
    wins = [float(v[i:i + k].mean()) for i in range(0, len(v) - k + 1, k // 2)]
    out["rolling_pos_frac"] = round(
        sum(1 for w in wins if w * sgn > 0) / len(wins), 3)
    out["rolling_std"] = round(float(np.std(wins, ddof=1)), 4)

    # block bootstrap z（均值显著性，块长抗自相关）
    rng = np.random.default_rng(seed)
    n = len(v)
    n_blocks = max(1, int(np.ceil(n / boot_block)))
    means = []
    for _ in range(int(boot_draws)):
        st = rng.integers(0, max(1, n - boot_block + 1), size=n_blocks)
        sample = np.concatenate([v[s:s + boot_block] for s in st])[:n]
        means.append(float(sample.mean()))
    se = float(np.std(means, ddof=1))
    out["boot_z"] = round(overall / se, 2) if se > 0 else None
    out["ic_mean_h_ref"] = round(overall, 4)
    out["n_ic_days"] = int(n)
    return out
