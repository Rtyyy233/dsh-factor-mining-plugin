# coding=utf-8
"""Registered-book rank cache + submission-time orthogonality check.

2026-09-06 正交门:提交期对候选与在册账本做值相关检查。动机与实测:
2026-09-05 全量正交审计发现量价相关域与历史 IDO 系同源(|ρ|≈0.70,符号
构造性相反)、tail 账本 vol_of_vol 八连注册两两至 1.000——显著性门只回答
"它自己是否真实",本门回答"账本是否已知"。deflation 账本对试验次数定价,
本门对账本有效维度定价,两者互补。

成本设计:在册因子秩矩阵在入册/回填时一次性缓存(dev 区 uint16,0=NaN),
提交期检查 = 候选秩(评估时本已计算)对缓存的逐日矩阵乘,T 次循环 <2s。
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

MIN_COMMON_PER_DATE = 300


def cache_paths(state_root):
    root = Path(state_root)
    return (root / "book_ranks.npz", root / "book_ranks.meta.json")


def load_cache(state_root):
    """→ {"names", "dates", "ranks"(uint16 D×N, 0=NaN), "fingerprint"} | None"""
    npz_p, meta_p = cache_paths(state_root)
    if not (npz_p.exists() and meta_p.exists()):
        return None
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        z = np.load(npz_p)
        ranks = z["ranks"]
        if ranks.ndim != 3 or len(meta.get("names", [])) != ranks.shape[0]:
            return None
        return {"names": list(meta["names"]), "dates": list(meta["dates"]),
                "ranks": ranks, "fingerprint": meta.get("fingerprint")}
    except Exception:
        return None


def load_book_cache(state_root):
    """bridge 正交门入口（bridge._registry_submit）：缺失/损坏 → None，
    调用方跳过并提示 book.backfill。"""
    return load_cache(state_root)


def save_cache(state_root, names, dates, ranks_u16, fingerprint):
    npz_p, meta_p = cache_paths(state_root)
    npz_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(npz_p) + ".tmp.npz"   # np.savez_compressed 对非 .npz 后缀会追加 .npz
    np.savez_compressed(tmp, ranks=ranks_u16.astype(np.uint16))
    os.replace(tmp, str(npz_p))
    meta_p.write_text(json.dumps(
        {"names": list(names), "dates": list(dates), "fingerprint": fingerprint},
        ensure_ascii=False), encoding="utf-8")


def append_to_cache(state_root, name, dates, ranks_u16, fingerprint):
    """入册成功后追加该因子的秩。缓存缺失/日期错位/指纹不符 → False(调用方
    提示运行 book.backfill)。"""
    cache = load_cache(state_root)
    if cache is None or name in cache["names"]:
        return False
    if cache["dates"] != list(dates) or cache.get("fingerprint") != fingerprint:
        return False
    names = cache["names"] + [name]
    ranks = np.concatenate([cache["ranks"], ranks_u16[None, :, :]], axis=0)
    save_cache(state_root, names, dates, ranks, fingerprint)
    return True


def dev_rank_u16(env, F, dev_end):
    """因子值 (T,N) → dev 区秩 (D,N) uint16(0=NaN)与 dev 日期列表。"""
    df = pd.DataFrame(np.asarray(F, dtype=np.float64),
                      index=pd.DatetimeIndex(env.dates))
    dev = df.loc[df.index <= pd.Timestamp(dev_end)]
    r = dev.rank(axis=1).values.astype(np.float32)
    u16 = np.where(np.isfinite(r), r, 0).astype(np.uint16)
    dates = [str(d.date()) for d in dev.index]
    return u16, dates


def _masked_center(rows_u16):
    """(D,N) uint16(0=NaN) → 逐行去均值、NaN 置 0 的 float32。"""
    X = rows_u16.astype(np.float32)
    X[X == 0] = np.nan
    with np.errstate(all="ignore"):
        mu = np.nanmean(X, axis=1, keepdims=True)
    X = X - mu
    X[~np.isfinite(X)] = 0.0
    return X


def rebuild_book_cache(state_root, env, entries, fingerprint=None):
    """全量重建：对 registry 的 accepted 条目逐只重算 dev 秩并落缓存。
    → {"status", "n_factors", "n_dates", "failed": [(name, why)...]}"""
    names, blocks = [], []
    dates = None
    failed = []
    for e in entries:
        nm = e.get("name")
        src = e.get("source") or ""
        if not src:
            failed.append((nm, "no source"))
            continue
        try:
            ns = {"env": env}
            exec(src, ns)
            F = np.asarray(ns["factor"](env), dtype=np.float64)
            u16, dts = dev_rank_u16(env, F, env.calibration.dev_end)
            if dates is None:
                dates = dts
            elif dts != dates:
                failed.append((nm, "date mismatch"))
                continue
            names.append(nm)
            blocks.append(u16)
        except Exception as ex:
            failed.append((nm, repr(ex)[:100]))
    if not names:
        return {"status": "failed", "why": "no factors computed",
                "failed": failed}
    ranks = np.stack(blocks).astype(np.uint16)
    save_cache(state_root, names, dates, ranks, fingerprint)
    return {"status": "ok", "n_factors": len(names), "n_dates": len(dates),
            "failed": failed}


def check_candidate(cache, cand_u16, exclude_name=None,
                    reject_th=0.80, annotate_th=0.60):
    """候选秩 vs 在册账本逐成员的平均秩相关(逐日 Pearson,公共有效
    ≥MIN_COMMON_PER_DATE 的日期)。→ report dict:
    {status, max_corr, worst_name, action(pass/annotate/reject), top, ...}
    纯函数:不写盘、不烧名——拒绝/标注动作由调用方执行。"""
    names = list(cache["names"])
    ranks = cache["ranks"]
    D = min(ranks.shape[1], cand_u16.shape[0])
    if D == 0 or not names:
        return {"status": "skipped", "why": "empty cache"}
    C = _masked_center(cand_u16[:D])
    cand_valid = (cand_u16[:D] != 0).sum(axis=1)
    cnorm = np.linalg.norm(C, axis=1)
    per = []
    for k, nm in enumerate(names):
        if exclude_name and nm == exclude_name:
            continue
        Rk = _masked_center(ranks[k, :D])
        mem_valid = (ranks[k, :D] != 0).sum(axis=1)
        valid = (cand_valid >= MIN_COMMON_PER_DATE) & (mem_valid >= MIN_COMMON_PER_DATE)
        if valid.sum() == 0:
            per.append((nm, float("nan")))
            continue
        mn = np.linalg.norm(Rk, axis=1)
        corr = (C * Rk).sum(axis=1) / (cnorm * mn + 1e-12)
        rho = float(np.mean(corr[valid]))
        per.append((nm, rho))
    finite = [(nm, r) for nm, r in per if np.isfinite(r)]
    if not finite:
        return {"status": "skipped", "why": "no comparable dates"}
    worst_nm, worst_r = max(finite, key=lambda x: abs(x[1]))
    a = abs(worst_r)
    action = ("reject" if a >= reject_th
              else "annotate" if a >= annotate_th else "pass")
    return {"status": "ok", "max_corr": round(a, 3), "worst_name": worst_nm,
            "worst_corr": round(worst_r, 3), "action": action,
            "thresholds": {"reject": reject_th, "annotate": annotate_th},
            "n_compared": len(finite),
            "top": sorted([(nm, round(r, 3)) for nm, r in finite],
                          key=lambda x: -abs(x[1]))[:5]}


# ---------------------------------------------------------------------------
# ts 线（factor-mining-futures，2026-11-07 任务书 §五）：在册信号序列正交账本
#
# 面板账本按「逐日截面秩 (D,N) uint16」缓存；单品种线无截面——缓存对象
# 换成 dev 窗信号序列 (names, D) float32，检查=候选与在册逐成员的
# dev 窗 Spearman（秩相关；0.80 软拒/0.60 标注，阈值随 MINING_CONFIG
# 注入）。与面板缓存分文件（book_ranks_ts.npz），互不误用。
# ---------------------------------------------------------------------------

TS_MIN_OVERLAP = 60   # Spearman 重叠下限（<60 点的 |ρ| 在零假设下期望≈0.13，全是噪声）


def ts_cache_paths(state_root):
    root = Path(state_root)
    return (root / "book_ranks_ts.npz", root / "book_ranks_ts.meta.json")


def load_ts_cache(state_root):
    """→ {"names", "dates", "values"(float32 M×D), "fingerprint"} | None"""
    npz_p, meta_p = ts_cache_paths(state_root)
    if not (npz_p.exists() and meta_p.exists()):
        return None
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        z = np.load(npz_p)
        values = z["values"]
        if values.ndim != 2 or len(meta.get("names", [])) != values.shape[0]:
            return None
        return {"names": list(meta["names"]), "dates": list(meta["dates"]),
                "values": values, "fingerprint": meta.get("fingerprint")}
    except Exception:
        return None


def save_ts_cache(state_root, names, dates, values, fingerprint):
    npz_p, meta_p = ts_cache_paths(state_root)
    npz_p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(npz_p) + ".tmp.npz"
    np.savez_compressed(tmp, values=np.asarray(values, dtype=np.float32))
    os.replace(tmp, str(npz_p))
    meta_p.write_text(json.dumps(
        {"names": list(names), "dates": list(dates),
         "fingerprint": fingerprint}, ensure_ascii=False), encoding="utf-8")


def append_ts_cache(state_root, name, dates, values, fingerprint) -> bool:
    """入册成功后追加该因子的 dev 窗信号序列（缺失/日期错位/指纹不符→False）。"""
    cache = load_ts_cache(state_root)
    if cache is None or name in cache["names"]:
        return False
    if cache["dates"] != list(dates) or cache.get("fingerprint") != fingerprint:
        return False
    names = cache["names"] + [name]
    values = np.concatenate(
        [cache["values"], np.asarray(values, dtype=np.float32)[None, :]], axis=0)
    save_ts_cache(state_root, names, dates, values, fingerprint)
    return True


def dev_series_block(env, F, dev_end):
    """因子值 (T,) → dev 窗（date ≤ dev_end）有限值 float32 与 dev 日期列表。"""
    from .factor.env import TsEnv as _TsEnv
    if not isinstance(env, _TsEnv):
        raise ValueError("dev_series_block 只支持 TsEnv（ts 线正交账本）")
    F = np.asarray(F, dtype=np.float64)
    dates = pd.DatetimeIndex(env.dates)
    dev = dates[dates <= pd.Timestamp(dev_end)]
    mask = np.isfinite(F[:len(dev)])
    vals = F[:len(dev)][mask].astype(np.float32)
    dts = [str(d.date()) for d in dev[mask]]
    return vals, dts


def _ts_spearman(a: np.ndarray, b: np.ndarray,
                 min_overlap: int = TS_MIN_OVERLAP) -> float | None:
    """两序列公共有限位的 Spearman 秩相关；重叠不足 → None。"""
    n = min(len(a), len(b))
    if n < min_overlap:
        return None
    x = a[-n:].astype(np.float64)
    y = b[-n:].astype(np.float64)
    m = np.isfinite(x) & np.isfinite(y)
    if int(m.sum()) < min_overlap:
        return None
    rx = pd.Series(x[m]).rank().values
    ry = pd.Series(y[m]).rank().values
    sx, sy = rx.std(), ry.std()
    if not (sx > 0 and sy > 0):
        return None
    return float(np.mean((rx - rx.mean()) * (ry - ry.mean())) / (sx * sy))


def check_candidate_ts(cache, cand_values, exclude_name=None,
                       reject_th=0.80, annotate_th=0.60) -> dict:
    """候选 vs 在册账本逐成员 dev 窗 Spearman（schema 与面板版同形）。

    尾部对齐（与面板逐日口径不同——ts 序列是时间轴整体，取公共尾部
    对齐后整段相关）。纯函数：动作由调用方执行。"""
    names = list(cache["names"])
    values = cache["values"]
    if not names or values.ndim != 2:
        return {"status": "skipped", "why": "empty cache"}
    per = []
    for k, nm in enumerate(names):
        if exclude_name and nm == exclude_name:
            continue
        rho = _ts_spearman(np.asarray(cand_values, dtype=np.float64),
                           values[k])
        per.append((nm, rho))
    finite = [(nm, r) for nm, r in per if r is not None and np.isfinite(r)]
    if not finite:
        return {"status": "skipped", "why": "no comparable series "
                f"(overlap < {TS_MIN_OVERLAP} 或零离散)"}
    worst_nm, worst_r = max(finite, key=lambda x: abs(x[1]))
    a = abs(worst_r)
    action = ("reject" if a >= reject_th
              else "annotate" if a >= annotate_th else "pass")
    return {"status": "ok", "max_corr": round(a, 3), "worst_name": worst_nm,
            "worst_corr": round(worst_r, 3), "action": action,
            "thresholds": {"reject": reject_th, "annotate": annotate_th,
                           "method": "spearman_dev_window"},
            "n_compared": len(finite),
            "top": sorted([(nm, round(r, 3)) for nm, r in finite],
                          key=lambda x: -abs(x[1]))[:5]}


def rebuild_book_cache_ts(state_root, env, entries, fingerprint=None) -> dict:
    """ts 版全量重建：registry accepted 条目逐只重算 dev 窗信号序列并落缓存。"""
    names, blocks, dates, failed = [], [], None, []
    for e in entries:
        nm = e.get("name")
        src = e.get("source") or ""
        if not src:
            failed.append((nm, "no source"))
            continue
        try:
            ns = {"env": env}
            exec(src, ns)
            F = np.asarray(ns["factor"](env), dtype=np.float64)
            vals, dts = dev_series_block(env, F, env.calibration.dev_end)
            if len(vals) < TS_MIN_OVERLAP:
                failed.append((nm, f"dev 有效点 {len(vals)} < {TS_MIN_OVERLAP}"))
                continue
            if dates is None:
                dates = dts
            elif dts != dates:
                failed.append((nm, "date mismatch"))
                continue
            names.append(nm)
            blocks.append(vals)
        except Exception as ex:  # noqa: BLE001 — 坏条目隔离（面板版同款）
            failed.append((nm, repr(ex)[:100]))
    if not names:
        return {"status": "failed", "why": "no factors computed",
                "failed": failed}
    save_ts_cache(state_root, names, dates, np.stack(blocks), fingerprint)
    return {"status": "ok", "n_factors": len(names), "n_dates": len(dates),
            "failed": failed}
