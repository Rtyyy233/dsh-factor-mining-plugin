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
    """滚动线性回归斜率：x 对时间的趋势（只向后看）。

    2026-09-18 修复（死算子复活）：t 原误用窗口长度的 0..w-1 构造 (w,N)
    矩阵却配全长 index——任何含 ts_slope 的树整棵 ValueError，被生成端
    except 静默吞掉（ts_slope 实际从未参与过采样产出）。窗口内绝对行号
    与窗口内相对位置的协方差等价（窗口内常数平移被均值消去），全长
    计数器与原意图同数学。
    """
    mp = max(2, w // 2)
    mean_x = x.rolling(w, min_periods=mp).mean()
    var_x = x.rolling(w, min_periods=mp).var()
    # 时间索引 t = 0..T-1 的 rolling 协方差：cov(x, t) = mean((x_i - mx)(i - mi))
    t = pd.DataFrame(np.tile(np.arange(x.shape[0], dtype=np.float64),
                             (x.shape[1], 1)).T, index=x.index, columns=x.columns)
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
    # 保号除零保护（2026-09-18 缝2）：分母为零/贴零的停牌限价列曾产 inf
    den = x.shift(w)
    return x.div(den.abs().clip(lower=1e-9) * np.sign(den.replace(0, 1.0))).sub(1.0)

def ts_vol_ratio(x, y, w):
    """x 的 w 期 std 除以 y 的 w 期 std（相对波动结构）。"""
    mp = max(2, w // 2)
    return x.rolling(w, min_periods=mp).std().div(
        y.rolling(w, min_periods=mp).std().clip(lower=1e-12))

def ts_resid(y, x, w):
    """滚动 OLS 残差：y 对 x 的窗口回归残差（对冲腿机械化——负相关对冲=残差结构）。"""
    mp = max(3, w // 2)
    cov = y.rolling(w, min_periods=mp).cov(x)
    var = x.rolling(w, min_periods=mp).var()
    beta = cov.div(var.clip(lower=1e-12))
    return y.sub(beta.mul(x))

def ts_beta(y, x, w):
    """滚动 OLS 斜率（敏感度/对冲系数）。"""
    mp = max(3, w // 2)
    cov = y.rolling(w, min_periods=mp).cov(x)
    var = x.rolling(w, min_periods=mp).var()
    return cov.div(var.clip(lower=1e-12))

def ts_decay_exp(x, w):
    """指数衰减加权均值（几何公比 0.5，近端权重高；与 ts_decay_linear 同构）。"""
    mp = max(2, w // 2)
    # 递减幂使最新样本拿最大权重 1（rolling 窗口 v 的 v[-1] = 最新值）
    weights = np.power(0.5, np.arange(w - 1, -1, -1, dtype=np.float64))
    weights /= weights.sum()

    def _apply(col):
        return col.rolling(w, min_periods=mp).apply(
            lambda v: float(np.dot(v, weights[-len(v):] / weights[-len(v):].sum()))
            if len(v) < w else float(np.dot(v, weights)), raw=True)
    return x.apply(_apply)

def hump(x, h):
    """换手抑制（单步近似，WorldQuant hump 语义）：相对变化 < h 时保持前值。

    真语义是递推保持链；这里向量化单步近似（只比前一日），因果只向后看。
    """
    prev = x.shift(1)
    rel = x.sub(prev).abs().div(prev.abs().clip(lower=1e-12))
    return x.where(rel >= h, prev)

# ---------------------------------------------------------------------------
# 时序算子第二批（2026-09-19 扩充）：加权/回撤区间/稳健统计/持续性/共矩
# ---------------------------------------------------------------------------
def ts_wmean(x, wgt, w):
    """加权滚动均值：Σ(x·wgt)/Σ(wgt)——量腿加权的单算子化（战役三度兑现）。"""
    mp = max(2, w // 2)
    num = x.mul(wgt).rolling(w, min_periods=mp).sum()
    den = wgt.rolling(w, min_periods=mp).sum()
    return num.div(den.abs().clip(lower=1e-9) * np.sign(den.replace(0, 1.0)))

def ts_drawdown(x, w):
    """滚动回撤：x/窗口最大值 − 1（价格语义下 ∈ (−1, 0]；近零分母列由健康门兜底）。"""
    mp = max(2, w // 2)
    mx = x.rolling(w, min_periods=mp).max()
    den = mx.abs().clip(lower=1e-9) * np.sign(mx.replace(0, 1.0))
    return x.div(den).sub(1.0)

def ts_range_pos(x, w):
    """窗口区间位置（随机指标 %K 形态）：(x−min)/(max−min) ∈ [0,1]。"""
    mp = max(2, w // 2)
    lo = x.rolling(w, min_periods=mp).min()
    hi = x.rolling(w, min_periods=mp).max()
    return x.sub(lo).div(hi.sub(lo).abs().clip(lower=1e-12))

def ts_arg_max(x, w):
    """窗口内最大值的位置（0=最新，w−1=最旧）——极值新近度（与 decay 族同成本档）。"""
    mp = max(2, w // 2)
    return x.apply(lambda col: col.rolling(w, min_periods=mp).apply(
        lambda v: float(len(v) - 1 - np.argmax(v)), raw=True))

def ts_arg_min(x, w):
    """窗口内最小值的位置（0=最新，w−1=最旧）。"""
    mp = max(2, w // 2)
    return x.apply(lambda col: col.rolling(w, min_periods=mp).apply(
        lambda v: float(len(v) - 1 - np.argmin(v)), raw=True))

def ts_median(x, w):
    """滚动中位数（稳健中心趋势）。"""
    return x.rolling(w, min_periods=max(3, w // 2)).median()

def ts_iqr(x, w):
    """滚动四分位距 p75−p25（稳健离散度）。"""
    mp = max(3, w // 2)
    q75 = x.rolling(w, min_periods=mp).quantile(0.75)
    q25 = x.rolling(w, min_periods=mp).quantile(0.25)
    return q75.sub(q25)

def ts_winsor(x, w):
    """时序 winsorize：clip 到滚动均值 ± 3σ（volmom_winsor_only 入册因子的机械化）。"""
    mp = max(3, w // 2)
    m = x.rolling(w, min_periods=mp).mean()
    s = x.rolling(w, min_periods=mp).std()
    lo = m.sub(3.0 * s)
    hi = m.add(3.0 * s)
    return pd.DataFrame(np.clip(x.values, lo.values, hi.values),
                        index=x.index, columns=x.columns)

def ts_rank_corr(x, y, w):
    """滚动秩相关（Spearman）：对滚动秩再相关——稳健版 ts_corr。"""
    mp = max(3, w // 2)
    rx = x.rolling(w, min_periods=mp).rank(pct=True)
    ry = y.rolling(w, min_periods=mp).rank(pct=True)
    return rx.rolling(w, min_periods=max(3, w)).corr(ry)

def ts_streak(x, w):
    """符号持续天数（带号，截幅 ±w）：sign(x.diff()) 连续不变天数，连涨为正连跌为负。

    每列 O(T) 纯 numpy（无逐单元 Python）；x 恒定列产常量 0 → 健康门兜底。
    """
    s = np.sign(x.diff().values)
    T, N = s.shape
    out = np.full((T, N), np.nan)
    idx = np.arange(T)
    for j in range(N):
        col = s[:, j]
        changed = np.ones(T, bool)
        changed[1:] = col[1:] != col[:-1]
        lc = np.maximum.accumulate(np.where(changed, idx, 0))
        runlen = (idx - lc + 1).astype(float)   # +1: 新符号首日记 1（含当日）
        out[:, j] = np.where(np.isnan(col), np.nan, runlen * col)
    out = np.clip(out, -float(w), float(w))
    return pd.DataFrame(out, index=x.index, columns=x.columns)

def ts_coskew(x, y, w):
    """滚动共偏度 E[(x−mx)²(y−my)]/(σx²·σy)——尾部相依战役手工构造的机械化。

    展开式全滚动均值可算（无逐窗 Python）：E[x²y]−2mx·E[xy]−my·E[x²]+2mx²·my。
    """
    mp = max(4, w // 2)
    mx = x.rolling(w, min_periods=mp).mean()
    my = y.rolling(w, min_periods=mp).mean()
    mxy = x.mul(y).rolling(w, min_periods=mp).mean()
    mxx = x.mul(x).rolling(w, min_periods=mp).mean()
    mxxy = x.mul(x).mul(y).rolling(w, min_periods=mp).mean()
    num = (mxxy.sub(mx.mul(mxy).mul(2.0)).sub(my.mul(mxx))
           .add(mx.mul(mx).mul(my).mul(2.0)))
    sx = x.rolling(w, min_periods=mp).std()
    sy = y.rolling(w, min_periods=mp).std()
    return num.div(sx.mul(sx).mul(sy).clip(lower=1e-18))

def ts_triple_corr(x, y, z, w):
    """滚动三阶共矩 E[(x−mx)(y−my)(z−mz)]/(σx·σy·σz)——尾部相依三元结构。

    展开式：E[xyz]−mx·E[yz]−my·E[xz]−mz·E[xy]+2·mx·my·mz。
    """
    mp = max(4, w // 2)
    mx = x.rolling(w, min_periods=mp).mean()
    my = y.rolling(w, min_periods=mp).mean()
    mz = z.rolling(w, min_periods=mp).mean()
    mxy = x.mul(y).rolling(w, min_periods=mp).mean()
    mxz = x.mul(z).rolling(w, min_periods=mp).mean()
    myz = y.mul(z).rolling(w, min_periods=mp).mean()
    mxyz = x.mul(y).mul(z).rolling(w, min_periods=mp).mean()
    num = (mxyz.sub(mx.mul(myz)).sub(my.mul(mxz)).sub(mz.mul(mxy))
           .add(mx.mul(my).mul(mz).mul(2.0)))
    sx = x.rolling(w, min_periods=mp).std()
    sy = y.rolling(w, min_periods=mp).std()
    sz = z.rolling(w, min_periods=mp).std()
    den = sx.mul(sy).mul(sz)
    den = den.abs().clip(lower=1e-18) * np.sign(den.replace(0, 1.0))
    return num.div(den)

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

def cs_demean(x):
    """截面去均值（市场中性化，不除 std——低分散日不被放大）。"""
    return x.sub(x.mean(axis=1, skipna=True), axis=0)

def cs_robust_z(x):
    """稳健截面标准化：(x − 中位数)/IQR（对截面尾部稳健）。"""
    med = x.median(axis=1, skipna=True)
    q75 = x.quantile(0.75, axis=1)
    q25 = x.quantile(0.25, axis=1)
    iqr = q75.sub(q25).clip(lower=1e-12)
    return x.sub(med, axis=0).div(iqr, axis=0)

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

def sfdiff(x, y):
    """无量纲差：(x−y)/(|x|+|y|) ∈ [−1,1]——跨量纲比较，免同空间约束。"""
    return x.sub(y).div(x.abs().add(y.abs()).clip(lower=1e-12))

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

def log_signed(x):
    """带号对数压缩：sign(x)·log(1+|x|)（保序保号压尾，log1p_abs 的带号版）。"""
    return np.log1p(x.abs()).mul(np.sign(x))

def softsign(x):
    """软符号：x/(1+|x|)，有界(−1,1)保号保序。"""
    return x.div(x.abs().add(1.0))

# ---------------------------------------------------------------------------
# 比较/条件算子（2026-09-18 结构扩充：条件三分支——战役"门+腿"形态的机械化）
# ---------------------------------------------------------------------------
def gt(x, y):
    """逐元素 x>y → 1.0/0.0（与 NaN 比较为 False）。"""
    return x.gt(y).astype("float64")

def lt(x, y):
    """逐元素 x<y → 1.0/0.0。"""
    return x.lt(y).astype("float64")

def where(cond, a, b):
    """条件三分支：cond∈{0,1}（来自 gt/lt）→ cond·a + (1−cond)·b，NaN 自然传播。"""
    return cond.mul(a).add(b.mul(1.0 - cond))

# ---------------------------------------------------------------------------
# 算子注册表（随机生成器的采样空间真值源）
# ---------------------------------------------------------------------------
# 类别 → {算子名: (函数, 参数空间)}
#   参数空间: "w" 从 DEFAULT_WINDOWS 采样；"d" 从 DEFAULT_DELAYS 采样；
#   "h" 从 DEFAULT_HUMPS 采样（换手族阈值）；"none" 无参数；
#   二元算子隐式需要两个子树，where 需要三个子树（cond, a, b）。
DEFAULT_WINDOWS = [3, 5, 10, 20, 60, 120, 250]
DEFAULT_DELAYS = [1, 2, 3, 5, 10, 20]
DEFAULT_HUMPS = [0.01, 0.05, 0.10]

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
        # 2026-09-18 扩充（调研 RESEARCH-random-search-space-expansion）：
        # 回归残差族 + 衰减族 + 换手族
        "ts_resid": (ts_resid, "w"), "ts_beta": (ts_beta, "w"),
        "ts_decay_exp": (ts_decay_exp, "w"), "hump": (hump, "h"),
        # 2026-09-19 第二批：加权/回撤区间/稳健统计/持续性/共矩族
        "ts_wmean": (ts_wmean, "w"), "ts_drawdown": (ts_drawdown, "w"),
        "ts_range_pos": (ts_range_pos, "w"),
        "ts_arg_max": (ts_arg_max, "w"), "ts_arg_min": (ts_arg_min, "w"),
        "ts_median": (ts_median, "w"), "ts_iqr": (ts_iqr, "w"),
        "ts_winsor": (ts_winsor, "w"), "ts_rank_corr": (ts_rank_corr, "w"),
        "ts_streak": (ts_streak, "w"), "ts_coskew": (ts_coskew, "w"),
        "ts_triple_corr": (ts_triple_corr, "w"),
    },
    "cs": {
        "cs_rank": (cs_rank, "none"), "cs_zscore": (cs_zscore, "none"),
        "cs_winsorize": (cs_winsorize, "none"), "cs_scale": (cs_scale, "none"),
        "cs_demean": (cs_demean, "none"), "cs_robust_z": (cs_robust_z, "none"),
    },
    "binary": {
        "add": (add, "none"), "sub": (sub, "none"), "mul": (mul, "none"),
        "div": (div, "none"), "minv": (minv, "none"), "maxv": (maxv, "none"),
        "signed_diff": (signed_diff, "none"), "sfdiff": (sfdiff, "none"),
    },
    "unary": {
        "absx": (absx, "none"), "signx": (signx, "none"),
        "log1p_abs": (log1p_abs, "none"), "sigmoid": (sigmoid, "none"),
        "tanhx": (tanhx, "none"), "sqrt_abs": (sqrt_abs, "none"),
        "inv": (inv, "none"), "negx": (negx, "none"),
        "log_signed": (log_signed, "none"), "softsign": (softsign, "none"),
    },
    "cmp": {
        "gt": (gt, "none"), "lt": (lt, "none"),
    },
    "cond": {
        "where": (where, "none"),
    },
}

# 双输入算子集合（生成器需要为它们生成两棵子树）
TWO_INPUT_TS = {"ts_corr", "ts_cov", "ts_vol_ratio", "ts_resid", "ts_beta",
                "ts_wmean", "ts_rank_corr", "ts_coskew"}

# 三输入算子集合（生成器需要三棵子树）
THREE_INPUT_TS = {"ts_triple_corr"}

# 同空间二元算子（2026-09-18 空间类型规则 R2）：两子树必须同空间
# （单位匹配——add/rank 混尺度是已知废树源）；mul/div/sfdiff 自由
# （乘除=加权/比值语义，sfdiff=无量纲差）
SAME_SPACE_BINARY = {"add", "sub", "minv", "maxv", "signed_diff"}

LEAVES = ["o", "h", "l", "c", "v", "amount"]
