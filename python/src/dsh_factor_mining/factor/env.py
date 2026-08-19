# coding=utf-8
"""PIT-safe factor environment.

The (T, N) matrix view mirrors the contract of the reference harness:

- o/h/l/c/v are float64 arrays shaped (T, N)
- dates and symbols are aligned to the matrices
- listed is a (T, N) bool tradability mask that is point-in-time safe
- amount is an optional (T, N) float64 turnover array

This module has no default data path.  Callers construct the environment from
already normalized matrices, usually through dsh_factor_mining.data.adapters.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Calibration:
    """市场口径（evaluators 全部从这里读，模块级常量只是历史默认值）。

    缺省值 = 原硬编码口径（cn_etf_daily），不带 calibration 的环境行为与
    旧版完全一致（parity）。研究性量（horizon/cost_bps/dev_end/sel_end）不进
    预设——预设只定结构性市场口径，避免偷偷改变研究结果。
    """

    frequency: str = "daily"           # daily | minute（minute: T 单位 = bar）
    horizon: int = 20                  # forward bars（daily=交易日, minute=bar）
    cost_bps: float = 10.0             # 单边成本
    execution: str = "t1"              # t1: T+1 开盘执行 | t0: 信号 bar 收盘执行
    # 三区分界（train/selection/test）：由用户显式选择，无隐式默认——
    # None 时 build_factor_env 按数据范围 60/20/20 自适应保底并回填实际值。
    # 建议分界选市场风格切换点（regime 拐点），不是随机比例切。
    dev_end: str | None = None         # development 区结束
    sel_end: str | None = None         # selection 区结束 = test 区开始
    # 分界来源标记：True = 按数据 60/20/20 自适应保底（未显式确认），
    # False = 用户/配置显式给定。报告口径时必须声明。
    regions_auto: bool = False
    annualization: float = 252.0       # 年化 bar 数（minute 预设 = bars_per_day*252）
    limit_up_down_mask: bool = False   # 一字板 bar 剔除（近似 h==l，个股 T+1 不可成交）
    ic_sample_every: int = 0           # 不重叠 IC 采样步长；0 = 用 horizon
    top_n: int = 10                    # top-N 组合宽度
    bars_per_day: int = 0              # minute 专用：每日 bar 数（0 = 不适用）

    @property
    def cost(self) -> float:
        return self.cost_bps / 1e4

    @property
    def sample_step(self) -> int:
        return self.ic_sample_every or self.horizon


class FactorEnv:
    """Point-in-time data view for factor functions."""

    def __init__(self, o, h, l, c, v, dates, symbols, listed=None, amount=None,
                 calibration: "Calibration | None" = None):
        self.o = np.asarray(o, dtype=np.float64)
        self.h = np.asarray(h, dtype=np.float64)
        self.l = np.asarray(l, dtype=np.float64)
        self.c = np.asarray(c, dtype=np.float64)
        self.v = np.asarray(v, dtype=np.float64)
        self.dates = list(dates)
        self.symbols = list(symbols)
        self.T, self.N = self.c.shape
        self.calibration = calibration if calibration is not None else Calibration()
        if listed is None:
            # Fallback: amount > 0 is used as a tradability proxy.
            self.listed = (
                (np.asarray(amount, dtype=np.float64) > 0)
                if amount is not None
                else np.ones((self.T, self.N), dtype=bool)
            )
        else:
            self.listed = np.asarray(listed, dtype=bool)
        self.amount = np.asarray(amount, dtype=np.float64) if amount is not None else None

    def __repr__(self):
        return f"FactorEnv(T={self.T}, N={self.N}, dates={self.dates[0]}~{self.dates[-1]})"


def env_from_arrays(o, h, l, c, v, dates, symbols, listed=None, amount=None) -> FactorEnv:
    """Build a FactorEnv from already aligned matrices."""
    if not (o.shape == h.shape == l.shape == c.shape == v.shape):
        raise ValueError("OHLCV arrays must share one (T, N) shape")
    if c.shape[0] != len(dates):
        raise ValueError(f"dates length {len(dates)} != T {c.shape[0]}")
    if c.shape[1] != len(symbols):
        raise ValueError(f"symbols length {len(symbols)} != N {c.shape[1]}")
    return FactorEnv(o, h, l, c, v, dates, symbols, listed=listed, amount=amount)
