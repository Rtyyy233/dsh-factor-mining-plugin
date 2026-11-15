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

#: tail 线（spread 三件套）K 占比的历史默认；唯一权威在 Calibration.tail_k。
#: tail.K_FRAC 引用本常量保持同名兼容。
TAIL_K_FRAC_DEFAULT = 0.2

#: ts 线高频特征低频化字段的命名规则：f_ 前缀 + 小写标识符
#: （f_ 命名空间与既有字段/方法机械隔离，leaves/AST 过滤按前缀识别）。
import re as _re
TS_FEATURE_RE = _re.compile(r"^f_[a-z][a-z0-9_]*$")


@dataclass
class Calibration:
    """市场口径（evaluators 全部从这里读，模块级常量只是历史默认值）。

    缺省值 = 原硬编码口径（cn_etf_daily），不带 calibration 的环境行为与
    旧版完全一致（parity）。研究性量（horizon/cost_bps/dev_end/sel_end）不进
    预设——预设只定结构性市场口径，避免偷偷改变研究结果。
    """

    frequency: str = "daily"           # daily | minute（minute: T 单位 = bar）
    horizon: int = 20                  # forward bars（daily=交易日, minute=bar）
    # horizon 菜单（v2 2026-08-20 申报制）：允许评估的赌注集合。None = [horizon]
    # （不配置菜单 = 行为与旧版逐字节一致）。因子评估可声明菜单内任一 horizon
    # （不同 horizon = 不同经济赌注 = 不同试验，trail 按 (hash, horizon) 计账）；
    # 配置菜单会改变环境指纹 → null 校准自动要求重跑（per-horizon 基线）。
    horizons: list[int] | None = None
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
    # tail 线（spread 三件套）K 占比。None = 历史默认 0.2（tail.K_FRAC），
    # 且 None 不参与环境指纹（与 horizons 同款先例：不配置的既有部署指纹
    # 逐字节不变）；显式配置 = 换口径 → 指纹变 → null 校准自动要求重跑。
    k_frac: float | None = None
    bars_per_day: int = 0              # minute 专用：每日 bar 数（0 = 不适用）

    @property
    def cost(self) -> float:
        return self.cost_bps / 1e4

    @property
    def sample_step(self) -> int:
        return self.ic_sample_every or self.horizon

    @property
    def horizon_menu(self) -> list[int]:
        """horizon 菜单（申报制评估的合法赌注集）；未配置 = [horizon]。"""
        if self.horizons:
            return [int(h) for h in self.horizons]
        return [self.horizon]

    @property
    def tail_k(self) -> float:
        """tail 线 K 占比；未配置 = 历史默认 0.2。"""
        return TAIL_K_FRAC_DEFAULT if self.k_frac is None else float(self.k_frac)


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


class TsEnv:
    """单品种时序环境（factor-mining-futures 线，2026-11-07 任务书 §〇.2）。

    与 FactorEnv（(T,N) 截面面板）平行的第二类环境：一切字段为 (T,)
    一维 float64 数组，无截面维度。因子契约：def factor(env) -> np.ndarray
    输出 (T,)。

    字段（全部对齐同一时间轴 dates，长度 T）：
    - o/h/l/c/v：日线 OHLCV（主力连续，G-roll 增量复权后价格）
    - oi：主力合约持仓量（当日主力合约原始值，不跨合约调整）
    - close2：次主力合约收盘（增量复权口径；数据不含时为 None）
    - limit_pct：品种涨跌停幅度（G-limit 掩码参数，如 rb≈0.05-0.10）
    - instrument：品种代码（如 "rb"）；换品种 = 新赌注 = 新环境

    只用过去数据纪律与截面线同：因果门（扰动法）对 TsEnv 同样适用。
    cs_* 截面算子在单品种线上无语义——引擎侧拒绝使用（固定文案）。
    """

    # 无 __slots__：_env_horizon_view 以 __dict__ 快照构造 horizon 视图
    # （dataclass replace 语义），slots 类没有 __dict__ 会破坏该机制。

    def __init__(self, o, h, l, c, v, oi, dates, instrument="",
                 close2=None, limit_pct=0.05,
                 calibration: "Calibration | None" = None,
                 features: dict | None = None):
        self.o = np.asarray(o, dtype=np.float64)
        self.h = np.asarray(h, dtype=np.float64)
        self.l = np.asarray(l, dtype=np.float64)
        self.c = np.asarray(c, dtype=np.float64)
        self.v = np.asarray(v, dtype=np.float64)
        self.oi = np.asarray(oi, dtype=np.float64)
        n = self.c.shape[0]
        if not (self.o.shape == self.h.shape == self.l.shape
                == self.c.shape == self.v.shape == self.oi.shape):
            raise ValueError("TsEnv 字段必须同为 (T,) 一维数组且等长")
        if self.c.ndim != 1:
            raise ValueError(f"TsEnv 字段必须一维（收到 ndim={self.c.ndim}）")
        if len(dates) != n:
            raise ValueError(f"dates 长度 {len(dates)} != T {n}")
        self.close2 = (np.asarray(close2, dtype=np.float64)
                       if close2 is not None else None)
        if self.close2 is not None and self.close2.shape != self.c.shape:
            raise ValueError("close2 必须与 c 同形 (T,)")
        self.dates = list(dates)
        self.instrument = str(instrument)
        self.limit_pct = float(limit_pct)
        self.calibration = calibration if calibration is not None else Calibration()
        self.T = n
        # 高频特征低频化字段（f_* 命名空间）：日频聚合特征 (T,) 数组，
        # 由数据层预聚合（因子契约不变 (T,)）。NaN 允许（暖机期）——
        # 评估按有限性剔除；causality 扰动与 npz 序列化全覆盖。
        self.features: dict = {}
        for k, arr in (features or {}).items():
            if not TS_FEATURE_RE.match(str(k)):
                raise ValueError(
                    f"特征字段名 {k!r} 非法——必须匹配 f_<小写字母开头的小写字母数字下划线>"
                    "（f_ 前缀=高频特征低频化专用命名空间，防与既有字段冲突）")
            a = np.asarray(arr, dtype=np.float64)
            if a.ndim != 1 or a.shape[0] != n:
                raise ValueError(f"特征字段 {k} 必须是 ({n},) 一维数组（收到 {a.shape}）")
            self.features[str(k)] = a
            setattr(self, str(k), a)

    def feature_nan_counts(self) -> dict:
        """每特征字段的非有限计数（加载/审计报告面）。"""
        return {k: int((~np.isfinite(v)).sum())
                for k, v in self.features.items()}

    def __repr__(self):
        return (f"TsEnv(instrument={self.instrument!r}, T={self.T}, "
                f"dates={self.dates[0]}~{self.dates[-1]})")
