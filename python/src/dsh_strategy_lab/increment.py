# coding=utf-8
"""G4′ 增量门：策略必须证明 overlay 在成本后加了增量。

基准 = **被动 top-K 持仓**（所引因子、同再平衡期、同费用）——因子层
已测过的东西；策略的增量 = 同一模拟器下 (策略 − 基线) 的日收益差，
block bootstrap CI 下界 > 0 才过。多维报告（Sharpe/MDD/换手/容量/
delta）不坍缩（PASS-FAIL 教训）。

block bootstrap 复用因子层 evaluate._block_bootstrap（同一数学：
circular block、z = mu/se）——复制 = 两处维护，直接 import。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dsh_factor_mining.factor.evaluate import _block_bootstrap

from .simulator import DEFAULT_COST_MODEL, simulate


def combine_factor_matrices(F_list: list, env) -> np.ndarray:
    """多因子被动基线的合成：逐 bar 截面 pct 秩取均值（秩平均——
    量纲无关，所引因子各自已过自己的门）。"""
    import numpy as np

    if not F_list:
        raise ValueError("F_list 为空——被动基线需要至少一个已准入因子")
    if len(F_list) == 1:
        return np.asarray(F_list[0], dtype=np.float64)
    rank_sum = np.zeros((env.T, env.N))
    for F in F_list:
        Fr = pd.DataFrame(np.asarray(F, dtype=np.float64))
        rank_sum += Fr.rank(axis=1, pct=True).values
    return rank_sum / len(F_list)


def passive_topk_path(F, env, top_k: int = 10,
                      rebalance_every: int = 5) -> list[dict]:
    """因子矩阵 F (T,N) → 被动 top-K 等权路径（同再平衡期参数）。

   再平衡 bar 上取 PIT 可交易截面（listed & 有限）按 F 排序 top-K 等权
   （1/k，不杠杆、余量现金）；再平衡之间复述上一目标（被动持有——
   与策略契约同语义）。首根 bar 即建仓（bar0 收盘信号 → bar1 开盘成交，
   与策略同一执行时点）。"""
    F = np.asarray(F, dtype=np.float64)
    listed = env.listed if env.listed is not None else np.ones(F.shape, bool)
    path: list[dict] = []
    cur: dict = {}
    for t in range(env.T):
        if t % max(int(rebalance_every), 1) == 0:
            m = listed[t] & np.isfinite(F[t])
            k = min(int(top_k), int(m.sum()))
            if k >= 1:
                order = np.argsort(-F[t][m], kind="stable")[:k]
                cols = np.where(m)[0][order]
                cur = {env.symbols[j]: 1.0 / k for j in cols}
            else:
                cur = {}
        path.append(dict(cur))
    return path


def increment_report(env, strategy_path: list[dict],
                     baseline_path: list[dict],
                     cost_model=None, block_L: int = 6) -> dict:
    """同模拟器双跑 + 日收益差 block bootstrap（CI 下界 > 0 = 过）。"""
    cm = cost_model or DEFAULT_COST_MODEL
    sim_s = simulate(env, strategy_path, cm)
    sim_b = simulate(env, baseline_path, cm)
    rs = np.array(sim_s["daily_returns"], dtype=np.float64)
    rb = np.array(sim_b["daily_returns"], dtype=np.float64)
    n = min(len(rs), len(rb))
    delta = rs[:n] - rb[:n]
    bb = _block_bootstrap(delta.tolist(), L=block_L)
    ms, mb = sim_s["metrics"], sim_b["metrics"]
    ds = ((ms["sharpe"] - mb["sharpe"])
          if (ms["sharpe"] is not None and mb["sharpe"] is not None)
          else None)
    # 容量只报告不建门（规划 §9）：名义容量 ≈ 平均日成交额 / 单日单边换手
    cap_s = _notional_capacity(env, sim_s)
    cap_b = _notional_capacity(env, sim_b)
    return {
        "strategy": ms, "baseline": mb,
        "delta_sharpe": ds,
        "delta_ann_return": ms["ann_return"] - mb["ann_return"],
        "delta_max_drawdown": ms["max_drawdown"] - mb["max_drawdown"],
        "block_bootstrap": bb,
        "capacity": {"strategy": cap_s, "baseline": cap_b},
    }


def _notional_capacity(env, sim: dict) -> float | None:
    """名义容量（报告字段）：全池平均日成交额 / 策略平均单日单边换手
    （参与率 100% 的理论上限，非可执行容量——不做门）。"""
    turns = [d["turnover"] for d in sim.get("turnover_days") or []]
    if not turns:
        return None
    amt = env.amount if env.amount is not None else None
    if amt is None:
        return None
    listed = env.listed if env.listed is not None else np.ones(amt.shape, bool)
    daily = np.nanmean(np.where(listed, amt, np.nan), axis=1)
    total = float(np.nansum(daily))
    if not np.isfinite(total) or total <= 0:
        return None
    return total / float(np.mean(turns))


def g4_increment(inc: dict, alpha: float = 0.05) -> tuple[bool, str]:
    bb = (inc or {}).get("block_bootstrap") or {}
    z, p = bb.get("z"), bb.get("p")
    if z is None or p is None or not np.isfinite(float(z)):
        return False, ("G4′ 无法判定：日收益差 bootstrap 不可算"
                       "（样本不足/零离散）")
    # _block_bootstrap 的 p 是双侧 erfc 口径 → 单侧减半
    if float(p) / 2.0 > alpha:
        ds = (inc or {}).get("delta_sharpe")
        return False, (f"G4′ 拒收：增量 CI 下界 ≤ 0（delta z={float(z):.2f}，"
                       f"delta Sharpe={ds}）——overlay 未在成本后超越"
                       "被动 top-K 基线")
    return True, (f"G4′ 通过：增量 z={float(z):.2f}（p={float(p):.4f}，"
                  "block bootstrap CI 下界 > 0）")
