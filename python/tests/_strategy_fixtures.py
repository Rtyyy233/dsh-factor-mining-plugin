# coding=utf-8
"""strategy_lab 测试共用夹具：确定性合成面板（无外部数据依赖）。"""
import numpy as np
import pandas as pd

from dsh_factor_mining.factor.env import Calibration, FactorEnv


def make_env(T=60, N=4, seed=7, flat=False, lock_bars=None,
             unlisted_bars=None, dev_frac=(1 / 3, 2 / 3),
             ar: float | None = None) -> FactorEnv:
    """合成 ETF 面板。

    flat=True：全价格恒 10（费用/计息算术的精确断言用）。
    ar：AR(1) 收益过程 rets[t] = ar·rets[t-1] + noise（ar<0 = 均值回复
    面板——延迟退化/扰动 null 测试的真信号源）。
    lock_bars：{(t, j): True} 该 bar 一字板（h==l，不可成交近似）。
    unlisted_bars：{(t, j): True} 该 bar 停牌（listed=False + 价格 NaN）。
    """
    rng = np.random.default_rng(seed)
    if flat:
        rets = np.zeros((T, N))
    elif ar is not None:
        rets = np.zeros((T, N))
        noise = rng.normal(0.0, 0.012, size=(T, N))
        rets[0] = noise[0]
        for t in range(1, T):
            rets[t] = ar * rets[t - 1] + noise[t]
    else:
        rets = rng.normal(0.0, 0.01, size=(T, N))
    c = 10.0 * np.cumprod(1.0 + rets, axis=0)
    gap = (np.zeros((T, N)) if flat else rng.normal(0.0, 0.003, size=(T, N)))
    o = np.vstack([c[:1], c[:-1]]) * (1.0 + gap)
    spread = 1.005   # 恒 >1：h==l 会被模拟器当作一字板锁死（含 flat 面板）
    h = np.maximum(o, c) * spread
    l = np.minimum(o, c) / spread
    v = np.exp(rng.normal(10.0, 0.3, size=(T, N)))
    listed = np.ones((T, N), dtype=bool)
    if lock_bars:
        for (t, j), _ in lock_bars.items():
            h[t, j] = l[t, j] = o[t, j]  # 一字板近似：h == l
    if unlisted_bars:
        for (t, j), _ in unlisted_bars.items():
            listed[t, j] = False
            o[t, j] = h[t, j] = l[t, j] = c[t, j] = v[t, j] = np.nan
    dates = list(pd.bdate_range("2020-01-01", periods=T))
    dev_end = str(dates[int(T * dev_frac[0])].date())
    sel_end = str(dates[int(T * dev_frac[1])].date())
    return FactorEnv(o, h, l, c, v, dates, [f"E{i}" for i in range(N)],
                     listed=listed, amount=v * c,
                     calibration=Calibration(dev_end=dev_end, sel_end=sel_end))


def const_path(env, weights: dict) -> list[dict]:
    """常权重路径（len T；费用/计息/重放测试的最小策略）。"""
    return [dict(weights) for _ in range(env.T)]
