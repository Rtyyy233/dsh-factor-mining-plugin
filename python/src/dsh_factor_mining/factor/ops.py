# coding=utf-8
"""因子算子库 — 随机种子生成器的公开算子集（向量化，禁循环嵌套）。

本模块是算子集的**真值源**：
  - 随机生成器从这里采样组合（random_gen.py 只做组合，不重写数学）
  - 生成的 factor 源码 `from dsh_factor_mining.factor.ops import ...` 引用这里
  - 用户经 bridge 的 factor.operators 工具查看/配置生效集（覆盖文件存 stateRoot）

设计约定（[[vectorize-no-nested-loops]]）：
  - 全部算子接受/返回 pandas DataFrame (T,N)，时间轴 rolling、截面 axis=1
  - 无逐日/逐股循环；窗口用 rolling，截面用 rank/zscore axis=1

口径：ts_* 为时序算子（窗口 w 沿时间轴），cs_* 为截面算子（每日截面），
二元/非线性为逐元素。所有算子因果安全（只向后看）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 时序算子（窗口沿时间轴，只向后看）
# ---------------------------------------------------------------------------
def ts_mean(x, w):
    return x.rolling(w, min_periods=max(2, w // 2)).mean()

def ts_std(x, w):
    return x.rolling(w, min_periods=max(2, w // 2)).std()

def ts_max(x, w):
    return x.rolling(w, min_periods=max(2, w // 2)).max()

def ts_min(x, w):
    return x.rolling(w, min_periods=max(2, w // 2)).min()

def ts_sum(x, w):
    return x.rolling(w, min_periods=max(2, w // 2)).sum()

def ts_rank(x, w):
    """当前值在过去 w 期中的分位（时序分位，非截面）。"""
    return x.rolling(w, min_periods=max(2, w // 2)).rank(pct=True)

def ts_zscore(x, w):
    m = x.rolling(w, min_periods=max(2, w // 2)).mean()
    s = x.rolling(w, min_periods=max(2, w // 2)).std()
    return (x - m).div(s.clip(lower=1e-12))

def ts_skew(x, w):
    return x.rolling(w, min_periods=max(3, w)).skew()

def ts_kurt(x, w):
    return x.rolling(w, min_periods=max(4, w)).kurt()

def ts_corr(x, y, w):
    return x.rolling(w, min_periods=max(3, w // 2)).corr(y)

def ts_cov(x, y, w):
    return x.rolling(w, min_periods=max(3, w // 2)).cov(y)

def ts_slope(x, w):
    """滚动线性回归斜率：x 对时间的趋势（只向后看）。"""
    mp = max(2, w // 2)
    mean_x = x.rolling(w, min_periods=mp).mean()
    var_x = x.rolling(w, min_periods=mp).var()
    # 时间索引 t = 0..w-1 的 rolling 协方差：cov(x, t) = mean((x_i - mx)(i - mi))
    t = pd.DataFrame(np.tile(np.arange(w, dtype=np.float64), (x.shape[1], 1)).T,
                     index=x.index, columns=x.columns)
    mean_t = t.rolling(w, min_periods=mp).mean()
    ct = (x.mul(t)).rolling(w, min_periods=mp).mean().sub(mean_x.mul(mean_t))
    return ct.div(var_x.clip(lower=1e-12))

def ts_delay(x, d):
    return x.shift(d)

def ts_diff(x, d):
    return x.diff(d)

def ts_decay_linear(x, w):
    """线性衰减加权均值（近端权重高）。"""
    mp = max(2, w // 2)
    weights = np.arange(1, w + 1, dtype=np.float64)
    weights /= weights.sum()

    def _apply(col):
        return col.rolling(w, min_periods=mp).apply(
            lambda v: float(np.dot(v, weights[-len(v):] / weights[-len(v):].sum()))
            if len(v) < w else float(np.dot(v, weights)), raw=True)
    return x.apply(_apply)

def ts_av_diff(x, w):
    return x.sub(x.rolling(w, min_periods=max(2, w // 2)).mean())

def ts_momentum(x, w):
    return x.div(x.shift(w)).sub(1.0)

def ts_vol_ratio(x, y, w):
    """x 的 w 期 std 除以 y 的 w 期 std（相对波动结构）。"""
    mp = max(2, w // 2)
    return x.rolling(w, min_periods=mp).std().div(
        y.rolling(w, min_periods=mp).std().clip(lower=1e-12))

# ---------------------------------------------------------------------------
# 截面算子（每日截面，axis=1）
# ---------------------------------------------------------------------------
def cs_rank(x):
    return x.rank(axis=1, pct=True)

def cs_zscore(x):
    m = x.mean(axis=1, skipna=True)
    s = x.std(axis=1, skipna=True).clip(lower=1e-12)
    return x.sub(m, axis=0).div(s, axis=0)

def cs_winsorize(x, k=3.0):
    """截面 winsorize：clip 到均值 ± k·std（k 固定 3.0，不参与随机）。"""
    m = x.mean(axis=1, skipna=True)
    s = x.std(axis=1, skipna=True).clip(lower=1e-12)
    lo = m.sub(k * s)
    hi = m.add(k * s)
    return x.clip(lower=lo, upper=hi, axis=0)

def cs_scale(x):
    """截面绝对值归一（L1），保号。"""
    denom = x.abs().sum(axis=1, skipna=True).clip(lower=1e-12)
    return x.div(denom, axis=0)

# ---------------------------------------------------------------------------
# 二元算子（逐元素）
# ---------------------------------------------------------------------------
def add(x, y):
    return x.add(y)

def sub(x, y):
    return x.sub(y)

def mul(x, y):
    return x.mul(y)

def div(x, y):
    return x.div(y.abs().clip(lower=1e-9) * np.sign(y.replace(0, 1.0)))

def minv(x, y):
    return np.minimum(x, y)

def maxv(x, y):
    return np.maximum(x, y)

def signed_diff(x, y):
    """x−y 的符号（方向结构，无量纲）。"""
    return np.sign(x.sub(y))

# ---------------------------------------------------------------------------
# 非线性/变换（单目）
# ---------------------------------------------------------------------------
def absx(x):
    return x.abs()

def signx(x):
    return np.sign(x)

def log1p_abs(x):
    """log(1+|x|)：压尾部保序。"""
    return np.log1p(x.abs())

def sigmoid(x):
    return pd.DataFrame(1.0 / (1.0 + np.exp(-np.clip(x.values, -30, 30))),
                        index=x.index, columns=x.columns)

def tanhx(x):
    return pd.DataFrame(np.tanh(np.clip(x.values, -30, 30)),
                        index=x.index, columns=x.columns)

def sqrt_abs(x):
    return np.sqrt(x.abs())

def inv(x):
    return 1.0 / x.abs().clip(lower=1e-9) * np.sign(x.replace(0, 1.0))

def negx(x):
    return -x

# ---------------------------------------------------------------------------
# 算子注册表（随机生成器的采样空间真值源）
# ---------------------------------------------------------------------------
# 类别 → {算子名: (函数, 参数空间)}
#   参数空间: "w" 从 DEFAULT_WINDOWS 采样；"d" 从 DEFAULT_DELAYS 采样；
#   "none" 无参数；二元算子隐式需要两个子树。
DEFAULT_WINDOWS = [3, 5, 10, 20, 60, 120, 250]
DEFAULT_DELAYS = [1, 2, 3, 5, 10, 20]

OPERATOR_REGISTRY = {
    "ts": {
        "ts_mean": (ts_mean, "w"), "ts_std": (ts_std, "w"),
        "ts_max": (ts_max, "w"), "ts_min": (ts_min, "w"),
        "ts_sum": (ts_sum, "w"), "ts_rank": (ts_rank, "w"),
        "ts_zscore": (ts_zscore, "w"), "ts_skew": (ts_skew, "w"),
        "ts_kurt": (ts_kurt, "w"), "ts_slope": (ts_slope, "w"),
        "ts_decay_linear": (ts_decay_linear, "w"), "ts_av_diff": (ts_av_diff, "w"),
        "ts_momentum": (ts_momentum, "w"), "ts_delay": (ts_delay, "d"),
        "ts_diff": (ts_diff, "d"),
        # 双输入时序算子（需要两棵子树）
        "ts_corr": (ts_corr, "w"), "ts_cov": (ts_cov, "w"),
        "ts_vol_ratio": (ts_vol_ratio, "w"),
    },
    "cs": {
        "cs_rank": (cs_rank, "none"), "cs_zscore": (cs_zscore, "none"),
        "cs_winsorize": (cs_winsorize, "none"), "cs_scale": (cs_scale, "none"),
    },
    "binary": {
        "add": (add, "none"), "sub": (sub, "none"), "mul": (mul, "none"),
        "div": (div, "none"), "minv": (minv, "none"), "maxv": (maxv, "none"),
        "signed_diff": (signed_diff, "none"),
    },
    "unary": {
        "absx": (absx, "none"), "signx": (signx, "none"),
        "log1p_abs": (log1p_abs, "none"), "sigmoid": (sigmoid, "none"),
        "tanhx": (tanhx, "none"), "sqrt_abs": (sqrt_abs, "none"),
        "inv": (inv, "none"), "negx": (negx, "none"),
    },
}

# 双输入算子集合（生成器需要为它们生成两棵子树）
TWO_INPUT_TS = {"ts_corr", "ts_cov", "ts_vol_ratio"}

LEAVES = ["o", "h", "l", "c", "v", "amount"]
