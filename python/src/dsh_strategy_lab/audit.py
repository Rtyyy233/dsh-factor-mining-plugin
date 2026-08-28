# coding=utf-8
"""审计（规划 §4 锁 3/4——每次 evaluate 全量跑；门判定只在 submit）。

锁 3 **截断不变性**（机械前视检查的字面实现）：pipeline（fit+apply）
在 env[:t] 与全量 env 各跑一次，权重路径前 t+1 段逐位一致；任何分歧
= 读过未来，fail-closed。本 harness 的立身测试（故意前视负例必须被抓）。

锁 4 **因果扰动族**：
- 强制执行延迟 +1 bar：真因果策略应温和退化（信号衰减），性能不变
  = 红旗（无时间信息或滞后套利）；
- 信号日置换：env bar 行序随机置换（截面结构保留、时间对齐破坏）——
  真策略崩向 null；置换后仍显著 = 拟合时间对齐 artifact；
- 随机游走面板（noise.generate_noise_world 复用，掩码/日历/symbols
  原样）：在假价格上赚钱 = 剥削模拟器（G2′ 输入，submit 侧门）。

锁 1（数据禁运）是 API 设计：env 只由 harness 构造分发（env_adapter/
worker），workflow 不出现原始面板路径；锁 2（代码只在沙箱执行）=
worker 子进程墙（P5）；锁 5（账本唯一出水口）= trail（P3）。
"""
from __future__ import annotations

import numpy as np

from dsh_factor_mining.factor.env import FactorEnv
from dsh_factor_mining.factor.noise import generate_noise_world

from .contract import run_apply, run_fit
from .env_adapter import truncate_env
from .simulator import DEFAULT_COST_MODEL, simulate


def run_pipeline(ns: dict, env, seed: int) -> list[dict]:
    """fit+apply 全管道（fit 可缺省 = 无状态策略）。

    fit 与 apply 各自独立注入同一 seed（apply 的 RNG 状态不依赖 fit
    消耗了多少随机数——管道确定性对任意实现成立）。"""
    state = run_fit(ns, env, seed)
    return run_apply(ns, state, env, seed)


def determinism_check(ns: dict, env, seed: int, n: int = 2) -> dict:
    """同 env 同 seed 双跑逐位一致（自带独立种子的实现被抓）。"""
    runs = [run_pipeline(ns, env, seed) for _ in range(n)]
    ok = all(r == runs[0] for r in runs[1:])
    return {"verdict": "deterministic" if ok else "NONDETERMINISTIC",
            "n_runs": n}


def truncation_invariance(ns: dict, env, seed: int, t_samples: int = 4) -> dict:
    """env[:t] 与全量 env 的管道权重前缀逐位比对。

    t 抽样 [T//4, T-2]（末段留执行窗口）；任何 t 分歧即 FUTURE_LEAK
    （报告首个泄漏点）。全量管道同时作为基线路径返回给调用方复用。"""
    base = run_pipeline(ns, env, seed)
    lo, hi = max(env.T // 4, 1), env.T - 2
    if hi <= lo:
        return {"verdict": "insufficient", "note": f"env.T={env.T} 太短",
                "base_path": base}
    ts = np.linspace(lo, hi, min(t_samples, hi - lo + 1)).astype(int)
    for t in ts:
        t = int(t)
        short = run_pipeline(ns, truncate_env(env, t), seed)
        if short != base[:t + 1]:
            bad = next((i for i in range(t + 1) if short[i] != base[i]),
                       None)
            return {
                "verdict": "FUTURE_LEAK", "leak_t": t,
                "first_diff_bar": int(bad) if bad is not None else None,
                "note": (f"env 截到 {t} 与全量各跑一次，权重前缀在第 "
                         f"{bad} 个 bar 分歧——apply/fit 读过未来"
                         "（检查 shift/rolling 的负窗口或末行索引）"),
                "base_path": base,
            }
    return {"verdict": "causal", "t_sampled": [int(x) for x in ts],
            "base_path": base}


def delay_verdict(base_sharpe, delayed_sharpe, eps: float = 1e-9) -> dict:
    """强制延迟 +1 bar 的判定：温和退化 = pass；不退化/反升 = 红旗。

    基线 sharpe 不可算（零波动/样本不足）= 无法判定 → fail-closed 不
    判过。eps 容差内视为「不变」。"""
    if base_sharpe is None or delayed_sharpe is None:
        return {"verdict": "undecidable",
                "note": "sharpe 不可算（零波动）——延迟退化检查无从判定"}
    if delayed_sharpe >= base_sharpe - eps:
        return {"verdict": "NO_DEGRADATION",
                "base_sharpe": base_sharpe, "delayed_sharpe": delayed_sharpe,
                "delta": base_sharpe - delayed_sharpe,
                "note": ("执行延迟 +1 bar 性能不降——信号无时间衰减，"
                         "疑似滞后套利/无信息持仓")}
    return {"verdict": "degraded", "base_sharpe": base_sharpe,
            "delayed_sharpe": delayed_sharpe,
            "delta": base_sharpe - delayed_sharpe}


def delay_check(env, base_path: list[dict], cost_model=None) -> dict:
    """基线路径 vs 整体后移 1 bar 的路径，同模拟器含费用。"""
    cm = cost_model or DEFAULT_COST_MODEL
    delayed = [dict()] + [dict(w) for w in base_path[:-1]]
    sim_b = simulate(env, base_path, cm)
    sim_d = simulate(env, delayed, cm)
    out = delay_verdict(sim_b["metrics"]["sharpe"], sim_d["metrics"]["sharpe"])
    out["delayed_metrics"] = sim_d["metrics"]
    return out


def permute_env(env: FactorEnv, rng: np.random.Generator) -> FactorEnv:
    """bar 行序随机置换（截面结构保留、时间对齐破坏——dates 随行走）。"""
    perm = rng.permutation(env.T)
    return FactorEnv(
        env.o[perm], env.h[perm], env.l[perm], env.c[perm], env.v[perm],
        [env.dates[i] for i in perm], env.symbols,
        listed=(env.listed[perm] if env.listed is not None else None),
        amount=(env.amount[perm] if env.amount is not None else None),
        calibration=env.calibration)


def _null_world_stats(ns, make_env_fn, seed: int, m: int, cost_model):
    """m 个扰动世界上的 sharpe 分布（日置换与随机游走共用）。"""
    cm = cost_model or DEFAULT_COST_MODEL
    stats = []
    for i in range(m):
        rng = np.random.default_rng(np.random.SeedSequence(seed, spawn_key=(i,)))
        wenv = make_env_fn(rng)
        try:
            path = run_pipeline(ns, wenv, seed)
            sim = simulate(wenv, path, cm)
        except Exception:    # noqa: BLE001 — 扰动世界里崩溃 = 该世界无统计
            continue
        sh = sim["metrics"]["sharpe"]
        if sh is not None and np.isfinite(sh):
            stats.append(float(sh))
    if len(stats) < 2 or float(np.std(stats, ddof=1)) == 0:
        return {"m": m, "n_valid": len(stats), "mean": None, "std": None,
                "z": None, "note": "有效世界不足或零离散"}
    arr = np.array(stats)
    mean, std = float(arr.mean()), float(arr.std(ddof=1))
    z = mean / (std / np.sqrt(len(arr)))
    return {"m": m, "n_valid": len(stats), "mean": mean, "std": std,
            "z": float(z), "max": float(arr.max()),
            "min": float(arr.min())}


def day_permutation(ns, env, seed: int, m: int = 20, cost_model=None) -> dict:
    """信号日置换 null：真策略在置换世界崩向 0；置换后均值显著为正
    （z ≥ 3，submit 侧定门）= 拟合时间对齐 artifact。

    边界（v1 文档化）：行置换破坏价格路径结构（动量/均值回复/自相关
    通道）；逐日历日期的 regime 拟合由随机游走门与启动点扰动补位。"""
    return _null_world_stats(ns, lambda rng: permute_env(env, rng),
                             seed, m, cost_model)


def random_walk_panel(ns, env, seed: int, m: int = 10,
                      cost_model=None) -> dict:
    """随机游走面板 artifact 检查（G2′ 输入）：在假价格上赚钱 = 剥削
    模拟器（成交假设漏洞/掩码结构），成本后显著为正即拒（口径 C4
    校准期定档，默认 z ≥ 3）。"""
    return _null_world_stats(ns, lambda rng: generate_noise_world(env, rng),
                             seed, m, cost_model)


def audit_full(ns: dict, env, seed: int, day_perm_m: int = 20,
               rw_m: int = 10, cost_model=None) -> dict:
    """G0 审计链编排：确定性 + 截断不变性 + 延迟退化（不过 = 程序性
    拒收，不进统计判定）；日置换/随机游走随附（submit 侧 G2′/门链用）。

    base_path/基线模拟返回给调用方（evaluate 流程复用，不重跑）。"""
    det = determinism_check(ns, env, seed)
    trunc = truncation_invariance(ns, env, seed)
    base_path = trunc.pop("base_path")
    if det["verdict"] != "deterministic" or trunc["verdict"] != "causal":
        return {"g0_pass": False, "determinism": det,
                "truncation": trunc, "note": "程序性拒收（G0）"}
    delay = delay_check(env, base_path, cost_model)
    g0 = delay["verdict"] == "degraded"
    out = {
        "g0_pass": g0,
        "determinism": det,
        "truncation": trunc,
        "delay": delay,
        "day_perm": day_permutation(ns, env, seed, m=day_perm_m,
                                    cost_model=cost_model),
        "random_walk": random_walk_panel(ns, env, seed, m=rw_m,
                                         cost_model=cost_model),
        "base_path": base_path,
    }
    if not g0:
        out["note"] = f"程序性拒收（G0）：延迟退化 {delay['verdict']}"
    return out
