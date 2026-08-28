# coding=utf-8
"""G1′ matched-turnover placebo（路径依赖统计的中心化检验）。

null 构造：与策略**同一再平衡日历、同一权重幅值**，但目标资产随机
（把每个 bar 的目标权重复制到随机抽取的资产上）——同换手量级/同持仓
期结构，通过**同一模拟器含费用**。策略 Sharpe 必须 z ≥ 3 超出该 null
（z = (sharpe_real − μ_null)/σ_null，σ_null = 逐 draw Sharpe 的 std）。

双档纪律（沿尾线 G1 轻/权威模式，C2 默认）：
- evaluate 轻量 m≥30（响应标 degraded——σ 相对误差大，弱证据）；
- submit 权威重跑 m≥60（按指纹缓存，预算/下限照噪声门模式：跑满 3
  draw 后按实测均时预算自适应截断，下限内截断报 budget_hit）。
"""
from __future__ import annotations

import time

import numpy as np

from .simulator import DEFAULT_COST_MODEL, simulate

PLACEBO_MIN_DRAWS_LIGHT = 30    # evaluate 轻量下限（degraded 标注）
PLACEBO_MIN_DRAWS_FULL = 60     # submit 权威下限
PLACEBO_BUDGET_SECS = 240.0     # 预算（给 worker 墙 300s 留头部）


def null_path_for_draw(base_path: list[dict], symbols: list[str],
                       rng: np.random.Generator) -> list[dict]:
    """同日历同幅值、随机目标的 null 权重路径（单 draw）。

    每个 bar：取真实目标的权重值列表，等量分配到随机抽取的资产
    （不放回）——换手量级/持仓期结构与真实路径同源（matched 的近似
    实现：随机目标天然带来略高换手，null 偏保守）。"""
    n = len(symbols)
    out = []
    for w in base_path:
        if not w:
            out.append({})
            continue
        vals = sorted(w.values(), reverse=True)
        pick = rng.choice(n, size=len(vals), replace=False)
        out.append({symbols[j]: float(v)
                    for j, v in zip(pick, vals)})
    return out


def matched_turnover_placebo(env, base_path: list[dict],
                             real_sharpe: float | None = None,
                             m: int = PLACEBO_MIN_DRAWS_LIGHT,
                             seed: int = 0,
                             cost_model=None,
                             budget_secs: float | None = None,
                             min_draws: int | None = None) -> dict:
    """m 个 matched null 的 Sharpe 分布 + z（real 需由调用方传入或先算）。

    real_sharpe=None → 先跑基线模拟取 Sharpe。预算自适应：跑满 3 draw
    后若下一 draw 将超预算且已过 min_draws（默认 m）则截断。"""
    cm = cost_model or DEFAULT_COST_MODEL
    if real_sharpe is None:
        sim = simulate(env, base_path, cm)
        real_sharpe = sim["metrics"]["sharpe"]
    budget = PLACEBO_BUDGET_SECS if budget_secs is None else float(budget_secs)
    floor = int(min_draws if min_draws is not None else m)
    t0 = time.monotonic()
    stats: list[float] = []
    m_used = m
    budget_hit = False
    for i in range(m):
        if len(stats) >= 3:
            elapsed = time.monotonic() - t0
            per = elapsed / len(stats)
            if elapsed + per > budget and len(stats) >= floor:
                m_used = len(stats)
                budget_hit = True
                break
        rng = np.random.default_rng(
            np.random.SeedSequence(seed, spawn_key=(i,)))
        null_path = null_path_for_draw(base_path, list(env.symbols), rng)
        sim = simulate(env, null_path, cm)
        sh = sim["metrics"]["sharpe"]
        if sh is not None and np.isfinite(sh):
            stats.append(float(sh))
    out = _null_summary(stats, m_used, m, real_sharpe)
    if budget_hit:
        out["budget_hit"] = True
        out["note"] = (f"预算自适应截断（{m}→{m_used} draw，单 draw "
                       f"{(time.monotonic() - t0) / max(m_used, 1):.1f}s）")
    return out


def _null_summary(stats: list[float], m_used: int, m_requested: int,
                  real: float | None) -> dict:
    if real is None or len(stats) < 2 or float(np.std(stats, ddof=1)) == 0:
        return {"m": m_used, "requested_m": m_requested,
                "n_valid": len(stats), "real_sharpe": real,
                "null_mean": None, "null_std": None, "z": None,
                "note": "null 不可算（Sharpe 缺失/有效 draw 不足/零离散）"}
    arr = np.array(stats)
    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    return {"m": m_used, "requested_m": m_requested, "n_valid": len(stats),
            "real_sharpe": float(real), "null_mean": mean, "null_std": std,
            "z": float((real - mean) / std),
            "null_q05": float(np.quantile(arr, 0.05)),
            "null_q95": float(np.quantile(arr, 0.95))}
