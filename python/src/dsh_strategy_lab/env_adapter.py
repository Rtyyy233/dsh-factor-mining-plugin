# coding=utf-8
"""env 适配层：复用因子层 FactorEnv（唯一经生产校验的时点隔离实现——
复制 = 两处维护前视防线），只加策略层需要的物理截断与日程 API。

审计锁 1（数据禁运）配套：面板加载器私有（CLI/worker 侧），env 只由
harness 构造分发给 worker；本模块的 truncate_env 是「物理截断」的
唯一实现（fit/apply 日程归 harness 的字面机制）。
"""
from __future__ import annotations

import numpy as np

from dsh_factor_mining.factor.env import Calibration, FactorEnv


def truncate_env(env: FactorEnv, upto_inclusive: int) -> FactorEnv:
    """物理截断：返回只含 bars [0, upto_inclusive] 的新 env（数组拷贝，
    防策略原地改写共享内存；calibration 引用共享——harness 不改它）。

    fit 拿到的 env、截断不变性审计的两次输入，都走这里。"""
    t = int(upto_inclusive)
    if t < 0 or t >= env.T:
        raise ValueError(f"截断索引 {t} 越界（env.T={env.T}；含端点）")
    cal = env.calibration
    return FactorEnv(
        env.o[:t + 1].copy(), env.h[:t + 1].copy(),
        env.l[:t + 1].copy(), env.c[:t + 1].copy(),
        env.v[:t + 1].copy(),
        list(env.dates[:t + 1]), list(env.symbols),
        listed=(env.listed[:t + 1].copy() if env.listed is not None else None),
        amount=(env.amount[:t + 1].copy() if env.amount is not None else None),
        calibration=cal,
    )


def offset_env(env: FactorEnv, skip: int) -> FactorEnv:
    """出生日偏移：丢弃前 skip 根 bar（robustness 启动点扰动用；与
    truncate_env 对称的左截断，同样拷贝隔离）。"""
    s = int(skip)
    if s < 0 or s >= env.T:
        raise ValueError(f"偏移 {s} 越界（0 ≤ skip < env.T={env.T}）")
    return FactorEnv(
        env.o[s:].copy(), env.h[s:].copy(), env.l[s:].copy(),
        env.c[s:].copy(), env.v[s:].copy(),
        list(env.dates[s:]), list(env.symbols),
        listed=(env.listed[s:].copy() if env.listed is not None else None),
        amount=(env.amount[s:].copy() if env.amount is not None else None),
        calibration=env.calibration,
    )


def region_span(env: FactorEnv, t0_date=None, t1_date=None) -> tuple[int, int]:
    """[t0_idx, t1_idx) 日期区间（含端排除；None = 数据端点）。"""
    import pandas as pd

    t0, t1 = 0, env.T
    if t0_date is not None:
        t0 = int(np.searchsorted(env.dates, pd.Timestamp(t0_date)))
    if t1_date is not None:
        t1 = int(np.searchsorted(env.dates, pd.Timestamp(t1_date)))
    return max(t0, 0), min(t1, env.T)


def walk_forward_schedule(env: FactorEnv, n_folds: int = 5,
                          embargo_bars: int = 5,
                          t0_date=None, t1_date=None) -> list[dict]:
    """walk-forward 折边界（harness 定，非 agent 可选项；配置进指纹）。

    expanding fit：折 i 的 fit 用数据 [0, apply_t0 − embargo)（物理截断
    交给 truncate_env），apply 区间 [apply_t0, apply_t1)。embargo 在 fit
    末与 apply 首之间留空窗（默认 5 bar；折内信息经慢变量渗透的缓冲）。

    walk_forward 是策略层**必经** stage（regime 依赖是策略第一死因）；
    折内参数重选不放大 N_eff（记为同指纹展开——见 trail 计试验语义）。"""
    if n_folds < 1:
        raise ValueError(f"n_folds 必须 ≥1，得到 {n_folds}")
    t0, t1 = region_span(env, t0_date, t1_date)
    n = t1 - t0
    min_fold = 2
    if n < n_folds * min_fold:
        raise ValueError(
            f"walk-forward 区间样本不足（n={n} < n_folds×{min_fold}"
            f"={n_folds * min_fold}）——缩小 n_folds 或放宽区间")
    edges = np.linspace(t0, t1, n_folds + 1).astype(int)
    folds = []
    for i in range(n_folds):
        a, b = int(edges[i]), int(edges[i + 1])
        fit_end = a - int(embargo_bars)  # fit 不含 [fit_end, a)
        folds.append({
            "fold": i,
            "fit_end_idx": fit_end,          # fit env = truncate_env(env, fit_end-1)（fit_end≤0 = 无 fit 数据）
            "apply_t0_idx": a,
            "apply_t1_idx": b,
            "fit_end_date": str(env.dates[fit_end - 1].date()) if fit_end >= 1 else None,
            "apply_t0_date": str(env.dates[a].date()),
            "apply_t1_date": str(env.dates[b - 1].date()),
        })
    return folds


def fold_envs(env: FactorEnv, fold: dict) -> tuple[FactorEnv | None, FactorEnv]:
    """折 → (fit_env 或 None, apply_env)。

    apply_env = 截到折末（折内 weights[t] 只准用 ≤t 的数据由截断不变性
    审计机械保证，不由 apply_env 端点保证——apply 拿折末端点是为了
    在整折上一次性产出权重路径）。

    calibration 的三区分界在截断 env 上语义会漂（dev_end 可能落在
    截断窗外）——策略层不消费因子层三区（有自己的 wf/registry 语义），
    分界字段原样携带不重算。"""
    fit_env = None
    if fold["fit_end_idx"] >= 1:
        fit_env = truncate_env(env, fold["fit_end_idx"] - 1)
    return fit_env, truncate_env(env, fold["apply_t1_idx"] - 1)


def env_identity(env: FactorEnv) -> dict:
    """env 结构身份（进指纹/trail 的字段；不含价格数据本身）。"""
    import hashlib
    import json

    sym_hash = hashlib.sha256(
        json.dumps(list(env.symbols), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "T": int(env.T), "N": int(env.N),
        "first_date": str(env.dates[0]),
        "last_date": str(env.dates[-1]),
        "symbols_hash": sym_hash,
        "calibration": {
            "horizon": env.calibration.horizon,
            "frequency": env.calibration.frequency,
            "cost_bps": env.calibration.cost_bps,
        },
    }
