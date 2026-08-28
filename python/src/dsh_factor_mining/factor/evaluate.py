# coding=utf-8
"""Evaluation engine (adapted from the reference harness; all paths are portable).

Numbers are produced with the same reference definitions so the P1 parity test
can compare this package against the original harness on synthetic matrices:
section IC / IC_IR, yearly consistency, column-perm null, beta exposure,
top-N excess, decay diagnostics, composite onion, batch deflate, walk-forward.

The only intentional differences:
- no machine-specific data paths;
- test_lock lives in a user-supplied state root (never in the package).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .gates import (
    blp_sigma as _blp_sigma,
    dsr_p_from_stats as _dsr_p_from_stats,
    dsr_sr0 as _dsr_sr0,
    norm_ppf as _norm_ppf,
)
from .registry import cs_rank_corr

H = 20            # 历史默认 horizon（现从 env.calibration.horizon 读取；此处仅作文档）
COST = 0.0010     # 历史默认成本（现从 env.calibration.cost 读取）
TOP_N = 10        # 历史默认组合宽度（现从 env.calibration.top_n 读取）
MIN_POOL = 30     # minimum cross-sectional names（结构性约束，非市场口径）
DEV_END = "2021-01-01"  # 历史默认 development end（现从 env.calibration.dev_end 读取）
SEL_END = "2024-01-01"  # 历史默认 selection end（现从 env.calibration.sel_end 读取）
TRAIN_END = DEV_END

DEFAULT_STATE_ROOT = Path.cwd() / ".factor-mining"


def _test_lock_path(state_root=None):
    root = Path(state_root) if state_root else Path(
        os.environ.get("DSH_FACTOR_MINER_STATE_ROOT", DEFAULT_STATE_ROOT)
    )
    root.mkdir(parents=True, exist_ok=True)
    return root / "test_lock.json"


def _forward_returns(env):
    """fwd[t] 按 env.calibration：
    - t1（默认）: 信号 T 收盘 -> T+1 开盘入场 -> T+H 收盘出场（close[t+H]/open[t+1]-1）
    - t0       : 信号 bar 收盘即入场 -> T+H 收盘出场（close[t+H]/close[t]-1）
    H 的单位 = bar（daily=交易日；minute=bar 数）。"""
    cal = env.calibration
    c = pd.DataFrame(env.c)
    o = pd.DataFrame(env.o)
    if cal.execution == "t0":
        return (c.shift(-cal.horizon) / c - 1.0).values
    return (c.shift(-cal.horizon) / o.shift(-1) - 1.0).values


def _pit_mask(env):
    m = env.listed.copy()
    if env.amount is not None:
        m = m & (env.amount > 0)
    if env.calibration.limit_up_down_mask:
        # 一字板近似（h==l）：涨/跌停无法成交，入场信号不可执行 —— 从可交易 mask 剔除。
        m = m & ~(env.h == env.l)
    return m


def _cross_sectional_ic(F, fwd, pit, env, sig_only=False):
    T, N = F.shape
    dates = env.dates
    out = {}
    Fr = np.empty_like(F)
    fwdr = np.empty_like(fwd)
    sig_idx = set(np.arange(0, T, env.calibration.sample_step)) if sig_only else None
    for t in range(T):
        if sig_only and t not in sig_idx:
            continue
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        if m.sum() < MIN_POOL:
            continue
        x = F[t][m]
        y = fwd[t][m]
        rx = pd.Series(x).rank(pct=True).values
        ry = pd.Series(y).rank(pct=True).values
        if rx.std() == 0 or ry.std() == 0:
            continue
        out[dates[t]] = np.corrcoef(rx, ry)[0, 1]
    return pd.Series(out)


def _ic_stats(ic_series):
    ic = ic_series.dropna()
    if len(ic) < 2:
        return dict(mean=np.nan, std=np.nan, ir=np.nan, n=len(ic))
    mean = ic.mean()
    std = ic.std(ddof=1)
    ir = mean / std if std > 0 else np.nan
    return dict(mean=float(mean), std=float(std), ir=float(ir), n=len(ic))


def _yearly_sign_consistency(ic_series):
    ic = ic_series.dropna()
    if len(ic) == 0:
        return dict(consistent=0.0, yearly={})
    yearly = {}
    for yr, g in ic.groupby(ic.index.year):
        yearly[int(yr)] = float(g.mean())
    overall_sign = np.sign(ic.mean())
    if overall_sign == 0:
        return dict(consistent=0.5, yearly=yearly)
    same = sum(1 for v in yearly.values() if np.sign(v) == overall_sign)
    return dict(consistent=same / len(yearly), yearly=yearly)


def _column_perm_test(F, fwd, pit, env, n_perm=200, seed=42, train_end=None, t0_date=None, t1_date=None,
                      real_ic=None):
    def _sl(ic):
        if t0_date is not None:
            ic = ic[ic.index >= pd.Timestamp(t0_date)]
        if t1_date is not None:
            ic = ic[ic.index < pd.Timestamp(t1_date)]
        elif train_end is not None:
            ic = ic[ic.index < pd.Timestamp(train_end)]
        return ic

    rng = np.random.default_rng(seed)
    # real_ic 可由调用方传入已过滤的序列（evaluate 已算过 train_sig，避免整段重复计算）
    real_ic = _sl(real_ic) if real_ic is not None else _sl(_cross_sectional_ic(F, fwd, pit, env, sig_only=True))
    real_mean = real_ic.mean()
    T, N = F.shape
    null_means = []
    for _ in range(n_perm):
        Fp = np.empty_like(F)
        for t in range(T):
            m = pit[t] & np.isfinite(F[t])
            idx = np.where(m)[0]
            if len(idx) < 2:
                Fp[t] = F[t]
            else:
                perm = idx.copy()
                rng.shuffle(perm)
                Fp[t, idx] = F[t, perm]
        icp = _sl(_cross_sectional_ic(Fp, fwd, pit, env, sig_only=True))
        if len(icp) > 0:
            null_means.append(icp.mean())
    null_means = np.array(null_means)
    if len(null_means) == 0 or null_means.std() == 0:
        return dict(z=np.nan, p=np.nan,
                    null_mean=float(null_means.mean()) if len(null_means) else np.nan)
    z = (real_mean - null_means.mean()) / null_means.std()
    p = math.erfc(abs(z) / math.sqrt(2))
    return dict(z=float(z), p=float(p), null_mean=float(null_means.mean()))


def _block_bootstrap(net_series, L=6, B=2000, seed=42):
    """块自助推断：均值是否显著异于 0。

    circular block bootstrap 的分布中心就是样本均值本身——拿「观测均值 − 自助均值」
    当 z 分子恒≈0（旧实现的结构性死检验）。正确形式：z = mu / se，
    se 用块自助标准误以吸收自相关。
    """
    x = np.asarray(net_series, dtype=np.float64)
    n = len(x)
    if n < 10 or x.std() == 0:
        return dict(z=np.nan, p=np.nan, null_mean=float(x.mean()), real_mean=float(x.mean()))
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / L))
    starts = rng.integers(0, n, size=(B, nb))
    off = np.arange(L)
    idx = (starts[:, :, None] + off[None, None, :]) % n
    xb = x[idx.reshape(B, -1)[:, :n]]
    stats = xb.mean(axis=1)
    se = stats.std(ddof=1)
    mu = x.mean()
    z = mu / se if se > 0 else np.nan
    p = math.erfc(abs(z) / math.sqrt(2)) if np.isfinite(z) else np.nan
    return dict(z=float(z), p=float(p), null_mean=float(stats.mean()), real_mean=float(mu))


def _rolling_ic_stability(ic_series, window=252):
    ic = ic_series.dropna()
    if len(ic) < window:
        return dict(stable=np.nan, n_windows=0)
    roll = ic.rolling(window, min_periods=window).mean().dropna()
    if len(roll) == 0:
        return dict(stable=np.nan, n_windows=0)
    overall_sign = np.sign(ic.mean())
    if overall_sign == 0:
        return dict(stable=0.5, n_windows=len(roll))
    same = float((np.sign(roll.values) == overall_sign).mean())
    return dict(stable=same, n_windows=len(roll))


def _decay_diagnostics(ic_series):
    ic = ic_series.dropna()
    n = len(ic)

    def _empty():
        return dict(n_total=n, K=0, n_per_interval=0, power="insufficient",
                    per_interval_ic=[], interval_slope=np.nan, first_last_delta=np.nan,
                    delta_z=np.nan, se_delta=np.nan, monotonic_rho=np.nan, sign_flip=False,
                    yearly_slope=np.nan, decay_direction="insufficient", decay_flag="insufficient")

    if n < 20:
        return _empty()

    MIN_PER_INTERVAL = 20
    MAX_INTERVALS = 8
    K = max(2, min(MAX_INTERVALS, n // MIN_PER_INTERVAL))
    edges = np.linspace(0, n, K + 1).astype(int)
    per_interval_ic = [float(np.mean(ic.iloc[edges[i]:edges[i + 1]].values)) for i in range(K)]
    pia = np.array(per_interval_ic)
    n_per = n // K

    x = np.arange(K, dtype=np.float64)
    if K >= 3 and np.std(x) > 0:
        interval_slope = float(np.polyfit(x, pia, 1)[0])
        rx = pd.Series(x).rank().values
        ry = pd.Series(pia).rank().values
        if np.std(rx) > 0 and np.std(ry) > 0:
            monotonic_rho = float(np.corrcoef(rx, ry)[0, 1])
        else:
            monotonic_rho = np.nan
    else:
        interval_slope = np.nan
        monotonic_rho = np.nan

    first_last_delta = float(pia[-1] - pia[0])
    sign_flip = bool(np.sign(pia[0]) != 0 and np.sign(pia[-1]) != 0
                     and np.sign(pia[0]) != np.sign(pia[-1]))

    ic_std = float(np.std(ic.values, ddof=1)) if n > 1 else 0.0
    se_delta = float(np.sqrt(2.0) * ic_std / np.sqrt(n_per)) if (n_per > 1 and ic_std > 0) else np.nan
    delta_z = float(first_last_delta / se_delta) if (np.isfinite(se_delta) and se_delta > 1e-12) else np.nan

    yearly = {}
    for yr, g in ic.groupby(ic.index.year):
        yearly[int(yr)] = float(g.mean())
    years = np.array(sorted(yearly.keys()), dtype=np.float64)
    vals = np.array([yearly[int(y)] for y in years], dtype=np.float64)
    if len(years) >= 3 and np.std(years) > 0:
        yearly_slope = float(np.polyfit(years, vals, 1)[0])
    else:
        yearly_slope = np.nan

    if n >= 120:
        power = "high"
    elif n >= 60:
        power = "medium"
    else:
        power = "low"

    if sign_flip:
        direction = "reversed"
    elif not np.isfinite(delta_z):
        direction = "low_power" if power == "low" else "flat"
    elif power == "high":
        if delta_z <= -2.0:
            direction = "decaying"
        elif delta_z >= 2.0:
            direction = "rising"
        else:
            direction = "stable"
    elif power == "medium":
        if delta_z <= -2.0:
            direction = "weak_decaying"
        elif delta_z >= 2.0:
            direction = "weak_rising"
        else:
            direction = "flat"
    else:
        direction = "low_power"

    return dict(n_total=n, K=K, n_per_interval=n_per, power=power,
                per_interval_ic=per_interval_ic, interval_slope=interval_slope,
                first_last_delta=first_last_delta, delta_z=delta_z, se_delta=se_delta,
                monotonic_rho=monotonic_rho, sign_flip=sign_flip,
                yearly_slope=yearly_slope, decay_direction=direction, decay_flag=direction)


def _max_corr_with_registered(F, env, registry_factors):
    if not registry_factors:
        return None
    Fd = pd.DataFrame(F)
    max_abs = 0.0
    for name, G in registry_factors.items():
        G = np.asarray(G, dtype=np.float64)
        if G.shape != F.shape:
            continue
        corrs = []
        for t in range(len(env.dates)):
            m = np.isfinite(Fd.iloc[t].values) & np.isfinite(G[t])
            if m.sum() < MIN_POOL:
                continue
            rF = pd.Series(Fd.iloc[t].values[m]).rank(pct=True)
            rG = pd.Series(G[t][m]).rank(pct=True)
            if rF.std() == 0 or rG.std() == 0:
                continue
            corrs.append(np.corrcoef(rF, rG)[0, 1])
        if corrs:
            max_abs = max(max_abs, abs(float(np.mean(corrs))))
    return float(max_abs)


def _beta_exposure(F, env):
    c = pd.DataFrame(env.c)
    ret1 = c.pct_change()
    mr = ret1.mean(axis=1, skipna=True)
    re_ema = ret1.ewm(span=252, min_periods=252).mean()
    pe_ema = mr.ewm(span=252, min_periods=252).mean()
    rp_ema = (ret1.multiply(mr, axis=0)).ewm(span=252, min_periods=252).mean()
    p2_ema = (mr ** 2).ewm(span=252, min_periods=252).mean()
    beta = rp_ema.sub(re_ema.multiply(pe_ema, axis=0)).div(p2_ema.sub(pe_ema ** 2).clip(1e-8), axis=0)
    beta_lag = beta.shift(1)
    Fd = pd.DataFrame(F)
    corrs = []
    for t in range(len(env.dates)):
        m = np.isfinite(Fd.iloc[t].values) & np.isfinite(beta_lag.iloc[t].values)
        if m.sum() < MIN_POOL:
            continue
        rF = pd.Series(Fd.iloc[t].values[m]).rank(pct=True)
        rB = pd.Series(beta_lag.iloc[t].values[m]).rank(pct=True)
        if rF.std() == 0 or rB.std() == 0:
            continue
        corrs.append(np.corrcoef(rF, rB)[0, 1])
    return float(np.mean(corrs)) if corrs else np.nan


def _top_n_excess(F, fwd, pit, env, top_n=None, cost=None, t0=0, t1=None):
    cal = env.calibration
    if top_n is None:
        top_n = cal.top_n
    if cost is None:
        cost = cal.cost
    hz = cal.horizon
    T, N = F.shape
    if t1 is None:
        t1 = T
    sig_idx = np.arange(0, T, cal.sample_step)
    sig_idx = sig_idx[(sig_idx >= t0) & (sig_idx < t1)]
    holdings = None
    rows = []
    for ti in sig_idx:
        if ti + hz >= T:
            continue
        m = pit[ti] & np.isfinite(F[ti]) & np.isfinite(fwd[ti])
        if m.sum() < top_n:
            continue
        order = np.argsort(-F[ti][m], kind='stable')[:top_n]
        cols = np.where(m)[0][order]
        top_set = set(cols.tolist())
        if holdings is None:
            turn = 1.0
        else:
            turn = len(top_set.symmetric_difference(holdings)) / (2 * top_n)
        holdings = top_set
        pool_mean = np.nanmean(fwd[ti][m])
        top_mean = np.nanmean(fwd[ti][cols])
        gross = top_mean - pool_mean
        net = gross - turn * 2 * cost
        rows.append((env.dates[ti], gross, net, turn))
    return pd.DataFrame(rows, columns=['date', 'gross', 'net', 'turn'])


def _tail_aligned_corr(sa: list | None, sb: list | None,
                       min_overlap: int = 20) -> float | None:
    """尾对齐 IC 相关（v3 F1 修复）：可测必测，缺证据才保守。

    v2 按 sketch **长度**分组测相关——长度=序列长度随回看窗变化（41~85
    点、18 种长度），2701 对里只实测 275 对（10.2%），2426 对被静默置
    独立。IC 序列共享同一 train 窗口，尾部天然对齐：取尾部 min(la,lb)
    重叠段实测。重叠 < min_overlap（短窗口噪声相关不可信）→ None
    （调用方按独立计——独立是 E[max|X|] 的最贵假设，缺证据=保守）。"""
    if not isinstance(sa, (list, tuple)) or not isinstance(sb, (list, tuple)):
        return None
    k = min(len(sa), len(sb))
    if k < min_overlap:
        return None
    a = np.asarray(sa[-k:], dtype=np.float64)
    b = np.asarray(sb[-k:], dtype=np.float64)
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        return None
    sa_, sb_ = a.std(ddof=1), b.std(ddof=1)
    if sa_ <= 0 or sb_ <= 0:
        return None
    return float(np.mean((a - a.mean()) * (b - b.mean())) / (sa_ * sb_))


class _LuckSampler:
    """选择运气采样器（v3 2026-08-21）：E[max|X|] 直算。

    X ~ N(0, R)，R = trail 试验 IC 相关矩阵（尾对齐 |ρ| 全对实测，
    缺测对 → 跨 horizon 先验 → 0）。bar_sigma = E[max_i |X_i|]（σ 单位）。

    实现：公共随机数（CRN）——Z 列独立固定种子，条件采样增列
    x_new = X@v + √s·z（v=R⁻¹r，s=1−rᵀv）。旧列不动、加试验只多一个
    |x| 候选 → E[max|X|] 估计量**天然单调不减**（定理，非补丁：superset
    的逐点 max ≥ subset）。增量 O(M²)，重启 rebuild O(M³+K·M²)，
    M≤千级秒级完成（K=2 万抽样亚秒）。

    附带产出（同一份状态，零额外成本）：
    - p_fw(z) = P(max|X| ≥ z)：精确族错误 p（高斯模型下的诊断字段，
      不带偏度/峰度校正——主门仍走 t-stat 路径）
    - nu_telemetry()：νᵢ = 1/diag(R⁻¹)ᵢ（试验 i 的残差方差占比——
      它的 IC 里有多大比例不能被其余试验解释。「独立试验个数」不是
      良定义量；ν 是关系属性：同一因子在空 trail 里 ν=1，混在 74 个
      亲戚中间 ν≈0。中位 ν / ν>0.5 计数是探索多样性的遥测）。
    """

    K = 20_000
    SEED = 42

    def __init__(self):
        self._keys: list = []
        self._sig: list = []                # 每 key 的 sketch 指纹（None=pending）
        self._R: np.ndarray | None = None
        self._Rinv: np.ndarray | None = None
        self._X: np.ndarray | None = None   # (K, M) 样本
        self._mx: np.ndarray | None = None  # (K,) 逐样本 max|X|

    # ---- Z 列独立固定种子（可扩展：增列不扰动旧列 → 单调性成立） ----
    @staticmethod
    def _z_col(j: int, k: int) -> np.ndarray:
        return np.random.default_rng(_LuckSampler.SEED + 1000 * (j + 1)).standard_normal(k)

    def _build_R(self, trials: dict, prior_fn=None) -> np.ndarray:
        """R：对角 1；尾对齐 |ρ| 全对实测；缺测对 → prior_fn(i,j) → 0。"""
        keys = list(trials)
        M = len(keys)
        R = np.eye(M)
        for i in range(M):
            for j in range(i + 1, M):
                c = _tail_aligned_corr(trials[keys[i]], trials[keys[j]])
                if c is None and prior_fn is not None:
                    p = prior_fn(keys[i], keys[j])
                    c = float(p) if isinstance(p, (int, float)) else None
                if c is not None and np.isfinite(c):
                    R[i, j] = R[j, i] = abs(c)
        return R

    @staticmethod
    def _sig_of(s) -> tuple | None:
        """sketch 指纹（可哈希）；None/pending → None。"""
        if not isinstance(s, (list, tuple)):
            return None
        return tuple(s)

    def _rebuild(self, trials: dict, prior_fn=None) -> None:
        """全量重建：PSD 投影（特征值 clip）→ Cholesky → X = Z@Aᵀ。"""
        keys = list(trials)
        M = len(keys)
        self._keys = keys
        self._sig = [self._sig_of(trials[k]) for k in keys]
        if M == 0:
            self._R = self._Rinv = self._X = self._mx = None
            return
        R = self._build_R(trials, prior_fn)
        w, V = np.linalg.eigh(R)
        w = np.clip(w, 1e-8, None)  # PSD 投影（|ρ| 组装可能非 PD）
        A = V * np.sqrt(w)
        Z = np.column_stack([self._z_col(j, self.K) for j in range(M)])
        self._R = R
        self._Rinv = (V / w) @ V.T
        self._X = Z @ A.T
        self._mx = np.abs(self._X).max(axis=1)

    def _extend(self, new_key, r: np.ndarray, Rinv: np.ndarray,
                sig: tuple | None = None) -> bool:
        """条件采样增列（O(M²)）：失败（非 PD 增长）返回 False → 上层 rebuild。"""
        v = Rinv @ r
        s = 1.0 - float(r @ v)
        if s <= 1e-8:
            return False
        x_new = self._X @ v + math.sqrt(s) * self._z_col(len(self._keys), self.K)
        # R⁻¹ 块扩展：[[R⁻¹+vvᵀ/s, −v/s], [−vᵀ/s, 1/s]]
        M = len(self._keys)
        Rinv_new = np.empty((M + 1, M + 1))
        Rinv_new[:M, :M] = Rinv + np.outer(v, v) / s
        Rinv_new[:M, M] = Rinv_new[M, :M] = -v / s
        Rinv_new[M, M] = 1.0 / s
        R_new = np.empty((M + 1, M + 1))
        R_new[:M, :M] = self._R
        R_new[:M, M] = R_new[M, :M] = r
        R_new[M, M] = 1.0
        self._keys = self._keys + [new_key]
        self._sig = self._sig + [sig]
        self._R, self._Rinv = R_new, Rinv_new
        self._X = np.column_stack([self._X, x_new])
        self._mx = np.maximum(self._mx, np.abs(x_new))
        return True

    def _pop_last(self) -> None:
        """弹出最后一列（O(M²)）：X 截列（高斯边缘分布不变）；
        Rinv 用 marginal precision 恢复：R11⁻¹ = P11 − p12p12ᵀ/p22。"""
        M = len(self._keys)
        if M == 0:
            return
        i = M - 1
        P = self._Rinv
        p22 = float(P[i, i])
        if p22 > 1e-12:
            P11 = P[:i, :i] - np.outer(P[:i, i], P[:i, i]) / p22
        else:  # 数值兜底（理论不达：对角元 ≥ 1/diag(R) > 0）
            P11 = np.linalg.inv(self._R[:i, :i]) if i > 0 else np.zeros((0, 0))
        self._keys = self._keys[:-1]
        self._sig = self._sig[:-1]
        self._R = self._R[:i, :i] if i > 0 else None
        self._Rinv = P11 if i > 0 else None
        self._X = self._X[:, :i] if i > 0 else None
        if self._X is not None and self._X.shape[1] > 0:
            self._mx = np.abs(self._X).max(axis=1)
        else:
            self._mx = None

    def sync(self, trials: dict, prior_fn=None) -> None:
        """对齐试验集（增量优先，分歧全量重建）。

        实际调用模式两类（都不触发重建）：
        - 单因子循环：provisional（尾部 +1 pending）→ 写盘转正（尾部 pop
          后按实测相关重新条件采样）
        - batch：M 个新 key 一次性追加（逐个条件采样）
        分歧（key 重排/删除/中间 sketch 变化）或非 PD 增长 → 全量重建。
        """
        keys = list(trials)
        if not keys:
            self._keys, self._sig = [], []
            self._R = self._Rinv = self._X = self._mx = None
            return
        if self._X is None or not self._keys:
            self._rebuild(trials, prior_fn)
            return
        if keys[:len(self._keys)] != self._keys:
            self._rebuild(trials, prior_fn)
            return
        new_sig = [self._sig_of(trials[k]) for k in keys]
        # 尾部 pending 转正：旧尾部 sig=None 的列弹出后按新状态重加
        n_old = len(self._keys)
        while (n_old > 0 and self._sig[n_old - 1] is None
               and n_old <= len(keys) and keys[n_old - 1] == self._keys[n_old - 1]):
            self._pop_last()
            n_old -= 1
        if keys[:n_old] != self._keys or new_sig[:n_old] != self._sig:
            self._rebuild(trials, prior_fn)
            return
        # pending 全弹出后状态为空（首条 trail：唯一旧列即 pending）→
        # 追加循环拿 None Rinv 会崩（盖章 try/except 吞掉 → 首条静默无章）。
        if not self._keys:
            self._rebuild(trials, prior_fn)
            return
        # 纯追加：新 keys 逐个条件采样（r 用尾对齐实测 + 先验兜底）
        for j in range(n_old, len(keys)):
            r = np.zeros(len(self._keys))
            for i, k in enumerate(self._keys):
                c = _tail_aligned_corr(trials[k], trials[keys[j]])
                if c is None and prior_fn is not None:
                    p = prior_fn(k, keys[j])
                    c = float(p) if isinstance(p, (int, float)) else None
                if c is not None and np.isfinite(c):
                    r[i] = abs(c)
            if not self._extend(keys[j], r, self._Rinv, sig=new_sig[j]):
                self._rebuild(trials, prior_fn)
                return

    def bar_sigma(self) -> float:
        """E[max|X|]（σ 单位）。M=0 → 0；M=1 → E|Z|=√(2/π)≈0.798
        （连符号都是选出来的——冷启动即有选运底价）。"""
        if self._mx is None or len(self._mx) == 0:
            return 0.0
        return float(self._mx.mean())

    def p_fw(self, z: float) -> float | None:
        """精确族错误 p：P(max|X| ≥ z)（诊断字段，非门）。"""
        if self._mx is None or len(self._mx) == 0:
            return None
        try:
            z = float(z)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(z):
            return None
        return float((self._mx >= z).mean())

    def nu_telemetry(self) -> dict | None:
        """ν 遥测：每试验残差方差占比 νᵢ=1/diag(R⁻¹)ᵢ。"""
        if self._Rinv is None or self._Rinv.shape[0] == 0:
            return None
        d = np.clip(np.diag(self._Rinv), 1.0, None)
        nu = 1.0 / d
        return {"min": float(nu.min()), "median": float(np.median(nu)),
                "high_count": int((nu > 0.5).sum()), "M": int(nu.shape[0])}


def _deflated_sharpe_p(ic_series, bar_sigma: float | None = None,
                       pool_std: float | None = None,
                       n_trials: float = 1) -> dict:
    """Deflated Sharpe Ratio 单因子 p（v3 2026-08-21：bar_sigma 口径）。

    对 IC 序列做偏度/峰度校正的 t 统计，并按「选择运气 bar」折减——
    sr0 = bar_sigma·pool_std（E[max|X|]，见 _LuckSampler）。同一批假设
    试得越多、族内越独立，bar 越高，门槛自动越高。

    - bar_sigma（门参数）：bridge 从 trail_engine 的试验相关矩阵直算注入；
      None/0 = 直调单检验口径（无选择折减——选择折减是 bridge 层职责，
      引擎侧硬统计，不依赖 agent 自觉）。
    - n_trials：纯遥测（试验计数 M），不参与门——v2 的谱 N_eff→B-LP 链条
      退役（三处失真见 _dsr_sr0 docstring）。
    - pool_std：已试假设 IC_IR 分布的 std（bridge 从 trail_engine 实测或
      null 地形分位数估计传入）。bar_sigma>0 而无 pool_std → 拒绝给 p
      （不给不可信数字）。
    """
    ic = ic_series.dropna()
    n = len(ic)
    if n < 5 or ic.std(ddof=1) == 0:
        return {"p": None, "n_trials": float(n_trials), "n_obs": n, "note": "样本不足"}
    sr = float(ic.mean() / ic.std(ddof=1))
    g3 = float(((ic - ic.mean()) ** 3).mean() / max(ic.std(ddof=1) ** 3, 1e-18))
    g4 = float(((ic - ic.mean()) ** 4).mean() / max(ic.std(ddof=1) ** 4, 1e-18))
    sr0, scale_note = _dsr_sr0(bar_sigma, pool_std)
    if sr0 is None:
        return {"p": None, "n_trials": float(n_trials), "n_obs": n, "sr_hat": sr,
                "skew": g3, "kurt": g4, "sr0": None,
                "note": scale_note}
    p = _dsr_p_from_stats(sr, g3, g4, n, bar_sigma, pool_std)
    return {"p": p, "n_trials": float(n_trials), "n_eff": float(n_trials),
            "n_obs": n, "bar_sigma": float(bar_sigma) if bar_sigma else 0.0,
            "sr_hat": sr, "sr0": sr0, "skew": g3, "kurt": g4,
            "pool_std": float(pool_std) if pool_std else None,
            "scale_note": scale_note}


def _train_sensitivity(ic_sig, env) -> dict:
    """启动点敏感性（诊断，不改变验收）：dev_end ±1/2/3 月扰动下 train 区结论稳定性。

    硬约束：只扰动 dev_end（侵入 selection 区可接受）；sel_end 绝不扰动——
    test 只能被消费一次，不得进入任何扰动扫描。
    """
    from ..discipline import red_flags_and_verdict  # noqa: F401 — 保持 evaluate 独立可测

    dev_end = pd.Timestamp(env.calibration.dev_end)
    sel_end = pd.Timestamp(env.calibration.sel_end)
    variants = []
    for months in (-3, -2, -1, 1, 2, 3):
        d = dev_end + pd.DateOffset(months=months)
        if d >= sel_end:
            d = sel_end - pd.Timedelta(days=1)
        if d <= ic_sig.index.min():
            continue
        sub = ic_sig[ic_sig.index < d]
        if len(sub) < 10:
            continue
        mean, std = float(sub.mean()), float(sub.std(ddof=1)) if len(sub) > 1 else float("nan")
        variants.append({"dev_end": str(d.date()), "offset_months": months,
                         "ic_mean": mean, "ic_ir": mean / std if std and std > 0 else None,
                         "n": len(sub)})
    if not variants:
        return {"variants": [], "stability": None, "verdict": "insufficient"}
    base = ic_sig[ic_sig.index < dev_end]
    base_ir = float(base.mean() / base.std(ddof=1)) if len(base) > 1 and base.std(ddof=1) > 0 else None
    irs = [v["ic_ir"] for v in variants if v["ic_ir"] is not None]
    same_sign = all(np.sign(x) == np.sign(base_ir) for x in irs) if base_ir else False
    rel_spread = ((max(irs) - min(irs)) / abs(base_ir)) if irs and base_ir else None
    if same_sign and (rel_spread is None or rel_spread < 0.5):
        verdict = "stable"
    elif same_sign:
        verdict = "magnitude_fragile"
    else:
        verdict = "sign_fragile"
    return {"variants": variants, "base_ic_ir": base_ir,
            "rel_spread": rel_spread, "verdict": verdict,
            "note": "只扰动 dev_end；sel_end/test 绝不进入扰动扫描"}


def _env_horizon_view(env, horizon):
    """per-call horizon 视图（v2 2026-08-20 申报制）：共享数据数组、仅口径覆盖。

    horizon=None 或等于主 horizon → 原样返回。视图的 ic_sample_every 同步
    放大至 max(原值, h)：per-call horizon 放大时保持 IC 不重叠铁律（D5）——
    原值 0（=horizon）随视图 horizon 自适应；原值 >0 且 < h 时抬到 h。
    """
    if horizon is None:
        return env
    h = int(horizon)
    if h == env.calibration.horizon:
        return env
    from dataclasses import replace as _replace
    cal = env.calibration
    ise = int(cal.ic_sample_every or 0)
    if 0 < ise < h:
        ise = h
    new_cal = _replace(cal, horizon=h, ic_sample_every=ise)
    view = object.__new__(type(env))
    view.__dict__.update(env.__dict__)
    view.calibration = new_cal
    return view


def _date_shift_placebo(F, fwd, pit, env, shifts=(5, 10, 21)):
    """date-shift placebo（2026-08-24 用户认可的过拟合检验）：

    因子矩阵整体后移 k 个 bar（今天的信号 = k 天前的信号），截面排序
    保留、时间对齐破坏。真因子的 IC 应随 shift 崩向 0；shift 后仍
    显著 = 因子在拟合慢变量与市场 regime 的巧合（截面置换测不到的
    伪相关通道——它破坏截面关联，这里破坏时间对齐）。"""
    out = {}
    T = F.shape[0]
    base = None
    ic0 = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    if len(ic0) >= 2:
        base = float(ic0.mean())
    for k in shifts:
        if k >= T:
            continue
        Fs = np.full_like(F, np.nan)
        Fs[k:] = F[:-k]
        ic_k = _cross_sectional_ic(Fs, fwd, pit, env, sig_only=True)
        out[str(k)] = (float(ic_k.mean())
                       if len(ic_k) >= 2 else None)
    return {"unshifted_mean_ic": base, "shifted_mean_ic": out}


def evaluate(F, env, train_end=None, verbose=False, n_trials: float = 1,
             pool_std: float | None = None, horizon=None,
             bar_sigma: float | None = None):
    env = _env_horizon_view(env, horizon)
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    if env.calibration.dev_end is None:
        raise ValueError(
            "三区分界未设置：calibration.dev_end/sel_end 为空（直调 API 需显式构造 "
            "Calibration(dev_end=..., sel_end=...)；config 路径会按数据自动划分）")
    train_end = train_end or env.calibration.dev_end

    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    if len(ic_sig) == 0:
        return dict(error="无有效截面（PIT mask 后标的不足 MIN_POOL）")

    t_end = np.searchsorted(env.dates, pd.Timestamp(train_end))
    train_sig = ic_sig[ic_sig.index < pd.Timestamp(train_end)]
    train_sig_stats = _ic_stats(train_sig)

    result = {
        "signal": None,
        # 实际生效赌注（v2 申报制）：trail 按 (source_hash, horizon) 计账的 key、
        # submit 重算按 horizon 取 per-horizon pool_std 基线
        "horizon": env.calibration.horizon,
        "ic_mean_train": train_sig_stats["mean"],
        "ic_ir_train": train_sig_stats["ir"],
        "ic_n_train": train_sig_stats["n"],
        "yearly_consistency_train": _yearly_sign_consistency(train_sig),
        "column_perm_train": _column_perm_test(F, fwd, pit, env, train_end=train_end,
                                                real_ic=train_sig),
        "rolling_ic_stability_train": _rolling_ic_stability(train_sig),
        "decay_train": _decay_diagnostics(train_sig),
        "date_shift": _date_shift_placebo(F, fwd, pit, env),
        "beta_exposure": _beta_exposure(F, env),
        "topn": None,
    }

    topn_train = _top_n_excess(F, fwd, pit, env, t0=0, t1=t_end)
    if len(topn_train) > 0:
        result["topn"] = _summarize_topn(topn_train, env)

    # 尾部三件套计算层（2026-08-25 用户批准设计）：自动计算 = 自动计数
    # （Phase 5 tail 账本据此累计，堵可选停时）；train 区、只 top 侧；
    # 本层只标注不门，失败不阻断评估主结果。
    try:
        from .tail import tail_metrics
        result["tail"] = tail_metrics(F, env)
    except Exception as _te:
        result["tail"] = {"error": f"{type(_te).__name__}: {_te}"[:120]}

    # 纪律层（discipline）：deflated p + 分界敏感性 + RED_FLAG + 结构化 verdict
    from ..discipline import red_flags_and_verdict
    result["deflated_train"] = _deflated_sharpe_p(train_sig, bar_sigma=bar_sigma,
                                                  pool_std=pool_std,
                                                  n_trials=n_trials)
    # A2（2026-08-19 trail 级 N_eff）：train 区 IC 序列挂到结果——bridge 摘出
    # 存 trail_engine 的 ic_series_sketch（同族参数变体的 IC 序列高度相关，
    # 是簇聚类的好代理），不进 agent 可见返回 / registry。
    result["ic_series_train"] = [round(float(x), 4) for x in train_sig.tolist()]
    result["train_sensitivity"] = _train_sensitivity(ic_sig, env)
    gv = red_flags_and_verdict(result, region="train")
    result["verdict"] = gv["verdict"]
    result["red_flags"] = gv["red_flags"]

    if verbose:
        _print_result(result)
    return result


def _summarize_topn(topn, env):
    cal = env.calibration
    # 交易每 sample_step 根 bar 发生一次（sig_idx 步长），年化倍数用它而非 horizon
    ann_factor = cal.annualization / cal.sample_step
    yearly_net = {}
    for yr, g in topn.groupby(topn['date'].dt.year):
        yearly_net[int(yr)] = float(g['net'].mean()) * ann_factor * 100
    return {
        "net_annual": float(topn['net'].mean()) * ann_factor * 100,
        "gross_annual": float(topn['gross'].mean()) * ann_factor * 100,
        "turn_avg": float(topn['turn'].mean()),
        "yearly_net": yearly_net,
        "block_bootstrap": _block_bootstrap(topn['net'].values),
    }


def _region_diagnostic(F, fwd, pit, env, region_ic, region_name, t0_date, t1_date):
    cp = _column_perm_test(F, fwd, pit, env, t0_date=t0_date, t1_date=t1_date)
    t0 = 0 if t0_date is None else np.searchsorted(env.dates, pd.Timestamp(t0_date))
    t1 = None if t1_date is None else np.searchsorted(env.dates, pd.Timestamp(t1_date))
    topn = _top_n_excess(F, fwd, pit, env, t0=t0, t1=t1)
    stats = _ic_stats(region_ic)
    result = {
        "region": region_name,
        "ic_mean": stats["mean"], "ic_ir": stats["ir"], "ic_n": stats["n"],
        "yearly_consistency": _yearly_sign_consistency(region_ic),
        "column_perm": cp,
        "rolling_ic_stability": _rolling_ic_stability(region_ic),
        "decay": _decay_diagnostics(region_ic),
        "beta_exposure": _beta_exposure(F, env),
        "topn": _summarize_topn(topn, env) if len(topn) > 0 else None,
    }
    # test 区尾块（WS3 2026-08-25）：最小集 {region, spread_ir, tail_ic,
    # k_typical}，无 null——placebo 是 train 区 null 语义，test 的判定是
    # 真实表现 vs 预先声明（也不给任何在 test 区反复试的机会）。spread
    # 统计量在 test 行集（全局采样网格 ∩ [t0, t1)）上算，与 topn 同行集。
    # 两条防线（tail_ledger stage 过滤 + submit 反查排除）保证它绝不
    # 混入计价/准入。
    if region_name == "test":
        try:
            from .tail import K_FRAC, _topk_block
            step = max(int(env.calibration.sample_step), 1)
            sig = np.arange(0, env.T, step)
            rows = sig[(sig >= int(t0))
                       & (sig < (env.T if t1 is None else int(t1)))]
            spreads, tail_ics, _ics, ks, _pairs, _turns = _topk_block(
                F, fwd, pit, rows, K_FRAC)
            tail = None
            if len(spreads) >= 10:
                sp = np.array(spreads)
                sd = sp.std(ddof=1)
                ti = np.array(tail_ics) if tail_ics else None
                tail = {
                    "region": "test",
                    "k_frac": K_FRAC,
                    "spread_ir": (round(float(sp.mean() / sd), 4) + 0.0
                                  if sd > 0 else None),
                    "tail_ic": (round(float(ti.mean() / ti.std(ddof=1)), 4) + 0.0
                                if ti is not None and len(ti) >= 5
                                and ti.std(ddof=1) > 0 else None),
                    "k_typical": int(round(float(np.mean(ks)))) if ks else 0,
                    "n_days": int(len(sp)),
                }
            result["tail"] = tail
        except Exception as _te:
            result["tail"] = {"error": f"{type(_te).__name__}: {_te}"[:120]}
    from ..discipline import red_flags_and_verdict
    gv = red_flags_and_verdict(result, region=region_name)
    result["verdict"] = gv["verdict"]
    result["red_flags"] = gv["red_flags"]
    return result


def evaluate_selection(F, env, verbose=False):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    if env.calibration.dev_end is None or env.calibration.sel_end is None:
        raise ValueError("三区分界未设置：直调 API 需显式构造 Calibration(dev_end=..., sel_end=...)")
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    dev_end, sel_end = env.calibration.dev_end, env.calibration.sel_end
    sel_sig = ic_sig[(ic_sig.index >= pd.Timestamp(dev_end)) & (ic_sig.index < pd.Timestamp(sel_end))]
    if len(sel_sig) == 0:
        return dict(error="selection 区无有效截面")
    result = _region_diagnostic(F, fwd, pit, env, sel_sig, "selection", dev_end, sel_end)
    if verbose:
        _print_region_result(result)
    return result


def _read_test_lock(lock_path):
    try:
        with open(lock_path, encoding='utf-8') as f:
            lock = json.load(f)
        return lock if isinstance(lock, dict) else {}
    except Exception:
        return {}


def _write_test_lock(lock_path, payload):
    """原子写锁文件（tmp + os.replace——裸 open('w') 崩溃留半截 JSON）。"""
    tmp = lock_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(lock_path))


def claim_test_lock(state_root=None, source_hash=None, fingerprint=None):
    """test 消费预约（claim-then-compute，并行 P0）。

    原实现「检查→算完 test→写锁」有完整 TOCTOU 窗口：两个进程可同时
    通过检查各消费一次。现在：写锁内查 consumed / 他进程 live claim →
    原子写 claim → 计算 → finalize。计算崩溃留下的 claim 不算消费
    （test 没算出结果就没被烧掉），PID 存活检测回收 stale claim。"""
    from ..filelock import pid_alive, state_write_lock

    lock_path = _test_lock_path(state_root)
    with state_write_lock(lock_path.parent):
        existing = _read_test_lock(lock_path)
        if existing.get("consumed", False):
            raise RuntimeError("test 已被消费（test_lock.json）。test 是最终消耗品，禁止反复评估调参。")
        claim = existing.get("claim") or {}
        cpid = claim.get("pid")
        if cpid and int(cpid) != os.getpid() and pid_alive(int(cpid)):
            raise RuntimeError(
                f"test 正在被另一进程评估（PID={cpid}，始于 {claim.get('ts', '?')}）——"
                "等它完成后再试；确认其已崩溃可手动删除 test_lock.json 后重试。")
        _write_test_lock(lock_path, {
            "consumed": False,
            "claim": {"pid": os.getpid(),
                      "ts": pd.Timestamp.now().strftime("%Y-%m-%dT%H:%M:%S"),
                      "source_hash": source_hash, "fingerprint": fingerprint},
            "note": "test 消费进行中（claim-then-compute）——崩溃残留的 claim "
                    "不算消费，按 PID 存活检测回收",
        })


def release_test_lock(state_root=None):
    """释放 claim（数据性失败路径：test 区无有效截面不是消费）。"""
    from ..filelock import state_write_lock

    lock_path = _test_lock_path(state_root)
    with state_write_lock(lock_path.parent):
        _write_test_lock(lock_path, {"consumed": False})


def finalize_test_lock(lock_payload: dict, state_root=None):
    """test 消费落锤（计算成功后调用；payload 带 diagnosis 上下文）。"""
    from ..filelock import state_write_lock

    lock_path = _test_lock_path(state_root)
    with state_write_lock(lock_path.parent):
        _write_test_lock(lock_path, lock_payload)


def evaluate_test(F, env, verbose=False, state_root=None, source_hash=None,
                  fingerprint=None):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    if env.calibration.sel_end is None:
        raise ValueError("三区分界未设置：直调 API 需显式构造 Calibration(dev_end=..., sel_end=...)")
    claim_test_lock(state_root, source_hash, fingerprint)
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    sel_end = env.calibration.sel_end
    test_sig = ic_sig[ic_sig.index >= pd.Timestamp(sel_end)]
    if len(test_sig) == 0:
        release_test_lock(state_root)
        return dict(error="test 区无有效截面")
    result = _region_diagnostic(F, fwd, pit, env, test_sig, "test", sel_end, None)
    # 消费上下文（批次1a）：谁、什么口径、什么结果消费了这一次性的 test
    import datetime as _dt
    lock_payload = {
        "consumed": True,
        "note": "test 已消费一次，禁止再次评估",
        "consumed_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_hash": source_hash,
        "fingerprint": fingerprint,
        "engine_version": __import__("dsh_factor_mining", fromlist=["__version__"]).__version__,
        "diagnosis_summary": {"ic_ir": result.get("ic_ir"), "ic_mean": result.get("ic_mean"),
                              "ic_n": result.get("ic_n"), "verdict": result.get("verdict"),
                              "spread_ir_test": (result.get("tail") or {}).get("spread_ir")
                              if isinstance(result.get("tail"), dict) else None},
        "calibration": {"dev_end": env.calibration.dev_end,
                        "sel_end": env.calibration.sel_end,
                        "horizon": env.calibration.horizon},
    }
    finalize_test_lock(lock_payload, state_root)
    if verbose:
        _print_region_result(result)
    return result


def _print_region_result(r):
    print("=" * 60)
    print(f"[{r['region']}] IC {r['ic_mean']:+.4f} (IR {r['ic_ir']:+.3f})  n={r['ic_n']}")
    cp = r['column_perm']
    print(f"  column-perm z={cp['z']:+.2f} p={cp['p']:.3f}")
    dc = r['decay']
    print(f"  衰减: {dc['decay_direction']}  power={dc['power']}  n={dc['n_total']}")
    if r['topn']:
        print(f"  top-N 净超额 {r['topn']['net_annual']:+.1f}%/yr")
    print("=" * 60)


def corr_with_registered(F, env, registry_factors):
    return _max_corr_with_registered(F, env, registry_factors)


def evaluate_composite(composite_F, parts, env, train_end=None):
    train_end = train_end or env.calibration.dev_end
    comp = evaluate(composite_F, env, train_end=train_end)
    part_evals = {name: evaluate(G, env, train_end=train_end) for name, G in parts.items()}

    def _topnet(r):
        t = r.get('topn') if isinstance(r, dict) else None
        return t.get('net_annual', np.nan) if isinstance(t, dict) else np.nan

    L0 = {name: r.get('ic_ir_train', np.nan) for name, r in part_evals.items()}
    best_name = None
    best_icir = -np.inf
    best_net = -np.inf
    best_F = None
    for name, r in part_evals.items():
        icir = r.get('ic_ir_train', np.nan)
        net = _topnet(r)
        if np.isfinite(icir) and icir > best_icir:
            best_icir = icir
            best_name = name
            best_F = parts[name]
        if np.isfinite(net):
            best_net = max(best_net, net)

    comp_icir = comp.get('ic_ir_train', np.nan)
    comp_net = _topnet(comp)
    inc_icir = (comp_icir - best_icir) if np.isfinite(comp_icir) and np.isfinite(best_icir) else np.nan
    inc_net = (comp_net - best_net) if np.isfinite(comp_net) and np.isfinite(best_net) else np.nan
    corr_best = _max_corr_with_registered(composite_F, env, {best_name: best_F}) if best_F is not None else None

    return {
        "composite": comp,
        "parts": part_evals,
        "onion": {
            "L0_parts": L0,
            "L1_composite": comp_icir,
            "synthesis_gain": inc_icir,
        },
        "diagnosis": {
            "ic_ir_delta": inc_icir,
            "net_delta": inc_net,
            "best_part": best_name,
            "best_ic_ir": best_icir if np.isfinite(best_icir) else np.nan,
            "best_net_annual": best_net if np.isfinite(best_net) else np.nan,
            "corr_vs_best": corr_best,
        }
    }


def _sample_signal_days(F_dict, env):
    sig_idx = np.arange(0, env.T, env.calibration.sample_step)
    out = {}
    for n, F in F_dict.items():
        out[n] = np.asarray(F, dtype=np.float64)[sig_idx]
    return out


def evaluate_batch(F_dict, env, train_end=None, horizon=None, pool_std=None,
                   factors=None):
    """批量评估 + 批内族口径（E[max|X|]）。

    factors（P3 并行批次）：可注入预计算的成员诊断（worker 进程池路径
    逐成员并行算 evaluate 后传入；None = 本进程顺序算）。成员级失败用
    {"error": ...} 标记（部分失败隔离——坏成员不烧整批），error 成员
    不进家族统计；家族口径（ρ̄/bar_sigma_b/deflated）只在有效成员上算。"""
    train_end = train_end or env.calibration.dev_end
    names = list(F_dict.keys())
    factors = dict(factors) if isinstance(factors, dict) else {}
    missing = [n for n in names if not isinstance(factors.get(n), dict)]
    for n in missing:
        factors[n] = evaluate(F_dict[n], env, train_end=train_end, horizon=horizon)
    ok = [n for n in names if not factors[n].get("error")]
    M = len(ok)
    if M == 0:
        return {"factors": factors,
                "batch": {"M": 0, "M_requested": len(names),
                          "note": "全部成员失败（逐成员 error 见 factors[*].error）"}}

    # v2 申报制：整批同一声明 horizon（不同 horizon 的成本/口径不同，混批无意义）
    p_single = {}
    for n in ok:
        cp = factors[n].get("column_perm_train") or {}
        p_single[n] = cp.get("p", np.nan)

    if M >= 2:
        Fs = _sample_signal_days(F_dict, env)
        corrs = []
        for i in range(M):
            for j in range(i + 1, M):
                c = cs_rank_corr(Fs[ok[i]], Fs[ok[j]])
                if np.isfinite(c):
                    corrs.append(abs(c))
        rho_bar = float(np.mean(corrs)) if corrs else 0.0
    else:
        rho_bar = 0.0

    # v3（2026-08-21）批内族口径统一 E[max|X|]：批成员的 train IC 序列
    # 尾对齐构建 R（与 trail 门控同一数学、同一采样器）——替代
    # N_eff=1+(M-1)(1-ρ̄) 幂校正。同族变体（|ρ|≈0.9 的参数扫描）的
    # 族内选择运气由 max|X| 直接计价，不再经"有效个数"中转。
    sketches = {}
    for n in ok:
        s = factors[n].get("ic_series_train")
        if isinstance(s, (list, tuple)) and len(s) >= 5:
            sketches[n] = s
    sampler = _LuckSampler()
    sampler.sync(sketches)
    bar_sigma_b = sampler.bar_sigma()
    # 谱 M_eff 保留为遥测（v2 字段语义：批内有效假设数的量级感）
    if M >= 2 and sampler._R is not None:
        w = np.linalg.eigvalsh(sampler._R)
        w = np.clip(w, 0.0, None)
        m_eff = float(w.sum() ** 2 / max((w ** 2).sum(), 1e-18))
    else:
        m_eff = float(M)

    deflated = {}
    for n in ok:
        p = p_single[n]
        dp_stats = factors[n].get("deflated_train") or {}
        dp = None
        if dp_stats.get("sr_hat") is not None:
            dp = _dsr_p_from_stats(dp_stats.get("sr_hat"), dp_stats.get("skew"),
                                   dp_stats.get("kurt"), dp_stats.get("n_obs"),
                                   bar_sigma_b, pool_std)
        if dp is not None:
            factors[n]["deflated_train"] = {**dp_stats, "p": dp,
                                            "bar_sigma": bar_sigma_b,
                                            "pool_std": pool_std,
                                            "recomputed_at_batch_family": True}
        deflated[n] = {"p_single": float(p) if np.isfinite(p) else None,
                       "deflated_p": dp, "survives": bool(dp is not None and dp < 0.05)}

    best_name = None
    best_ic_ir = -np.inf
    for n in ok:
        ir = factors[n].get("ic_ir_train", np.nan)
        if np.isfinite(ir) and ir > best_ic_ir:
            best_ic_ir = ir
            best_name = n

    return {
        "factors": factors,
        "batch": {
            "M": M,
            "M_requested": len(names),
            "rho_bar": rho_bar,
            "N_eff": m_eff,
            "bar_sigma": bar_sigma_b,
            "deflated": deflated,
            "best_name": best_name,
            "best_ic_ir": float(best_ic_ir) if np.isfinite(best_ic_ir) else None,
            "best_deflated_p": deflated[best_name]["deflated_p"] if best_name else None,
        }
    }


def passes_acceptance(result, z_threshold=3.0, beta_threshold=0.3, min_n=20, alpha=0.05):
    cp = result.get("column_perm_train") or {}
    z = cp.get("z", np.nan)
    beta = result.get("beta_exposure", np.nan)
    ic_n = result.get("ic_n_train", 0)

    if not np.isfinite(z):
        return False, "column-perm z 非有限"
    if abs(z) < z_threshold:
        return False, f"column-perm |z|={abs(z):.2f} < {z_threshold}（截面结构不显著）"
    if np.isfinite(beta) and abs(beta) >= beta_threshold:
        return False, f"beta 暴露 |{beta:.2f}| >= {beta_threshold}（beta 伪装嫌疑）"
    if ic_n < min_n:
        return False, f"样本不足 n={ic_n} < {min_n}"
    # 多重检验门控（2026-08-18 复核补全）：deflated p 是入册硬门——
    # 不显著 = 选择运气不可排除。此前 z≥3 的不显著因子照样 accepted=True。
    # v3（2026-08-21）：bar_sigma>0（含 M=1 的 E|Z| 底价）都需 pool_std——
    # p=None 一律拒绝，N=1 豁免取消（符号选择也是选择，trail 实证）。
    dp = result.get("deflated_train") or {}
    p, n_trials = dp.get("p"), dp.get("n_trials") or 1
    if p is None:
        return False, ("deflated p 不可算（缺池分布基线或样本不足）——"
                       "先跑 null-calibration 建 pool_std 基线")
    if isinstance(p, (int, float)) and p > alpha:
        return False, (f"deflated p={p:.4f} > {alpha}（{n_trials:.0f} 次已试假设的"
                       "多重检验校正下不显著——选择运气不可排除）")
    return True, "pass"


def finalize_mining(candidate_F, env, verbose=True, state_root=None):
    if not candidate_F:
        return {"error": "候选池为空，无可验证因子"}

    batch = evaluate_batch(candidate_F, env, train_end=env.calibration.dev_end)
    best_name = batch["batch"]["best_name"]
    best_deflated_p = batch["batch"]["best_deflated_p"]

    selection = {}
    for n, F in candidate_F.items():
        r = evaluate_selection(F, env, verbose=False)
        selection[n] = r.get("ic_ir", np.nan)

    try:
        test_res = evaluate_test(candidate_F[best_name], env, verbose=False, state_root=state_root)
    except RuntimeError as e:
        test_res = {"error": str(e)}

    report = {
        "finalized_at_round": None,
        "best_candidate": best_name,
        "best_ic_ir_dev": batch["batch"]["best_ic_ir"],
        "best_deflated_p": best_deflated_p,
        "selection_ic_ir": selection,
        "batch": batch["batch"],
        "test": test_res,
    }
    if verbose:
        print("=" * 60)
        print(f"最终验证：最优候选 = {best_name}")
        print(f"  dev IC_IR = {report['best_ic_ir_dev']:+.3f}  deflated_p = {best_deflated_p}")
        print(f"  selection 区 IC_IR: " + " ".join(f"{n}:{v:+.3f}" for n, v in selection.items()))
        if isinstance(test_res, dict) and "error" not in test_res:
            print(f"  test IC_IR = {test_res.get('ic_ir', np.nan):+.3f}  衰减={test_res.get('decay', {}).get('decay_direction', '?')}")
            print(f"  test column-perm z = {test_res.get('column_perm', {}).get('z', np.nan):+.2f}")
        else:
            print(f"  test: {test_res}")
        print("=" * 60)
    return report


def evaluate_walk_forward(F, env, n_folds=5, t0_date=None, t1_date=None, verbose=False):
    F = np.asarray(F, dtype=np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 输出形状 {F.shape} != (T,N) {(env.T, env.N)}")
    t0_date = env.calibration.dev_end if t0_date is None else t0_date
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic_sig = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    if t0_date is not None:
        ic_sig = ic_sig[ic_sig.index >= pd.Timestamp(t0_date)]
    if t1_date is not None:
        ic_sig = ic_sig[ic_sig.index < pd.Timestamp(t1_date)]
    ic_sig = ic_sig.dropna()
    n = len(ic_sig)
    if n < n_folds * 2:
        return dict(error=f"区间样本不足（n={n} < {n_folds * 2}）")

    edges = np.linspace(0, n, n_folds + 1).astype(int)
    overall_mean = float(ic_sig.mean())
    sign = int(np.sign(overall_mean))
    per_fold = []
    same = 0
    for i in range(n_folds):
        seg = ic_sig.iloc[edges[i]:edges[i + 1]]
        st = _ic_stats(seg)
        per_fold.append({
            "t0": str(seg.index[0].date()), "t1": str(seg.index[-1].date()),
            "ic_mean": st["mean"], "ic_ir": st["ir"], "n": st["n"],
        })
        if np.sign(st["mean"]) == sign:
            same += 1

    result = {
        "n_folds": n_folds,
        "per_fold": per_fold,
        "per_fold_ic_ir": [f["ic_ir"] for f in per_fold],
        "per_fold_ic_mean": [f["ic_mean"] for f in per_fold],
        "fold_consistency": same / n_folds,
        "sign": sign,
        "overall_ic_mean": overall_mean,
    }
    if verbose:
        print("=" * 60)
        print(f"walk-forward 分块稳健性: {result['n_folds']} 块  跨块同号 {result['fold_consistency']*100:.0f}%  全样本 IC {result['overall_ic_mean']:+.4f}")
        for f in result['per_fold']:
            print(f"  {f['t0']}~{f['t1']}:  IC {f['ic_mean']:+.4f}  IR {f['ic_ir']:+.3f}  n={f['n']}")
        print("=" * 60)
    return result


def _print_result(r):
    print("=" * 60)
    print(f"IC: train {r['ic_mean_train']:+.4f} (IR {r['ic_ir_train']:+.3f})")
    yc = r['yearly_consistency_train']
    print(f"逐年符号一致性(train): {yc['consistent']*100:.0f}%")
    rl = r['rolling_ic_stability_train']
    if rl and np.isfinite(rl['stable']):
        print(f"滚动IC稳定性(train): {rl['stable']*100:.0f}% ({rl['n_windows']} 窗口)")
    dc = r.get('decay_train')
    if dc:
        intervals = ' '.join(f"{v:+.3f}" for v in dc.get('per_interval_ic', []))
        slope_str = f"{dc['yearly_slope']:+.4f}/yr" if np.isfinite(dc.get('yearly_slope')) else "n/a"
        print(f"衰减(train): {dc['decay_direction']}  power={dc['power']}  n={dc['n_total']}({dc['K']}区间×{dc['n_per_interval']})  Δ首末={dc['first_last_delta']:+.4f}  z={dc['delta_z']:+.2f}")
        if intervals:
            print(f"  区间IC: [{intervals}]  yearly_slope={slope_str}")
    cp = r['column_perm_train']
    print(f"column-perm(train): z={cp['z']:+.2f} p={cp['p']:.3f}")
    print(f"beta 暴露: {r['beta_exposure']:+.3f}  (|.|>0.3 警惕 beta 伪装)")
    if r['topn']:
        t = r['topn']
        print(f"top-N 超额(train): 净 {t['net_annual']:+.1f}%/yr  毛 {t['gross_annual']:+.1f}%/yr  换手 {t['turn_avg']:.2f}")
        yrs = ' '.join(f"{y}:{v:+.1f}" for y, v in t['yearly_net'].items())
        print(f"逐年净超额(%/yr): {yrs}")
        bb = t['block_bootstrap']
        print(f"block-bootstrap: z={bb['z']:+.2f} p={bb['p']:.3f}")
    print("=" * 60)
