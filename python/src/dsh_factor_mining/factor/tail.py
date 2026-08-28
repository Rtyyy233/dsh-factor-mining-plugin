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


def spread_turn_stats(F, fwd, pit, sample_step, k_frac: float = K_FRAC,
                      *, t_end: int | None = None,
                      cost: float | None = None) -> dict:
    """spread_ir_statistic 全家福版（2026-08-28 换手率定价规划 WS-T1/T2）：
    一次遍历同时产出组差 IR、钉死规则换手（相邻 top-K 集对称差 / 2K，
    单边口径，首期 1.0——与 _top_n_excess 同式）、成本后 net 统计与
    break-even 成本 c*。evaluate 尾块 / 噪声世界 G2 / null 校准 / 轻量
    扫描共用此一处实现，杜绝多循环口径漂移。

    cost=None → 毛口径（net_* 缺省，行为与旧版逐位一致）；给定 →
    net 序列 = spread − 2·cost·turn（单边成本 × 双边 × 单边换手，
    _top_n_excess 同式）。c* = mean(spread)/(2·mean(turn))（单边 bps）
    ——每单位换手毛利，免假设可比；毛均 ≤0 或零换手 → None（不可比
    如实缺省，不造负数误导漏斗排序）。"""
    F = np.asarray(F, dtype=np.float64)
    end = F.shape[0] if t_end is None else min(int(t_end), F.shape[0])
    spreads, turns = [], []
    prev: set | None = None
    for t in range(0, end, max(int(sample_step), 1)):
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        n = int(m.sum())
        if n < 8:
            continue
        k = max(1, int(round(k_frac * n)))
        top_idx = np.argsort(F[t][m])[-k:]
        cols = set(np.where(m)[0][top_idx].tolist())
        spreads.append(float(fwd[t][m][top_idx].mean() - fwd[t][m].mean()))
        turns.append(1.0 if prev is None else len(cols ^ prev) / (2.0 * k))
        prev = cols
    out: dict = {"n_periods": len(spreads)}
    if len(spreads) < 5:
        out["spread_ir"] = None
        return out
    sp = np.array(spreads)
    s = sp.std(ddof=1)
    out["spread_ir"] = float(sp.mean() / s) if s > 0 else None
    tn = np.array(turns)
    out["gross_mean"] = float(sp.mean())
    out["turn_avg"] = float(tn.mean())
    if tn.mean() > 0 and sp.mean() > 0:
        out["break_even_cost"] = round(
            float(sp.mean() / (2.0 * tn.mean()) * 1e4), 1) + 0.0
    else:
        out["break_even_cost"] = None
    if cost is not None:
        net = sp - 2.0 * float(cost) * tn
        out["net_mean"] = float(net.mean())
        ns = net.std(ddof=1)
        out["net_spread_ir"] = float(net.mean() / ns) if ns > 0 else None
    return out


def spread_ir_statistic(F, fwd, pit, sample_step, k_frac: float = K_FRAC,
                        *, t_end: int | None = None,
                        cost: float | None = None):
    """尾部组差 IR 独立统计量（noise/day-perm 的 spread 版共用签名：
    (F, fwd, pit, step) → float | None）。t_end=None 全区间（无区域概念——
    噪声世界/置换面板上的区域边界无意义），与 tail_metrics 的 train 区
    口径的差只影响绝对水平，同轨内比较不受影响；t_end 给定 → 只用
    [0, t_end) 行（WS1：null 校准的 spread 段与 tail_metrics 同口径，
    K = round(0.2·n)，行集 arange(0, t_end, sample_step)）。
    cost（WS-T2）：net 模式——G2 噪声门在 net 基下把合成世界的组差
    同样按 2·cost·turn 净掉（与 G3 判定口径一致，一个成本模型）。"""
    return spread_turn_stats(F, fwd, pit, sample_step, k_frac,
                             t_end=t_end, cost=cost).get("spread_ir")


def rank_autocorr(F, pit, rows, lag: int):
    """截面秩自相关（t vs t+lag 的秩 Pearson，跨采样行平均）——信号
    持续性诊断，预测任何组合规则下的换手（rank autocorrelation 超时
    事故的正名：想法对，实现必须向量化——行内 argsort，生产面板毫秒
    级）。lag = horizon（持仓期尺度）；有效行 <5 → None。"""
    F = np.asarray(F, dtype=np.float64)
    T = F.shape[0]
    acs = []
    for t in rows:
        t2 = int(t) + int(lag)
        if t2 >= T:
            break
        m = pit[t] & pit[t2] & np.isfinite(F[t]) & np.isfinite(F[t2])
        n = int(m.sum())
        if n < 8:
            continue
        a = F[t][m]
        b = F[t2][m]
        ra = np.empty(n)
        ra[np.argsort(a, kind="stable")] = np.arange(n)
        rb = np.empty(n)
        rb[np.argsort(b, kind="stable")] = np.arange(n)
        if ra.std() == 0 or rb.std() == 0:
            continue
        c = float(np.corrcoef(ra, rb)[0, 1])
        if np.isfinite(c):
            acs.append(c)
    return float(np.mean(acs)) if len(acs) >= 5 else None


def topn_placebo(F, fwd, pit, env, t_end, draws, seed,
                 budget_secs: float | None = None) -> dict:
    """top-N 净超额 vs 随机选股 placebo（WS2 2026-08-25 公共抽取：
    evaluate 轻量版与 submit 权威版共用——一处实现两处口径）。

    逐采样日置换因子截面（同构造/同成本/同期）重放 draws 次 → null
    net 分布 → z。budget_secs 给 submit 权威版：跑满 10 次后按实测
    均时外推，超预算且 ≥60 已跑即截断（60 下限保证 z 的 σ 估计误差
    ≤ ~9%——evaluate 轻量版 10 draws 的 ~24% 不可作硬判据）。
    截断不是失败，是如实样本量（draws 字段报实际值）。

    返回 dict：periods/net_mean/gross_mean/turn_avg/draws（有效数）/
    null_mean/null_std/z（不足 5 或零离散时缺省）+ truncated 标注。
    """
    import time as _time

    from .evaluate import _top_n_excess
    F = np.asarray(F, dtype=np.float64)
    rows = np.arange(0, int(t_end), max(int(env.calibration.sample_step), 1))
    real = _top_n_excess(F, fwd, pit, env, t0=0, t1=t_end)
    if len(real) < 8 or int(draws) < 5:
        return {"periods": int(len(real)), "draws": 0,
                "note": "real 组合期 <8 或 draws <5——placebo 无从算"}
    real_net = float(real["net"].mean())
    rng = np.random.default_rng(seed)
    null_nets = []
    truncated = False
    t_start = _time.monotonic()
    target = int(draws)
    i = 0
    while i < target:
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
            pl = None
        if pl is not None and len(pl) >= 8:
            null_nets.append(float(pl["net"].mean()))
        i += 1
        if budget_secs is not None and i >= 10 and i < target:
            elapsed = _time.monotonic() - t_start
            per = elapsed / i
            if (elapsed + per * (target - i) > budget_secs
                    and len(null_nets) >= 60):
                truncated = True
                break
    out = {
        "net_mean": round(real_net, 6) + 0.0,
        "gross_mean": round(float(real["gross"].mean()), 6) + 0.0,
        "turn_avg": round(float(real["turn"].mean()), 4) + 0.0,
        "periods": int(len(real)),
        "draws": len(null_nets),
    }
    if truncated:
        out["truncated"] = True
        out["note"] = (f"预算自适应截断（{target}→{len(null_nets)} draws，"
                       f"预算 {budget_secs:.0f}s）——截断不是失败，draws 如实报")
    if len(null_nets) >= 5 and np.std(null_nets, ddof=1) > 0:
        out["null_mean"] = round(float(np.mean(null_nets)), 6) + 0.0
        out["null_std"] = round(float(np.std(null_nets, ddof=1)), 6) + 0.0
        out["z"] = round((real_net - np.mean(null_nets))
                         / np.std(null_nets, ddof=1), 4) + 0.0
    return out


def _train_end(env: FactorEnv) -> int:
    import pandas as pd

    return int(np.searchsorted(env.dates,
                               pd.Timestamp(env.calibration.dev_end)))


def _topk_block(F, fwd, pit, rows, k_frac):
    """逐采样日：top-K 组差 / top-K 内 IC / 全截面 IC（凸性对照）/
    top-K 名单 (day, 全局资产列) 对（Phase 5 名单 Jaccard 家族的原料）/
    相邻 top-K 集换手（2026-08-28 换手率定价：对称差 / 2K，单边口径，
    首期 1.0——无效行跳过时持有集沿用上一有效行，与 _top_n_excess 同
    纪律）。"""
    spreads, tail_ics, ics, ks = [], [], [], []
    pairs = []
    turns = []
    prev_top: set | None = None
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
        top_set = {int(cols[i]) for i in top_idx}
        pairs.extend((int(t), int(cols[i])) for i in top_idx)
        turns.append(1.0 if prev_top is None
                     else len(top_set ^ prev_top) / (2.0 * k))
        prev_top = top_set
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
    return spreads, tail_ics, ics, ks, pairs, turns


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

    spreads, tail_ics, ics, ks, pairs, turns = _topk_block(
        F, fwd, pit, rows, k_frac)
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

    # spread 高阶矩（WS4 2026-08-25）：G3 的 t 分母偏度/峰度修正原料——
    # 与 IC 线 _dsr_p_from_stats 同构（g3/g4 = 总体矩 / 样本 std(ddof=1)
    # 的幂；矩从完整序列算非 sketch，round 6 位；条目增量 ~60B）。
    # 只存矩不存序列：尾线族几何用名单 MinHash，spread 序列无账本用途。
    spread_moments = None
    if sp_std > 0:
        mu = float(sp.mean())
        spread_moments = {
            "g3": round(float(np.mean((sp - mu) ** 3)) / sp_std ** 3, 6) + 0.0,
            "g4": round(float(np.mean((sp - mu) ** 4)) / sp_std ** 4, 6) + 0.0,
            "n": int(len(sp)),
        }

    # ---- 换手率定价（2026-08-28 规划书 WS-T1/T2 v1 双报）----
    # v1 判定基照旧毛口径（tail_net_basis=false）；net 三件套陪跑进
    # 响应/账本/registry（紧凑投影带 c*），WS1 net 段重校后切基。
    # 成本 = env.calibration.cost（与 _top_n_excess 同源，一个成本模型）；
    # 版本串来自 MINING_CONFIG——变更 = 尾账本新键（结果不可比，理应重计）。
    turn_arr = np.array(turns) if turns else np.array([np.nan])
    turn_tail = float(turn_arr.mean()) if len(turns) else None
    cost = float(getattr(env.calibration, "cost", 0.0) or 0.0)
    net_spread_ir = None
    net_spread_moments = None
    if sp_std > 0 and len(turns) == len(sp):
        net = sp - 2.0 * cost * turn_arr
        ns = net.std(ddof=1)
        if ns > 0:
            net_spread_ir = float(net.mean() / ns)
            nmu = float(net.mean())
            net_spread_moments = {
                "g3": round(float(np.mean((net - nmu) ** 3)) / ns ** 3, 6) + 0.0,
                "g4": round(float(np.mean((net - nmu) ** 4)) / ns ** 4, 6) + 0.0,
                "n": int(len(net)),
            }
    break_even_cost = None
    if turn_tail is not None and turn_tail > 0 and float(sp.mean()) > 0:
        break_even_cost = round(
            float(sp.mean()) / (2.0 * turn_tail) * 1e4, 1) + 0.0
    try:
        from ..state import MINING_CONFIG as _MC
        _cmv = str(_MC.get("cost_model_version", "flat:v1"))
    except Exception:
        _cmv = "flat:v1"

    # ---- top-N 净超额 + random-N placebo（复用生产 _top_n_excess）----
    # placebo 走公共 topn_placebo（WS2：evaluate 轻量版与 submit 权威版
    # 共用一处实现）；本层保持轻量自动计数语义不变（无预算参数）
    topn = {}
    if placebo_draws is None:
        placebo_draws = max(10, min(40, int(4e6 / max(F.size, 1))))
    pl = topn_placebo(F, fwd, pit, env, t_end, int(placebo_draws),
                      placebo_seed)
    if pl.get("periods", 0) >= 8:
        for k in ("net_mean", "gross_mean", "turn_avg", "periods"):
            topn[k] = pl.get(k)
        topn["placebo_draws"] = pl.get("draws", 0)
        if 0 < pl.get("draws", 0) < 20:
            # σ 估计相对误差 ~ 1/√(2(n-1))：n=10 时 ~24%——z≥3 硬判据
            # 建立在这么薄的 null 上只能是弱证据，G1 判定方（tailgate/人工）
            # 需要看见样本数；权威判定在 submit 侧重跑（WS2）
            topn["placebo_warning"] = (
                f"null draws 仅 {pl['draws']}（<20）——placebo_z 的 σ "
                "估计误差大（~1/√(2n)），G1 判定视为弱证据"
                "（submit 侧重跑加厚，见 placebo_m）")
        if pl.get("z") is not None:
            topn["placebo_null_mean"] = pl["null_mean"]
            topn["placebo_z"] = pl["z"]

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
        "spread_moments": spread_moments,
        "tail_ic": _r4(tail_ic),
        "spread_ic_corr": _r4(spread_ic_corr),
        "selection_mh": selection_minhash(pairs),
        "topn": topn,
        # 换手定价（WS-T1/T2）：turn/net/c* 陪跑 + 成本口径戳——
        # 尾块自带口径元数据，账本/registry/审计不需再猜
        "turn_tail": _r4(turn_tail),
        "net_spread_ir": _r4(net_spread_ir),
        "net_spread_moments": net_spread_moments,
        "break_even_cost": break_even_cost,
        "rank_autocorr": _r4(rank_autocorr(F, pit, rows,
                                           int(env.calibration.horizon))),
        "cost_used": round(cost * 1e4, 1) + 0.0,
        "cost_model_version": _cmv,
    }
