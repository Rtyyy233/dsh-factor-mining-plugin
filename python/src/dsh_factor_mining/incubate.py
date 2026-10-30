# -*- coding: utf-8 -*-
"""前向孵化(通道⑤)— PLAN-false-negative-remedy 批次 4(2026-09-13)。

终审法庭:候补线成员源码 hash 冻结,等真实新数据(日更管道喂入)积累
预注册窗口(120 交易日),cohort 一次性判决——复活线 = z_{1−α/K}/√n
(α=0.0253 层 Šidák,K=cohort 规模;生产预演线≈IC_IR 0.28@120日)。

纪律红线:
- 源码冻结:入队时拷贝全文进 cohort 记录,孵化期禁改(改=新候选重排);
- 判决只算 cohort.start 之后的新鲜日期(选择影子不跨样本);
- 幸存者落 survivors.jsonl(引擎写);submit 凭 cohort_id 豁免 dev deflated p
  (豁免的是选择运气价,不是其他门——噪声/正交/同构照旧)。

状态(均可重建?否——cohort 含冻结源码与起算点,是一等事实):
  stateRoot/incubation/cohorts.json    {cohorts: [...]}
  stateRoot/incubation/survivors.jsonl 引擎判决追加
(不进任何 reset scope——同 registered_ledger 待遇。)
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

ALPHA = 0.0253          # 层间 Šidák
DEFAULT_WINDOW = 120    # 交易日
MAX_COHORT = 50


def _root(root: str | Path) -> Path:
    return Path(root) / "incubation"


def _read_cohorts(root: str | Path) -> list[dict]:
    p = _root(root) / "cohorts.json"
    try:
        if p.exists():
            v = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(v, dict) and isinstance(v.get("cohorts"), list):
                return v["cohorts"]
    except Exception:
        pass
    return []


def _write_cohorts(root: str | Path, cohorts: list[dict]) -> None:
    d = _root(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "cohorts.json.tmp"
    tmp.write_text(json.dumps({"cohorts": cohorts}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(d / "cohorts.json")


def _trail_hashes(root: str | Path) -> dict[str, list]:
    """全量 hash → 最新的 trail 条目(sketch/sr)——入队解析与符号冻结。"""
    p = Path(root) / "trail_engine.json"
    out: dict[str, dict] = {}
    try:
        entries = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        h = e.get("source_hash")
        if h:
            out[str(h)] = e
    return out


def enqueue(root: str | Path, hashes: list[str], window_days: int = DEFAULT_WINDOW,
            start_date: str | None = None) -> dict[str, Any]:
    """入队(源码冻结)。hashes 可用前缀匹配 trail;容量 ≤MAX_COHORT。"""
    root = Path(root)
    if not hashes or not isinstance(hashes, list):
        return {"ok": False, "reason": "hashes 必填(cohort 成员,agent 自由选择——统计免费)"}
    if len(hashes) > MAX_COHORT:
        return {"ok": False, "reason": f"cohort 容量上限 {MAX_COHORT}"}
    window_days = int(window_days) or DEFAULT_WINDOW
    trail = _trail_hashes(root)
    members = []
    for hp in hashes:
        full = next((h for h in trail if h.startswith(str(hp))), None)
        if full is None:
            return {"ok": False, "reason": f"hash 前缀 {hp} 不在 trail"}
        f = root / "sources" / full[:2] / f"{full}.py"
        if not f.exists():
            return {"ok": False, "reason": f"源码缺失 {str(hp)[:12]}"}
        sr = ((trail[full].get("dsr_stats") or {}).get("sr_hat"))
        if not isinstance(sr, (int, float)):
            sr = trail[full].get("ic_ir")
        members.append({
            "hash": full,
            "source": f.read_text(encoding="utf-8"),   # 冻结
            "sign": 1 if (sr or 0) >= 0 else -1,        # 交易方向冻结
            "dev_sr": round(float(sr), 4) if isinstance(sr, (int, float)) else None,
        })
    cid = "INC-" + time.strftime("%Y%m%d-%H%M%S")
    cohorts = _read_cohorts(root)
    cohorts.append({
        "id": cid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "window_days": window_days,
        "start_date": start_date,      # None=入队日(正式);回放演练可显式指定
        "status": "open", "members": members,
    })
    _write_cohorts(root, cohorts)
    return {"ok": True, "cohort_id": cid, "n_members": len(members),
            "window_days": window_days,
            "note": "源码已冻结;start 起算(入队日或显式);判决=新鲜窗口一次性"}


def status(root: str | Path) -> dict[str, Any]:
    cohorts = _read_cohorts(root)
    out = []
    for c in cohorts:
        out.append({"id": c["id"], "ts": c["ts"], "status": c["status"],
                    "n_members": len(c.get("members") or []),
                    "window_days": c.get("window_days"),
                    "start_date": c.get("start_date")})
    return {"cohorts": out,
            "note": "进度与判决走 incubate.judge(需 env 已加载,数据含 start 后新窗)"}


def judge(root: str | Path, env, cohort_id: str, compile_fn) -> dict[str, Any]:
    """一次性判决:新鲜窗口(≥start 的日期)逐成员 IC;t=sign·ir·√n vs z。

    z = Φ⁻¹(1−α/K),α=0.0253。窗口不足(新鲜交易日 < window_days)→
    not_ready(不消耗判决)。幸存者追加 survivors.jsonl。
    """
    from .factor.evaluate import _cross_sectional_ic, _forward_returns, _pit_mask
    import numpy as np
    import pandas as pd

    cohorts = _read_cohorts(root)
    c = next((x for x in cohorts if x.get("id") == cohort_id), None)
    if c is None:
        return {"ok": False, "reason": f"cohort {cohort_id} 不存在"}
    if c.get("status") != "open":
        return {"ok": False, "reason": f"cohort 状态 {c.get('status')}(判决一次性)"}
    start = c.get("start_date") or c["ts"][:10]
    dates = pd.DatetimeIndex(env.dates)
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    fresh_idx = dates >= pd.Timestamp(start)
    n_fresh = int(fresh_idx.sum())
    need = int(c.get("window_days") or DEFAULT_WINDOW)
    if n_fresh < need:
        return {"ok": True, "judged": False, "not_ready": True,
                "fresh_days": n_fresh, "need": need,
                "note": "窗口未满——继续等新数据(不消耗判决)"}
    K = len(c.get("members") or [])
    z_th = _norm_ppf(1.0 - ALPHA / max(1, K))
    results, survivors = [], []
    for m in c["members"]:
        row = {"hash": m["hash"][:12], "dev_sr": m.get("dev_sr")}
        try:
            F = np.asarray(compile_fn(m["source"])(env), dtype=np.float64)
            ic = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
            ic = ic[ic.index >= pd.Timestamp(start)]
            n = len(ic)
            if n >= 20 and ic.std(ddof=1) > 0:
                ir = float(ic.mean() / ic.std(ddof=1))
                t = m["sign"] * ir * math.sqrt(n)
                row.update({"n": n, "ic_ir_fresh": round(ir, 4),
                            "t": round(t, 3), "z_th": round(z_th, 3),
                            "survived": bool(t > z_th)})
            else:
                row.update({"n": n, "survived": False, "reason": "新鲜样本不足"})
        except Exception as e:
            row.update({"survived": False, "error": f"{type(e).__name__}: {e}"[:120]})
        results.append(row)
        if row.get("survived"):
            survivors.append(m["hash"])
    # 幸存者落盘(引擎写;submit 豁免的权威源)
    if survivors:
        p = _root(root) / "survivors.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            for h in survivors:
                f.write(json.dumps({"cohort_id": cohort_id, "hash": h,
                                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "t": next(r["t"] for r in results
                                              if r["hash"] == h[:12])},
                                   ensure_ascii=False) + "\n")
    c["status"] = "judged"
    c["judged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_cohorts(root, cohorts)
    return {"ok": True, "judged": True, "cohort_id": cohort_id,
            "fresh_days": n_fresh, "K": K, "z_th": round(z_th, 3),
            "results": results, "n_survivors": len(survivors),
            "note": "幸存者可 submit(带 incubated_cohort 豁免 dev deflated p;其余门照旧)"}


def is_survivor(root: str | Path, source_hash: str) -> dict | None:
    """hash 是否为已判决幸存者(submit 豁免的验证口;引擎调用)。"""
    p = _root(root) / "survivors.jsonl"
    if not p.exists():
        return None
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("hash") == source_hash:
                return r
    except Exception:
        pass
    return None


def _norm_ppf(q: float) -> float:
    from .factor.gates import norm_ppf
    return norm_ppf(q)
