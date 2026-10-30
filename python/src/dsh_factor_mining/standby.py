# -*- coding: utf-8 -*-
"""候补线(standby)视图 — PLAN-false-negative-remedy 批次 0(2026-09-13)。

deepdig 工作单的引擎化:纯派生只读视图(零新事实源,随时可重建),
数据 = trail_engine(near-miss 谓词)+ registry(排除已入册)
+ registered_ledger(层标签)。拒绝记录的是「此刻不够」不是「永远不行」
——复活走通道①③⑤,agent 无申诉权。

谓词(初值,分层翻转后按层内口径重校准):dev/batch 试验,
|dsr_stats.sr_hat| ≥ min_abs_sr,未入册,(hash,horizon) 去重。
族内去重:IC 序列尾对齐 |ρ|≥0.6 聚簇,每簇保留最优(逐只复活时
仍需过正交门——视图的去重只为展示不重复)。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np


def _read_json(path: Path, default):
    try:
        if path.exists():
            v = json.loads(path.read_text(encoding="utf-8"))
            return v if isinstance(v, type(default)) else default
    except Exception:
        pass
    return default


def _registered_hashes(root: Path) -> set:
    out = set()
    p = root / "registered_ledger.jsonl"
    if not p.exists():
        return out
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                if isinstance(r, dict) and r.get("source_hash"):
                    out.add(r["source_hash"])
            except Exception:
                continue
    except Exception:
        pass
    return out


def _corr(sa, sb) -> float | None:
    """IC 序列尾对齐相关(短/缺 → None;展示用,门不复用此值)。"""
    if not isinstance(sa, list) or not isinstance(sb, list):
        return None
    k = min(len(sa), len(sb))
    if k < 60:
        return None
    a = np.asarray(sa[-k:], dtype=np.float64)
    b = np.asarray(sb[-k:], dtype=np.float64)
    if not (np.isfinite(a).all() and np.isfinite(b).all()):
        return None
    ea, eb = a.std(), b.std()
    if ea <= 0 or eb <= 0:
        return None
    return float(((a - a.mean()) * (b - b.mean())).mean() / (ea * eb))


def standby_view(root: str | Path, min_abs_sr: float = 0.75,
                 top_n: int = 30, family_dedup: bool = True,
                 include_sketch: bool = False) -> dict[str, Any]:
    root = Path(root)
    trail = _read_json(root / "trail_engine.json", [])
    reg = _read_json(root / "registry.json", [])
    admitted = {e.get("source_hash") for e in reg
                if isinstance(e, dict) and e.get("accepted")
                and e.get("source_hash")}
    registered = _registered_hashes(root)

    seen: set = set()
    rows = []
    for e in trail:
        if not isinstance(e, dict) or e.get("stage") not in ("batch", "development"):
            continue
        sr = (e.get("dsr_stats") or {}).get("sr_hat")
        h = e.get("source_hash")
        if not isinstance(sr, (int, float)) or abs(sr) < min_abs_sr or not h:
            continue
        if h in admitted:
            continue
        k = (h, e.get("horizon"))
        if k in seen:
            continue
        seen.add(k)
        rows.append({
            "hash": h,
            "abs_sr": round(abs(float(sr)), 3),
            "sr": round(float(sr), 3),
            "horizon": e.get("horizon"),
            "origin": e.get("origin"),
            "stratum": "mechanism" if h in registered else "mechanical",
            "ts": e.get("ts"),
            "sketch": e.get("ic_series_sketch"),
        })
    rows.sort(key=lambda r: -r["abs_sr"])

    deduped = []
    if family_dedup:
        kept_sigs: list = []
        for r in rows:
            s = r.get("sketch")
            dup = False
            if isinstance(s, list) and len(s) >= 60:
                for u in kept_sigs:
                    c = _corr(s, u)
                    if c is not None and abs(c) >= 0.6:
                        dup = True
                        break
                if not dup:
                    kept_sigs.append(s)
            if not dup:
                deduped.append(r)
    else:
        deduped = rows

    n_mech = sum(1 for r in deduped if r["stratum"] == "mechanism")
    out_rows = [{k: v for k, v in r.items()
                 if include_sketch or k != "sketch"}
                for r in deduped[:top_n]]
    return {
        "exists": bool(rows),
        "count_raw": len(rows),
        "count_family_dedup": len(deduped),
        "count_mechanism_stratum": n_mech,
        "rows": out_rows,
        "meta": {
            "min_abs_sr": min_abs_sr,
            "top_n": top_n,
            "family_dedup": family_dedup,
            "note": ("候补线=派生只读视图(dev优异未过线的存量);复活通道:"
                     "①分层重定价(影子期后)/③规则化组合/⑤前向孵化——"
                     "机械触发,agent 无申诉权;每只复活仍过正交门"),
            "walls_note": "当前墙高见 evaluate 响应 deflated_train(bar×pool_std)",
        },
    }


# ---------------------------------------------------------------------------
# 通道③:规则化组合(PLAN-false-negative-remedy 批次3,2026-09-13)
# ---------------------------------------------------------------------------

def _assemble_composite(members: list[dict], sources: dict[str, str]) -> str:
    """符号对齐等权组合源码:z(F_i) 按各自 dev 符号加权求平均。

    成员源码(def factor(env): ...)的函数体缩进嵌入为 _mK(env)。
    机械文本手术,不执行成员源码本身。
    """
    import textwrap

    NL = chr(10)
    parts_code = []
    call_terms = []
    for i, m in enumerate(members, 1):
        src = sources[m["hash"]]
        body = src.split(NL, 1)[1] if NL in src else ""
        # 首行是 def factor(env):,其余为体;体统一缩进嵌入 _mK
        inner = textwrap.indent(textwrap.dedent(body).rstrip("\n"), "        ")
        parts_code.append(f"    def _m{i}(env):{NL}{inner}")
        sign = "-" if m["sr"] < 0 else ""
        call_terms.append(f"{sign}_z(_m{i}(env))")
    k = len(members)
    head = [
        "def factor(env):",
        "    import numpy as np",
        "    def _z(F):",
        "        F = np.nan_to_num(np.asarray(F, dtype=np.float64), nan=0.0)",
        "        mu = np.nanmean(F, axis=1, keepdims=True)",
        "        sd = np.nanstd(F, axis=1, keepdims=True)",
        "        sd[sd == 0] = 1.0",
        "        return (F - mu) / sd",
    ]
    tail = [f"    return ({' + '.join(call_terms)}) / {k}.0"]
    return NL.join(head + parts_code + tail) + NL


def combine(root: str | Path, env=None, k: int = 4, ortho_th: float = 0.3,
            min_abs_sr: float = 0.75) -> dict[str, Any]:
    """规则执行器:候补池内两两|ρ|<ortho_th、按|sr|取前 k、等权符号对齐组合。

    返回 {members, composite_source, composite_hash, rule}。评估/注册由
    调用方(bridge)接线:组合作为 dof=1 的注册试验进机制层(journal 登记
    +台账落盘),走标准 evaluate_composite 门链。
    """
    from .discipline import source_fingerprint
    root = Path(root)
    view = standby_view(root, min_abs_sr=min_abs_sr, top_n=200,
                        family_dedup=True, include_sketch=True)
    rows = [r for r in view["rows"] if isinstance(r.get("sketch"), list)
            and len(r["sketch"]) >= 60]
    # 正交贪心:按 |sr| 降序,互 |ρ|<ortho_th
    picked, sigs = [], []
    for r in rows:
        if len(picked) >= k:
            break
        ok = all((c := _corr(r["sketch"], s)) is None or abs(c) < ortho_th
                 for s in sigs)
        if ok:
            picked.append(r)
            sigs.append(r["sketch"])
    if len(picked) < 3:
        return {"ok": False, "reason": f"正交组不足 3 只({len(picked)})",
                "pool_size": len(rows)}
    # 源码解析(sources 库按全量 hash)
    src_map = {}
    for r in picked:
        f = root / "sources" / r["hash"][:2] / f"{r['hash']}.py"
        if not f.exists():
            return {"ok": False, "reason": f"源码缺失 {r['hash'][:12]}", "pool_size": len(rows)}
        src_map[r["hash"]] = f.read_text(encoding="utf-8")
    comp_src = _assemble_composite(picked, src_map)
    comp_hash = source_fingerprint(comp_src)
    return {
        "ok": True,
        "members": [{kk: r[kk] for kk in ("hash", "sr", "abs_sr", "stratum")}
                    for r in picked],
        "composite_source": comp_src,
        "composite_hash": comp_hash,
        "rule": {"k": k, "ortho_th": ortho_th, "weighting": "equal-sign-aligned",
                 "note": "规则预注册(dof=1):候补池两两|ρ|<0.3 按层内分数取前k等权"},
    }
