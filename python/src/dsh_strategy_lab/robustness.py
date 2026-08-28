# coding=utf-8
"""R robustness 块（不计 N_eff——它们是一次假设的探针）。

- **启动点扰动**（出生日偏移网格 ≥8 点）：同一策略从不同起始 bar 起
  跑，报告 delta 中位数/IQR/正占比。v1 = 软门：报告 + 软信号；对照
  matched null 离散度校准后升硬门（C3，P6 定档）。regime 依赖是策略
  第一死因——出生日即 regime 抽签。
- **参数邻域平缓性**：策略参数变体（源码变体，参数已烘焙进源）逐个
  跑，报告邻域 Sharpe 离散度。跳过的邻居 ≠ 失败（沿因子层 flatness
  纪律——编译/运行失败的变体记 skipped，不做门）。
"""
from __future__ import annotations

import numpy as np

from .audit import run_pipeline
from .contract import compile_strategy
from .env_adapter import offset_env
from .simulator import DEFAULT_COST_MODEL, simulate


def startup_perturbation(ns: dict, env, seed: int = 42,
                         n_offsets: int = 8, cost_model=None) -> dict:
    """出生日偏移网格：o ∈ linspace(0, T//8?, ...) 均匀 ≥8 点。

    每点：env 丢前 o 根 → 管道 → 模拟 → Sharpe；报告 vs 全样本基线的
    delta 分布（median/IQR/正占比）与逐点明细。基线 Sharpe 不可算 →
    verdict=insufficient（软门：报告照给，不判过不判拒）。"""
    cm = cost_model or DEFAULT_COST_MODEL
    max_off = max(env.T // 4, 1)
    offsets = np.unique(np.linspace(0, max_off, max(int(n_offsets), 8)
                                    ).astype(int))
    base_sim = simulate(env, run_pipeline(ns, env, seed), cm)
    base_sharpe = base_sim["metrics"]["sharpe"]
    points = []
    for o in offsets:
        sub = offset_env(env, int(o))
        try:
            sim = simulate(sub, run_pipeline(ns, sub, seed), cm)
        except Exception as e:    # noqa: BLE001 — 单点崩溃记 error 不中断
            points.append({"offset": int(o),
                           "error": f"{type(e).__name__}: {e}"[:80]})
            continue
        sh = sim["metrics"]["sharpe"]
        points.append({
            "offset": int(o),
            "sharpe": sh,
            "delta": (sh - base_sharpe)
            if (sh is not None and base_sharpe is not None) else None,
        })
    deltas = [p["delta"] for p in points if p.get("delta") is not None]
    out = {"n_points": len(offsets), "base_sharpe": base_sharpe,
           "points": points, "n_valid": len(deltas)}
    if len(deltas) < 3 or base_sharpe is None:
        out["verdict"] = "insufficient"
        out["note"] = "有效偏移点不足或基线 Sharpe 不可算——软门只报告"
        return out
    arr = np.array(deltas)
    out.update({
        "verdict": "reported",          # v1 软门（C3 校准后升硬）
        "delta_median": float(np.median(arr)),
        "delta_iqr": float(np.percentile(arr, 75) - np.percentile(arr, 25)),
        "positive_frac": float((arr > 0).mean()),
        "note": ("软门 v1：对照 matched null 离散度校准后升硬门（C3）——"
                 "中位/正占比是 regime 依赖的遥测，不单独判拒"),
    })
    return out


def param_flatness(sources: dict[str, str], env, seed: int = 42,
                   cost_model=None) -> dict:
    """参数邻域平缓性：sources = {变体名: 源码}（含中心）；逐个编译 +
    管道 + 模拟，报告 Sharpe 邻域离散度（极差 / |中心|）。跳过的邻居 ≠
    失败（skipped 记录，不判拒）。"""
    cm = cost_model or DEFAULT_COST_MODEL
    rows = {}
    for name, src in sources.items():
        try:
            ns = compile_strategy(src)
            sim = simulate(env, run_pipeline(ns, env, seed), cm)
            rows[name] = {"sharpe": sim["metrics"]["sharpe"]}
        except Exception as e:    # noqa: BLE001 — 跳过的邻居 ≠ 失败
            rows[name] = {"skipped": f"{type(e).__name__}: {e}"[:80]}
    vals = [r["sharpe"] for r in rows.values()
            if isinstance(r.get("sharpe"), (int, float))
            and np.isfinite(r["sharpe"])]
    out = {"variants": rows, "n_total": len(rows), "n_valid": len(vals)}
    if len(vals) < 2:
        out["verdict"] = "insufficient"
        out["note"] = "有效变体不足 2——平缓性无从谈起（skipped 不判拒）"
        return out
    center = rows.get("center", {}).get("sharpe")
    spread = float(max(vals) - min(vals))
    rel = (spread / abs(center)) if center not in (None, 0) else None
    out.update({"verdict": "reported",
                "sharpe_spread": spread,
                "rel_spread": rel,
                "note": ("报告字段：rel_spread 大 = 参数尖峰（过拟合嗅觉"
                         "信号），v1 不建门" if rel is not None else
                         "中心 Sharpe 为 0/缺——rel_spread 不可算，报告极差")})
    return out
