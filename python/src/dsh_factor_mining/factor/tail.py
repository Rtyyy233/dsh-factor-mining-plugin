# coding=utf-8
"""尾部维度三件套计算层（2026-08-25 用户批准设计；本层只标注不门——
准入与独立多重检验在 Phase 5）。

三件套各答一个问题（排序 claim ≠ 水平 claim，等权 top-N 盈利不需要
尾部内部有序）：

1. **尾部组差 spread_ir**（主指标雏形）：逐采样日 top-K 等权 fwd 均值
   − 池均值 → 跨日 IR。top-N 收益的因子层廉价版：水平 claim、
   long-only、K = 池的 20%（钉死不许扫——扫 K = 新试验进 tail 账本）
2. **尾部 IC**（long-side，仅诊断不设门）：top-K 子集内 rank IC——
   尾部内部能否排序。等权 top-N 用不上内部排序（G73 教训：rank 加权
   杀 edge）；对称构造（混入 bottom 尾）会误收防御因子，一切只取
   top 侧
3. **top-N 组合净超额 null 校准**：复用生产 `_top_n_excess`（含换手
   成本、PIT、T+1 口径）；新增 random-N placebo——逐日置换因子截面
   （同构造、同成本、同期）重放 R 次 → z_sim。null 不是 "IC=0" 而是
   「随机选股」

凸性检测：线性支付下 spread_t ∝ IC_t——corr(spread 序列, IC 序列)
应接近 1；系统性解耦 = 非线性（凸性/不对称）存在的直接证据。

区域纪律：全部只在 train 区（dates < dev_end）——test 是消耗品，
诊断也不触碰。

自动计算 = 自动计数（2026-08-25 用户决策）：evaluate/evaluate_batch
每个因子自动附带 tail 块——agent 不存在「选择性申报」的口子，
Phase 5 tail 账本据此累计。
"""
from __future__ import annotations

import numpy as np

from .env import FactorEnv

#: 主指标 K 占比（池的 20%，钉死；改动 = 新试验进 tail 账本）
K_FRAC = 0.2


def spread_ir_statistic(F, fwd, pit, sample_step, k_frac: float = K_FRAC):
    """尾部组差 IR 独立统计量（noise/day-perm 的 spread 版共用签名：
    (F, fwd, pit, step) → float | None）。全区间（无区域概念——
    噪声世界/置换面板上的区域边界无意义），与 tail_metrics 的
    train 区口径的差只影响绝对水平，同轨内比较不受影响。"""
    F = np.asarray(F, dtype=np.float64)
    spreads = []
    for t in range(0, F.shape[0], max(int(sample_step), 1)):
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        n = int(m.sum())
        if n < 8:
            continue
        k = max(1, int(round(k_frac * n)))
        top_idx = np.argsort(F[t][m])[-k:]
        spreads.append(float(fwd[t][m][top_idx].mean() - fwd[t][m].mean()))
    if len(spreads) < 5:
        return None
    s = np.std(spreads, ddof=1)
    return float(np.mean(spreads) / s) if s > 0 else None


def _train_end(env: FactorEnv) -> int:
    import pandas as pd

    return int(np.searchsorted(env.dates,
                               pd.Timestamp(env.calibration.dev_end)))


def _topk_block(F, fwd, pit, rows, k_frac):
    """逐采样日：top-K 组差 / top-K 内 IC / 全截面 IC（凸性对照）/
    top-K 名单 (day, 全局资产列) 对（Phase 5 名单 Jaccard 家族的原料）。"""
    from .tailgate import selection_minhash

    spreads, tail_ics, ics, ks = [], [], [], []
    pairs = []
    for t in rows:
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        n = int(m.sum())
        if n < 8:
            continue
        k = max(1, int(round(k_frac * n)))
        f = F[t][m]
        r = fwd[t][m]
        cols = np.where(m)[0]
        top_idx = np.argsort(f)[-k:]
        top_r = r[top_idx]
        spreads.append(float(top_r.mean() - r.mean()))
        ks.append(k)
        pairs.extend((int(t), int(cols[i])) for i in top_idx)
        rf = np.empty(n)
        rf[np.argsort(r, kind="stable")] = np.arange(n)
        ff = np.empty(n)
        ff[np.argsort(f, kind="stable")] = np.arange(n)
        rf = (rf - rf.mean()) / (rf.std() + 1e-12)
        ff = (ff - ff.mean()) / (ff.std() + 1e-12)
        ics.append(float(np.mean(rf * ff)))
        if k >= 4:
            rt = np.empty(k)
            rt[np.argsort(r[top_idx], kind="stable")] = np.arange(k)
            ft = np.empty(k)
            ft[np.argsort(f[top_idx], kind="stable")] = np.arange(k)
            rt = (rt - rt.mean()) / (rt.std() + 1e-12)
            ft = (ft - ft.mean()) / (ft.std() + 1e-12)
            tail_ics.append(float(np.mean(rt * ft)))
    return spreads, tail_ics, ics, ks, pairs


def tail_metrics(F: np.ndarray, env: FactorEnv, k_frac: float = K_FRAC,
                 placebo_draws: int | None = None,
                 placebo_seed: int = 20260825) -> dict | None:
    """尾部三件套（见模块 docstring）。截面样本不足（<10 日或 <8 资产）
    → None。placebo_draws 默认按面板规模自适应（大面板降抽样防每次
    evaluate 都跑重 placebo）。失败可由调用方捕获记 error，不阻断评估。"""
    from .evaluate import _forward_returns, _pit_mask, _top_n_excess

    F = np.asarray(F, dtype=np.float64)
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    t_end = _train_end(env)
    rows = np.arange(0, t_end, max(int(env.calibration.sample_step), 1))

    spreads, tail_ics, ics, ks, pairs = _topk_block(F, fwd, pit, rows, k_frac)
    if len(spreads) < 10:
        return None

    sp = np.array(spreads)
    sp_std = sp.std(ddof=1)
    spread_ir = float(sp.mean() / sp_std) if sp_std > 0 else None
    ti = np.array(tail_ics) if tail_ics else None
    tail_ic = (float(ti.mean() / ti.std(ddof=1))
               if ti is not None and len(ti) >= 5 and ti.std(ddof=1) > 0
               else None)
    ic_arr = np.array(ics)
    spread_ic_corr = (float(np.corrcoef(sp, ic_arr)[0, 1])
                      if len(ic_arr) >= 10 and ic_arr.std() > 0 and sp_std > 0
                      else None)

    # ---- top-N 净超额 + random-N placebo（复用生产 _top_n_excess）----
    topn = {}
    real = _top_n_excess(F, fwd, pit, env, t0=0, t1=t_end)
    if len(real) >= 8:
        real_net = float(real["net"].mean())
        topn["net_mean"] = round(real_net, 6) + 0.0
        topn["gross_mean"] = round(float(real["gross"].mean()), 6) + 0.0
        topn["turn_avg"] = round(float(real["turn"].mean()), 4) + 0.0
        topn["periods"] = int(len(real))
        # placebo 规模自适应：生产面板（~5M 单元格）降抽样
        if placebo_draws is None:
            placebo_draws = max(10, min(40, int(4e6 / max(F.size, 1))))
        rng = np.random.default_rng(placebo_seed)
        null_nets = []
        for _ in range(placebo_draws):
            Fp = F.copy()
            for t in rows:
                m = pit[t] & np.isfinite(Fp[t])
                n = int(m.sum())
                if n >= 2:
                    idx = np.where(m)[0]
                    Fp[t][idx] = Fp[t][idx][rng.permutation(n)]
            try:
                pl = _top_n_excess(Fp, fwd, pit, env, t0=0, t1=t_end)
            except Exception:
                continue
            if len(pl) >= 8:
                null_nets.append(float(pl["net"].mean()))
        topn["placebo_draws"] = len(null_nets)
        if len(null_nets) >= 5 and np.std(null_nets, ddof=1) > 0:
            topn["placebo_null_mean"] = round(float(np.mean(null_nets)), 6) + 0.0
            topn["placebo_z"] = round(
                float((real_net - np.mean(null_nets))
                      / np.std(null_nets, ddof=1)), 4) + 0.0

    def _r4(x):
        return None if x is None else round(float(x), 4) + 0.0

    # 名单 MinHash（Phase 5 家族几何：IC ρ 高的两因子尾部名单可大面积
    # 不重叠——尾部恰是分歧最大处；32 哈希签名 128B/试验）
    from .tailgate import selection_minhash
    return {
        "region": "train",
        "k_frac": k_frac,
        "k_typical": int(round(float(np.mean(ks)))) if ks else 0,
        "spread_ir": _r4(spread_ir),
        "spread_mean": round(float(sp.mean()), 6) + 0.0,
        "tail_ic": _r4(tail_ic),
        "spread_ic_corr": _r4(spread_ic_corr),
        "selection_mh": selection_minhash(pairs),
        "topn": topn,
    }
