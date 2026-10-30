# coding=utf-8
"""证据遥测共享数学（2026-10-06 假阴性救助线，任务 A）。

单一权威定义，三处消费必须同源 import（各自复制公式=口径漂移=校准失真）：
- bridge 评估落账尾部 → evidence_ledger.jsonl（原始值旁路账本，模型面零暴露）
- random_gen null 校准 → null_landscape.json 的 a4_dd / famsize 分位段
- rescue_ops 人审回扫 → 卡片证据列 + evidence_fit.json 拟合/残差

A4 = 累计 IC 回撤比（2026-10-06 证据性质对比研究定局的唯一一级证据性质）：
符号按总体 IC 均值对齐后 cumsum 的峰值回撤 / |期末累计|——度量"符号
对齐后的持续性与否"：真 alpha 的累计线单调性远好于幸运喷雾。暴露给
模型即激励适配（校准被侵蚀），故只落引擎侧账本，不进任何响应面。

族（family）= IC 序列草图 |ρ|≥0.6 的贪心聚类（或派生图连通分量）；
famsize/depth 是二级证据性质（真=小族早命中；匹配噪声=大家族最大值
成员——与出身纠缠，须分层解读）。
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import numpy as np

#: 族聚类阈值（与引擎链判定 |ρ|≥0.6、证据对比研究同口径）
FAMILY_RHO = 0.6
#: A4 期末累计退化阈（|c[-1]|≤此值 → 返回 None，不制造 1e9 级伪值）
A4_DEGENERATE = 1e-9
FIT_FILE = "evidence_fit.json"


# ---------------------------------------------------------------------------
# A4：累计 IC 回撤比
# ---------------------------------------------------------------------------
def a4_dd_ratio(series) -> float | None:
    """符号对齐后 cumsum 的峰值回撤 / |期末累计|。

    sg=sign(mean(series))，a=series·sg，c=cumsum(a)，峰值回撤 =
    max_i(peak_i − c_i)（peak=前缀最大值）。期末累计绝对值退化
    （≤1e-9，如常数序列）→ None（该样本不进任何拟合/分位）。
    序列长度 <5 或含非有限值 → None。"""
    a = np.asarray(series, dtype=np.float64)
    if a.size < 5 or not np.isfinite(a).all():
        return None
    sg = 1.0 if a.mean() >= 0 else -1.0
    a = a * sg
    c = np.cumsum(a)
    peak = np.maximum.accumulate(c)
    dd = float((peak - c).max())
    total = abs(float(c[-1]))
    if total <= A4_DEGENERATE:
        return None
    return dd / total


# ---------------------------------------------------------------------------
# 草图（sketch）几何：共同长度截断 / 标准化相关 / 贪心聚类
# ---------------------------------------------------------------------------
def truncate_common(sketches: list) -> np.ndarray:
    """草图矩阵：截断到共同长度（坑⑤：跨条目长度不一，直接堆叠会炸）。"""
    rows = [list(map(float, s)) for s in sketches]
    L = min(len(r) for r in rows)
    return np.asarray([r[:L] for r in rows], dtype=np.float64)


def _standardize(mat: np.ndarray) -> np.ndarray:
    out = mat - mat.mean(axis=1, keepdims=True)
    std = np.maximum(out.std(axis=1, keepdims=True), 1e-12)
    return out / std


def corr_matrix(sketches: list) -> np.ndarray:
    """标准化草图相关矩阵（对角=0，供族聚类与 max_rho 同源使用）。"""
    Xn = _standardize(truncate_common(sketches))
    gram = (Xn @ Xn.T) / (Xn.shape[1] - 1.0)
    gram = np.clip(gram, -1.0, 1.0)
    np.fill_diagonal(gram, 0.0)
    return gram


def corr_pair(a, b) -> float | None:
    """两条草图的标准化相关（截断共同长度；零方差 → None）。"""
    n = min(len(a), len(b))
    if n < 5:
        return None
    m = np.asarray([[float(x) for x in a[:n]], [float(x) for x in b[:n]]])
    try:
        g = corr_matrix([m[0], m[1]])
    except Exception:
        return None
    v = float(g[0, 1])
    return v if math.isfinite(v) else None


def cluster_families(skmat: np.ndarray, ts: list[str]):
    """贪心 |ρ|≥FAMILY_RHO 聚类（按时间序）。

    返回 (fam, depth, size)：fam[i]=族号；depth[i]=族内时间序位（1=首名）；
    size[f]=族规模。与 2026-10-06 证据对比研究同一算法（逐位一致），
    该研究的结论（A4 一级 / 族规模二级）以本口径为校准基线。"""
    n = skmat.shape[0]
    if n == 0:
        return np.zeros(0, int), np.zeros(0, int), {}
    norm = _standardize(skmat)
    gram = (norm @ norm.T) / (skmat.shape[1] - 1.0)
    gram = np.clip(gram, -1.0, 1.0)
    np.fill_diagonal(gram, 0.0)
    fam = np.full(n, -1)
    depth = np.ones(n, int)
    size: dict[int, int] = {}
    order = sorted(range(n), key=lambda i: ts[i] or "")
    nf = 0
    for i in order:
        if fam[i] >= 0:
            continue
        fam[i] = nf
        members = [i]
        for j in order:
            if fam[j] < 0 and abs(gram[i, j]) >= FAMILY_RHO:
                fam[j] = nf
                members.append(j)
        for d, m in enumerate(sorted(members, key=lambda x: ts[x] or ""), 1):
            depth[m] = d
        size[nf] = len(members)
        nf += 1
    return fam, depth, size


# ---------------------------------------------------------------------------
# 分位数段（null 地形 / 卡片列共用 schema：p10/p50/p90/p95）
# ---------------------------------------------------------------------------
def quantile_dict(vals, ps=(10, 50, 90, 95)) -> dict:
    arr = np.asarray([v for v in vals if v is not None], dtype=np.float64)
    out = {}
    for p in ps:
        out[f"p{p}"] = (float(np.percentile(arr, p)) if arr.size else None)
    return out


# ---------------------------------------------------------------------------
# 冻结拟合（evidence_fit.json）：a4 ~ 1 + sr_abs + sr_abs^2
# ---------------------------------------------------------------------------
def fit_path(state_root) -> Path:
    return Path(state_root) / FIT_FILE


def load_fit(state_root) -> dict:
    p = fit_path(state_root)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def latest_fit_version(fit: dict) -> str | None:
    """版本键 vN 的最大 N（无拟合 → None）。"""
    nums = [int(m.group(1)) for k in (fit or {})
            if (m := re.fullmatch(r"v(\d+)", str(k)))]
    return f"v{max(nums)}" if nums else None


def get_version(fit: dict, version: str | None) -> dict | None:
    if not version:
        return None
    v = fit.get(version)
    return v if isinstance(v, dict) else None


def ols_fit_a4(sr_abs_list, a4_list) -> list[float] | None:
    """OLS：a4 ~ 1 + sr_abs + sr_abs^2（拟合集=random 出身池）。样本 <10 拒拟合。"""
    x = np.asarray([float(v) for v in sr_abs_list], dtype=np.float64)
    y = np.asarray([float(v) for v in a4_list], dtype=np.float64)
    if x.size < 10 or x.size != y.size:
        return None
    X = np.stack([np.ones_like(x), x, x * x], axis=1)
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return [float(beta[0]), float(beta[1]), float(beta[2])]


def fit_predict(coef, sr_abs) -> float | None:
    if not coef or len(coef) != 3:
        return None
    x = float(sr_abs)
    return float(coef[0] + coef[1] * x + coef[2] * x * x)


def residual(coef, sr_abs, a4) -> float | None:
    """残差 = a4 − 拟合值（读时确定性导出——ledger 只存原始值）。"""
    pred = fit_predict(coef, sr_abs)
    if pred is None:
        return None
    return float(a4) - pred


def write_fit_version(state_root, version: str, payload: dict) -> dict:
    """追加一个拟合版本（旧档留存，红线 11：重拟合=版本递增）。"""
    p = fit_path(state_root)
    fit = load_fit(state_root)
    fit[version] = payload
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fit, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(p))
    return fit


def percentile_against(vals, x) -> float | None:
    """x 在经验分布 vals 中的百分位（0-100；空 → None）。"""
    arr = np.asarray([v for v in vals if v is not None], dtype=np.float64)
    if arr.size == 0 or x is None:
        return None
    return float((arr <= float(x)).mean() * 100.0)


# ---------------------------------------------------------------------------
# 机制层试验集（2026-10-06 特质奖励全自动 P1：层 M 防塌缩）
# ---------------------------------------------------------------------------
def mechanism_trials(state_root, trail_entries: list) -> list:
    """机制层试验集 = trail∩台账 ＋ 台账独有 hash 的无 sketch 合成单例。

    防的是 state.reset 塌缩洞：层 bar 现从 trail∩台账计，reset 删 trail 后
    注册试验消失 → 层 M 塌缩 → 墙突然变矮。合成单例走 _n_eff_from_entries
    的退化路径（无 sketch = 按独立计 = 最贵假设，保守）——reset 后被删的
    试验以独立身份回来，层 bar 只升不降（CRN 单调性定理延续）。
    bridge._trial_stats(stratum="mechanism") 与 rescue_ops 双消费，口径单一。
    """
    root = Path(state_root)
    reg_hashes: set = set()
    try:
        p = root / "registered_ledger.jsonl"
        if p.exists():
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                h = r.get("source_hash") if isinstance(r, dict) else None
                if isinstance(h, str) and h:
                    reg_hashes.add(h)
    except Exception:
        return list(trail_entries or [])
    kept = [e for e in (trail_entries or [])
            if isinstance(e, dict) and e.get("source_hash") in reg_hashes]
    seen = {e.get("source_hash") for e in kept}
    synth = [{"source_hash": h, "ic_series_sketch": None}
             for h in sorted(reg_hashes - seen)]
    return kept + synth
