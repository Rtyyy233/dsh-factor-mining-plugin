# coding=utf-8
"""regime 分解（调度器票 2b，PLAN-scheduler-task-brief-2026-09-29 §五）。

U_regime 的计算层：按实现波动三分位划分日期 regime，分 regime 秩 IC，
未解决度 U = (max IC_r − min IC_r)/SE_pooled——「当前分数看不出、但
暗示存在具体下一步假设」的势能量化（IC 在 regime1 远大于 0、regime2
约 0 → U 大 Q 低：叶子价值低状态价值高）。

诚实性控制（任务书 §四.3 F5——必带）：U_regime 对 R 个 regime 扫一遍
再取极值，本身是 max-over-noise 统计量，不做控制调度器会专门追不稳定
因子（Goodhart 通道）。控制 = 分裂样本复验门：regime 分解在不相交
两半上方向复制才保留 U_regime，否则归零。

口径说明（内部一致性优先）：逐日秩 IC 为本模块自实现（rank pct +
Pearson，与 evaluate._cross_sectional_ic 同式），forward return 口径
t0 = close[t+H]/close[t] − 1（t0 基，与 evaluate._forward_returns 的
一致分支）；前视收益对齐与 PIT 掩码做最简防御（NaN 剔除 + 一字板
h==l 剔除），完整 PIT 掩码复用主评估器——本度量只进打分不进门。
"""
from __future__ import annotations

import numpy as np


MIN_POOL = 30          # 单日截面最小有效样本（与 evaluate.MIN_POOL 对齐）
N_REGIMES = 3          # 三分位起步（任务书 2b）


# ---------------------------------------------------------------- regime 划分

def realized_vol_regimes(env, window: int = 20, n_regimes: int = N_REGIMES
                         ) -> dict[str, np.ndarray]:
    """实现波动 regime：日度横截面 |log ret| 均值 → window 平滑 →
    时序等分位分组。返回 {regime_name: bool[T] 掩码}（低/中/高）。"""
    c = np.asarray(env.c, dtype=np.float64)
    T = c.shape[0]
    with np.errstate(divide="ignore", invalid="ignore"):
        ret = np.diff(np.log(np.where(c > 0, c, np.nan)), axis=0)
    daily = np.full(T, np.nan)
    daily[1:] = np.nanmean(np.abs(ret), axis=1)
    if window > 1:  # 居中滚动均值（NaN 感知：卷积会传播 NaN，先拆值/权两路）
        v = np.nan_to_num(daily, nan=0.0)
        w = (~np.isnan(daily)).astype(np.float64)
        sv = np.convolve(v, np.ones(window), mode="same")
        sw = np.convolve(w, np.ones(window), mode="same")
        daily = np.where(sw > 0, sv / np.maximum(sw, 1e-9), np.nan)
    valid = ~np.isnan(daily)
    out: dict[str, np.ndarray] = {}
    if valid.sum() < n_regimes * 10:
        return {f"r{i}": np.zeros(T, dtype=bool) for i in range(n_regimes)}
    qs = np.quantile(daily[valid], np.linspace(0, 1, n_regimes + 1)[1:-1])
    labels = np.digitize(daily, qs)  # 0..n_regimes-1
    names = ["low", "mid", "high"] if n_regimes == 3 else \
        [f"r{i}" for i in range(n_regimes)]
    for i, name in enumerate(names):
        out[name] = labels == i
    return out


# ---------------------------------------------------------------- 分 regime IC

def _forward_returns_t0(env) -> np.ndarray:
    """t0 基前向收益 F[t] = close[t+H]/close[t] − 1 的 horizon 无关部分
    由调用方切片：本函数返回单步对数收益，调用方按 horizon 累乘。"""
    c = np.asarray(env.c, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        lr = np.diff(np.log(np.where(c > 0, c, np.nan)), axis=0)
    return np.vstack([np.full((1, c.shape[1]), np.nan), lr])


def forward_returns(env, horizon: int) -> np.ndarray:
    """(T,N)：F[t] = close[t+H]/close[t] − 1（t0 基；尾 H 行 NaN）。"""
    lr = _forward_returns_t0(env)
    T = lr.shape[0]
    out = np.full_like(lr, np.nan)
    h = max(int(horizon), 1)
    for t in range(T - h):
        with np.errstate(invalid="ignore"):
            out[t] = np.nansum(lr[t + 1:t + h + 1], axis=0)
    return np.exp(np.where(np.isnan(out), np.nan, out)) - 1.0


def _pit_mask(env) -> np.ndarray:
    """最简 PIT 防御：一字板（h==l）与 NaN 收益日剔除。"""
    h = np.asarray(env.h, dtype=np.float64)
    l = np.asarray(env.l, dtype=np.float64)
    return ~((h == l) | np.isnan(h) | np.isnan(l))


def regime_rank_ic(F: np.ndarray, env, horizon: int,
                   mask: np.ndarray) -> tuple[float | None, int]:
    """mask 内逐日截面秩 IC 均值与有效日数。

    返回 (mean_ic, n_days)；有效日 < 10 → (None, n)——诚实不可算。"""
    F = np.asarray(F, dtype=np.float64)
    fwd = forward_returns(env, horizon)
    pit = _pit_mask(env)
    ics = []
    for t in np.nonzero(mask)[0]:
        if t >= fwd.shape[0]:
            continue
        x, y = F[t], fwd[t]
        ok = pit[t] & ~np.isnan(x) & ~np.isnan(y)
        if ok.sum() < MIN_POOL:
            continue
        xr = _pct_rank(x[ok])
        yr = _pct_rank(y[ok])
        if xr.std() < 1e-12 or yr.std() < 1e-12:
            continue
        ics.append(float(np.corrcoef(xr, yr)[0, 1]))
    if len(ics) < 10:
        return None, len(ics)
    return float(np.mean(ics)), len(ics)


def _pct_rank(v: np.ndarray) -> np.ndarray:
    """截面百分位秩（与 evaluate 口径同式：pct rank + Pearson）。"""
    order = np.argsort(v, kind="stable")
    ranks = np.empty(len(v), dtype=np.float64)
    ranks[order] = np.arange(1, len(v) + 1)
    return ranks / (len(v) + 1)


def u_regime(F: np.ndarray, env, horizon: int,
             regimes: dict[str, np.ndarray] | None = None
             ) -> dict | None:
    """U_regime 主量：(max_r IC_r − min_r IC_r)/SE_pooled。

    SE_pooled = sqrt(1/Σ n_r)（各 regime 日 IC 独立、单日 IC se≈1/√n 的
    合并）。任一 regime 不可算 → 整项 None（不打 0 分——诚实缺失）。"""
    regimes = regimes if regimes is not None else realized_vol_regimes(env)
    ics: dict[str, float] = {}
    ns: dict[str, int] = {}
    for name, mask in regimes.items():
        if not isinstance(mask, np.ndarray) or mask.sum() < 10:
            continue
        ic, n = regime_rank_ic(F, env, horizon, mask)
        if ic is not None:
            ics[name] = ic
            ns[name] = n
    if len(ics) < 2:
        return None
    vals = list(ics.values())
    se = (1.0 / max(sum(ns.values()), 1)) ** 0.5
    if se <= 0:
        return None
    return {
        "u": (max(vals) - min(vals)) / se,
        "regime_ics": ics,
        "n_days": ns,
        "se_pooled": se,
    }


# ---------------------------------------------------------------- 分裂复验门

def split_replication_gate(F: np.ndarray, env, horizon: int,
                           regimes: dict[str, np.ndarray] | None = None,
                           min_cos: float = 0.0,
                           min_z: float = 2.0) -> dict:
    """F5 诚实性控制：regime 分解在不相交两半上方向复制才保留。

    复制 = 方向（前半/后半时序中位切分，regime 掩码内再切；各自的
    regime IC 向量中心化后余弦 > min_cos）**且量级**（两半各自的
    regime 极差 z=(max−min)/SE ≥ min_z——纯噪声的中心化向量也能偶然
    同向，量级条件挡掉 Goodhart 通道的浅层形态；实测 6 噪声 5 个仅靠
    方向门漏过）。不复制 → keep=False，调用方将 U_regime 归零。"""
    regimes = regimes if regimes is not None else realized_vol_regimes(env)
    T = np.asarray(env.c).shape[0]
    half = T // 2
    date_idx = np.arange(T)
    a_ics, b_ics, a_ns, b_ns = [], [], [], []
    names = sorted(regimes.keys())
    for name in names:
        mask = regimes[name]
        # 全局日期掩码（不得切片 mask——切片后索引重置而 F/env 未切，
        # 半样本会读到对方半的数据）
        ia, na = regime_rank_ic(F, env, horizon, mask & (date_idx < half))
        ib, nb = regime_rank_ic(F, env, horizon, mask & (date_idx >= half))
        a_ics.append(np.nan if ia is None else ia)
        b_ics.append(np.nan if ib is None else ib)
        a_ns.append(max(na, 0))
        b_ns.append(max(nb, 0))
    a = np.asarray(a_ics, dtype=np.float64)
    b = np.asarray(b_ics, dtype=np.float64)
    if np.isnan(a).any() or np.isnan(b).any() or len(a) < 2:
        return {"keep": False, "reason": "分裂半样本不足", "cos": None}
    se_a = (1.0 / max(sum(a_ns), 1)) ** 0.5
    se_b = (1.0 / max(sum(b_ns), 1)) ** 0.5
    z_a = (a.max() - a.min()) / se_a if se_a > 0 else 0.0
    z_b = (b.max() - b.min()) / se_b if se_b > 0 else 0.0
    ac = a - a.mean()
    bc = b - b.mean()
    na_, nb_ = np.linalg.norm(ac), np.linalg.norm(bc)
    if na_ < 1e-12 or nb_ < 1e-12:
        return {"keep": False, "reason": "某半 regime IC 无差异", "cos": None}
    cos = float((ac @ bc) / (na_ * nb_))
    if cos <= min_cos:
        return {"keep": False, "reason": "方向不复制", "cos": cos,
                "z_a": round(z_a, 2), "z_b": round(z_b, 2)}
    if z_a < min_z or z_b < min_z:
        return {"keep": False, "reason": "量级不复制（极差 z 未过线）",
                "cos": cos, "z_a": round(z_a, 2), "z_b": round(z_b, 2)}
    return {"keep": True, "cos": cos, "z_a": round(z_a, 2),
            "z_b": round(z_b, 2)}


def u_regime_gated(F: np.ndarray, env, horizon: int,
                   regimes: dict[str, np.ndarray] | None = None) -> dict:
    """带复验门的 U_regime（调度器消费口径）：复验不复制 → u=0 且标注。"""
    base = u_regime(F, env, horizon, regimes)
    if base is None:
        return {"u": None, "gate": "不可算"}
    gate = split_replication_gate(F, env, horizon, regimes)
    if not gate.get("keep"):
        return {"u": 0.0, "gate": "split_replication_failed",
                "cos": gate.get("cos"), **{k: v for k, v in base.items()
                                           if k != "u"}}
    return {"u": base["u"], "gate": "passed", "cos": gate.get("cos"),
            "regime_ics": base.get("regime_ics"),
            "n_days": base.get("n_days"), "se_pooled": base.get("se_pooled")}
