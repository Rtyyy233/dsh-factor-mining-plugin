# coding=utf-8
"""假阴性救助线运维模块（2026-10-06 总任务书 PLAN-unified-false-negative-rescue）。

纯人触发的运维入口，不进 bridge RPC 面（红线 3：回扫/放行只能由人显式
命令发起，引擎/模型都不得自动触发）。三个子命令：

- scan：只读重放全部注册候选的双价（主价 / 机制层价），出人审卡片。
  机制层价 = 多重检验 M 只按 registered_ledger 注册试验计的折减口径
  （影子，与引擎 evaluate 的 shadow_stratum 同式）。卡片含证据列
  （A4 残差+null 分位 / famsize+caliber / max_rho）。
- admit：人审后放行——四重拒绝（时序非法/超配额/已入册/正交≥0.80）+
  红线 6（未清 red_flags 不放行）+ 材料缺失检查；写 registry 与
  rescue_scans.jsonl 台账。缺省 dry-run（--yes 才落盘）。
- evidence-refit：残差化拟合 evidence_fit.json 的版本化冻结（旧档留存，
  重拟合=版本递增；残差读时确定性导出）。

硬规则（无参数可关）：
- 时序过滤（红线 4）：registered_ts ≥ 该 hash 首次 trail 评估 ts 的
  候选不进卡片、不放行——堵"先评估、见好再补注册"的事后补票路径。
- 配额硬顶（红线 5）：每轮每账本 ≤10 只（--quota 可显式改数值，
  上限始终存在且强制）。
- 台账只增不减（红线 7）：rescue_scans.jsonl 追加式，轮次号=既有
  最大 round+1（同轮重跑=新轮次，永不重复）。
- 确定性重放（红线 10）：一切计价 import 引擎自身代码
  （Bridge._n_eff_from_entries / _LuckSampler / dsr_p_from_stats），
  禁止重写公式；同输入同输出（卡片内容不含墙钟）。
- 模型面零暴露（红线 2）：本模块不注册任何 RPC；回扫判据（层价门槛/
  配额/时序过滤）只在本文件与输出卡片中，对模型不可见。

用法（repo 根）：
  PYTHONPATH=python/src python -m dsh_factor_mining.rescue_ops scan --root <stateRoot>
  PYTHONPATH=python/src python -m dsh_factor_mining.rescue_ops admit --root <stateRoot> --hashes h1,h2 --yes
  PYTHONPATH=python/src python -m dsh_factor_mining.rescue_ops evidence-refit --root <stateRoot>
（或直接 python python/src/dsh_factor_mining/rescue_ops.py ...，自举包上下文）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

ALPHA_LAYER = 0.0253        # 层间 Šidák 口径（与引擎 shadow_stratum 同值）
P_MAIN_GATE = 0.05          # 主价门（p_main>0.05 才是"被墙拦下"的假阴性候选）
DEFAULT_QUOTA = 10          # 红线 5：每轮每账本放行硬顶
ORTHO_REJECT_TH = 0.80      # 红线 9：正交硬阈值照走
ORTHO_ANNOTATE_TH = 0.60    # 0.60-0.80 卡片标警由人裁量
#: sketch 聚类最小长度/截断上限（坑⑤：stock h10≈158 / etf h20≈79 点）
MIN_SKETCH = {10: 158, 20: 79}
DEFAULT_MIN_SKETCH = 30
SCANS_FILE = "rescue_scans.jsonl"
#: 折减族红旗标记——这些旗**正是救助对象的挂线标记**（主墙/池基线问题），
#: 不算红线 6 的硬拒；其余旗（噪声门/展平/价差/前视/好得离谱/样本不足
#: 等）= 硬拒，不进卡片、不放行。
_DEFLATION_FLAG_MARKS = ("deflated p", "池分布基线", "多重检验校正")


def _hard_red_flags(flags) -> list:
    """红线 6 的「未清 red_flags」= 非折减族的硬拒旗列表。"""
    out = []
    for f in flags or []:
        s = f if isinstance(f, str) else str(f)
        if any(m in s for m in _DEFLATION_FLAG_MARKS):
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# 生产账本读取（全部只读；畸形行容错跳过）
# ---------------------------------------------------------------------------
def _read_json_list(path: Path) -> list:
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
            if isinstance(r, dict):
                out.append(r)
        except Exception:
            continue
    return out


def _pool_std_from_landscape(root: Path, horizon: int | None) -> tuple[float | None, dict]:
    """pool_std=(p90−p10)/2.5631（与 bridge._landscape_pool_std 同式；只读）。"""
    from .factor import random_gen
    land = random_gen.read_null_landscape(str(root))
    if not isinstance(land, dict) or not isinstance(land.get("ic_ir"), dict):
        return None, {}
    h = horizon
    if h is None and isinstance(land.get("horizons"), list) and land["horizons"]:
        h = int(land["horizons"][0])
    q = land["ic_ir"].get(str(h)) if h is not None else None
    if not isinstance(q, dict) or q.get("p10") is None or q.get("p90") is None:
        return None, {}
    a4_seg = (land.get("a4_dd") or {}).get(str(h)) if isinstance(land.get("a4_dd"), dict) else None
    return (q["p90"] - q["p10"]) / 2.5631, (a4_seg if isinstance(a4_seg, dict) else {})


def _registered_map(root: Path) -> dict[str, str]:
    """hash → 最早 ts_registered（B5：registered_ts=该 hash 最早台账行的 journal 时间戳）。"""
    out: dict[str, str] = {}
    for r in _read_jsonl(root / "registered_ledger.jsonl"):
        h, ts = r.get("source_hash"), r.get("ts_registered")
        if not h or not ts:
            continue
        if h not in out or str(ts) < out[h]:
            out[h] = str(ts)
    return out


def _registry_facts(root: Path) -> tuple[set, dict]:
    """(已入册 hash 集（ic 或 tail accepted）, hash→最新 registry 条目)。"""
    accepted, by_hash = set(), {}
    for e in _read_json_list(root / "registry.json"):
        if not isinstance(e, dict) or not e.get("source_hash"):
            continue
        h = e["source_hash"]
        by_hash[h] = e
        tr = e.get("tracks") or {}
        for k in ("ic", "tail"):
            t = tr.get(k) if isinstance(tr, dict) else None
            if isinstance(t, dict) and t.get("accepted"):
                accepted.add(h)
    return accepted, by_hash


def _trail_reps(trail: list) -> dict[str, dict]:
    """hash → 代表条目（ts 最新的带 dsr_stats+sketch 条目）+ 首评 ts / 首评 red_flags。"""
    best: dict[str, dict] = {}
    first_ts: dict[str, str] = {}
    first_flags: dict[str, list] = {}
    for e in trail:
        if not isinstance(e, dict):
            continue
        h = e.get("source_hash")
        if not h:
            continue
        ts = str(e.get("ts") or "")
        if h not in first_ts or ts < first_ts[h]:
            first_ts[h] = ts
            first_flags[h] = e.get("red_flags") or []
        sk = e.get("ic_series_sketch")
        d = e.get("dsr_stats")
        ok = (isinstance(sk, list) and len(sk) >= 5 and isinstance(d, dict)
              and isinstance(d.get("sr_hat"), (int, float))
              and math.isfinite(d["sr_hat"]))
        if not ok:
            continue
        prev = best.get(h)
        if prev is None or ts >= str(prev.get("ts") or ""):
            best[h] = e
    out = {}
    for h, e in best.items():
        out[h] = {"entry": e, "first_ts": first_ts.get(h, ""),
                  "first_red_flags": first_flags.get(h, []),
                  "horizon": e.get("horizon")}
    return out


def _graph_families(root: Path) -> dict[str, dict]:
    """派生图口径族：hash→{famsize, depth}（parent 边无向连通分量；ts 序位）。"""
    nodes: dict[str, dict] = {}
    adj: dict[str, set] = {}
    for n in _read_jsonl(root / "derivation_graph.jsonl"):
        h = n.get("hash")
        if not h:
            continue
        nodes[h] = n
        adj.setdefault(h, set())
        p = n.get("parent")
        if p:
            adj.setdefault(p, set()).add(h)
            adj[h].add(p)
    out: dict[str, dict] = {}
    seen: set = set()
    for h in nodes:
        if h in seen:
            continue
        comp, stack = set(), [h]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack.extend(adj.get(x, ()))
        seen |= comp
        members = sorted(
            (m for m in comp if m in nodes),
            key=lambda m: str(nodes[m].get("ts") or ""))
        for d, m in enumerate(members, 1):
            out[m] = {"famsize": len(members), "depth": d}
    return out


def _journal_excerpts(root: Path) -> dict[str, str]:
    """ref:hash → 预期正文摘录一行（跨全部 lane 的 live.md 与 arcs，只读）。"""
    ref_re = re.compile(r"^ref:([0-9a-fA-F]{8,64})\s*$", re.M)
    out: dict[str, str] = {}
    jdir = root / "journal"
    if not jdir.exists():
        return out
    files = []
    for lane in sorted(jdir.iterdir()):
        if not lane.is_dir():
            continue
        if (lane / "live.md").exists():
            files.append(lane / "live.md")
        arcs = lane / "arcs"
        if arcs.is_dir():
            files.extend(sorted(arcs.glob("arc-*.md")))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        blocks = re.split(r"(?m)^## ", text)
        for b in blocks[1:]:
            lines = b.splitlines()
            m = ref_re.search("\n".join(lines[:6]))
            if not m:
                continue
            h = m.group(1).lower()
            if h in out:
                continue
            body = [ln.strip() for ln in lines[1:]
                    if ln.strip() and not ln.startswith("> [update")
                    and not re.match(r"^(ref|ts|status)\s*:", ln.strip(), re.I)]
            out[h] = (body[0][:100] if body else "(预期正文为空)")
    return out


# ---------------------------------------------------------------------------
# 回扫上下文（scan/admit 共用；计价全部 import 引擎自身代码）
# ---------------------------------------------------------------------------
class ScanContext:
    def __init__(self, root: str | Path):
        from .bridge import Bridge
        from .factor.evaluate import _LuckSampler
        from .factor.gates import dsr_p_from_stats
        from .factor import evidence as ev

        self.root = Path(root)
        self.ev = ev
        self.dsr_p_from_stats = dsr_p_from_stats
        self.trail = _read_json_list(self.root / "trail_engine.json")
        self.reps = _trail_reps(self.trail)
        self.registered = _registered_map(self.root)
        self.accepted, self.registry_by_hash = _registry_facts(self.root)
        self.journal = _journal_excerpts(self.root)
        self.graph_fam = _graph_families(self.root)
        self.fit = ev.load_fit(str(self.root))
        self.fit_v = ev.latest_fit_version(self.fit)

        # 主 horizon：地形 horizons 首位（缺 → trail 条目众数）
        land_h = None
        from .factor import random_gen
        land = random_gen.read_null_landscape(str(self.root))
        if isinstance(land, dict) and isinstance(land.get("horizons"), list) and land["horizons"]:
            land_h = int(land["horizons"][0])
        if land_h is None:
            hs = [e.get("horizon") for e in self.trail
                  if isinstance(e, dict) and isinstance(e.get("horizon"), int)]
            land_h = int(np.bincount(hs).argmax()) if hs else None
        self.main_h = land_h
        self.pool_std, self.a4_null_seg = _pool_std_from_landscape(self.root, land_h)

        # 双价重放（红线 10：引擎自身代码；fresh sampler=确定性）。
        # P1（2026-10-06）：机制层试验集经 evidence.mechanism_trials——
        # 台账独有 hash 补无 sketch 合成独立单例，防 state.reset 塌缩层墙
        mech_entries = ev.mechanism_trials(self.root, self.trail)
        self.stats_main = Bridge._n_eff_from_entries(
            None, self.trail, main_horizon=self.main_h, sampler=_LuckSampler())
        self.stats_mech = Bridge._n_eff_from_entries(
            None, mech_entries, main_horizon=self.main_h, sampler=_LuckSampler())
        self.bar_main = float(self.stats_main["bar_sigma"])
        self.bar_mech = float(self.stats_mech["bar_sigma"])

        # sketch 聚类人口（graph 外 hash 的 famsize 兜底口径）
        min_sk = MIN_SKETCH.get(self.main_h, DEFAULT_MIN_SKETCH)
        pop = [(h, r) for h, r in self.reps.items()
               if isinstance(r["entry"].get("ic_series_sketch"), list)
               and len(r["entry"]["ic_series_sketch"]) >= min_sk]
        self._pop_order = sorted(h for h, _ in pop)
        self._pop = {h: r for h, r in pop}
        if len(pop) >= 2:
            skmat = ev.truncate_common(
                [r["entry"]["ic_series_sketch"] for _, r in pop])
            cap = min(skmat.shape[1], min_sk)
            fam, depth, size = ev.cluster_families(
                skmat[:, :cap], [str(r["entry"].get("ts") or "") for _, r in pop])
            self._pop_list = [h for h, _ in pop]
            self._sk_fam = {h: {"famsize": int(size[int(fam[i])]),
                                "depth": int(depth[i])}
                            for i, (h, _) in enumerate(pop)}
        else:
            self._pop_list = []
            self._sk_fam = {}

    # ---- 单候选卡片行 ----
    def card_row(self, h: str, alpha: float) -> dict:
        r = self.reps.get(h)
        e = r["entry"]
        d = e.get("dsr_stats") or {}
        sketch = e.get("ic_series_sketch") or []
        p_main = self.dsr_p_from_stats(
            d.get("sr_hat"), d.get("skew"), d.get("kurt"), d.get("n_obs"),
            self.bar_main, self.pool_std)
        p_mech = self.dsr_p_from_stats(
            d.get("sr_hat"), d.get("skew"), d.get("kurt"), d.get("n_obs"),
            self.bar_mech, self.pool_std)
        # 证据列（E7）：A4 残差+null 分位 / famsize+caliber / max_rho
        a4 = self.ev.a4_dd_ratio(sketch) if len(sketch) >= 5 else None
        sr_abs = abs(float(d["sr_hat"]))
        coef = (self.ev.get_version(self.fit, self.fit_v) or {}).get("coef")
        a4_resid = self.ev.residual(coef, sr_abs, a4) if (coef and a4 is not None) else None
        null_pct = self.ev.percentile_against(
            [self.a4_null_seg.get(f"p{k}") for k in (10, 50, 90, 95)]
            if self.a4_null_seg else [], a4)
        # 尾轨候选（registry 历史按 tail 轨提交）不消费 A4 证据语义（红线 12）
        reg_e = self.registry_by_hash.get(h) or {}
        tail_track = bool(reg_e) and reg_e.get("admit_basis") == "tail"
        # famsize：派生图优先、sketch 兜底（caliber 标签强制随值）
        if h in self.graph_fam:
            fam, caliber = self.graph_fam[h]["famsize"], "graph"
            depth = self.graph_fam[h]["depth"]
        elif h in self._sk_fam:
            fam, caliber = self._sk_fam[h]["famsize"], "sketch"
            depth = self._sk_fam[h]["depth"]
        else:
            fam, caliber, depth = None, "unavailable", None
        # max_rho：提交时正交门权威值优先，否则 sketch 口径复算
        ortho = reg_e.get("orthogonality") if isinstance(reg_e, dict) else None
        max_rho = (ortho.get("max_corr")
                   if isinstance(ortho, dict)
                   and isinstance(ortho.get("max_corr"), (int, float)) else None)
        rho_src = "registry_ortho" if max_rho is not None else None
        if max_rho is None:
            max_rho, rho_src = self._max_rho_to_registry(h, sketch)
        return {
            "hash": h,
            "horizon": e.get("horizon"),
            "origin": e.get("origin"),
            "sr_abs": round(sr_abs, 4),
            "p_main": p_main,
            "p_mech": p_mech,
            "a4_dd": (round(a4, 4) if a4 is not None else None),
            "a4_dd_resid": (round(a4_resid, 4) if a4_resid is not None else None),
            "fit_v": (self.fit_v if a4_resid is not None else None),
            "a4_null_p50": self.a4_null_seg.get("p50"),
            "a4_null_p90": self.a4_null_seg.get("p90"),
            "a4_null_pct": (round(null_pct, 1) if null_pct is not None else None),
            "calibrated_for": ("tail_track" if tail_track else "ic_track"),
            "famsize": fam,
            "famsize_caliber": caliber,
            "famsize_depth": depth,
            "max_rho": (round(float(max_rho), 3)
                        if isinstance(max_rho, (int, float)) else None),
            "max_rho_source": rho_src,
            "registered_ts": self.registered.get(h),
            "first_eval_ts": r["first_ts"],
            "journal_excerpt": self.journal.get(h),
            "first_red_flags": r["first_red_flags"],
            "rep_red_flags": e.get("red_flags") or [],
            "name_hint": (reg_e.get("name") if isinstance(reg_e, dict) else None),
            "alpha": alpha,
        }

    def _max_rho_to_registry(self, h: str, sketch: list):
        """候选 sketch 与全体在册 sketches 的最大 |ρ|（在册无 sketch 的跳过）。"""
        reg_sk = []
        for a in sorted(self.accepted):
            r = self.reps.get(a)
            if not r:
                continue
            s = r["entry"].get("ic_series_sketch")
            if isinstance(s, list) and len(s) >= 5:
                reg_sk.append(s)
        if not reg_sk or len(sketch) < 5:
            return None, None
        best = None
        for s in reg_sk:
            c = self.ev.corr_pair(sketch, s)
            if c is not None and (best is None or abs(c) > abs(best)):
                best = c
        return (float(best) if best is not None else None), "sketch_recompute"


# ---------------------------------------------------------------------------
# scan（只读）
# ---------------------------------------------------------------------------
def _filter_chain(ctx: ScanContext, h: str, alpha: float) -> tuple[str, dict | None]:
    """B.1 过滤链。返回 (status, card_row)；status=ok 时 card_row 非空。

    顺序：注册成员 → 时序（红线 4）→ 已入册 → 未清 red_flags（红线 6）
    → 材料缺失 → 主价已过（非假阴性）→ 层价两档。"""
    if h not in ctx.registered:
        return "not_registered", None
    r = ctx.reps.get(h)
    if r is None:
        return "no_stats", None
    if str(ctx.registered[h]) >= str(r["first_ts"]):
        return "timing", None
    if h in ctx.accepted:
        return "already_accepted", None
    if _hard_red_flags(r["entry"].get("red_flags")):
        return "red_flags", None
    row = ctx.card_row(h, alpha)
    if row["p_main"] is None or row["p_mech"] is None:
        return "no_stats", None
    if row["p_main"] <= P_MAIN_GATE:
        return "main_significant", row
    if row["p_mech"] > alpha:
        return ("p_mech_loose" if row["p_mech"] <= 0.05 else "p_mech_high"), row
    return "ok", row


def _family_dedup(rows: list[dict]) -> dict[str, dict]:
    """卡片内族归并（sketch |ρ|≥0.6）：hash → {rep, n}。代表=族内 p_mech 最小。"""
    from .factor import evidence as ev
    if not rows:
        return {}
    sketches = {}
    for row in rows:
        r = None
        ctx = row.get("_ctx")
        if ctx is not None:
            e = ctx.reps.get(row["hash"])
            s = (e or {}).get("entry", {}).get("ic_series_sketch")
            if isinstance(s, list) and len(s) >= 5:
                sketches[row["hash"]] = s
    hashes = [row["hash"] for row in rows if row["hash"] in sketches]
    out = {row["hash"]: {"rep": row["hash"], "n": 1} for row in rows}
    if len(hashes) < 2:
        return out
    try:
        gram = ev.corr_matrix([sketches[h] for h in hashes])
    except Exception:
        return out
    fam, _depth, size = ev.cluster_families(
        ev.truncate_common([sketches[h] for h in hashes]),
        [str(i) for i in range(len(hashes))])
    pm = {row["hash"]: row["p_mech"] for row in rows}
    for f in range(len(size)):
        members = [hashes[i] for i in range(len(hashes)) if int(fam[i]) == f]
        if len(members) <= 1:
            continue
        rep = min(members, key=lambda h: (pm.get(h) is None, pm.get(h) or 9.9))
        for h in members:
            out[h] = {"rep": rep, "n": len(members)}
    return out


def _fmt(v, nd=4):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def cmd_scan(root: str, out_dir: str | None = None,
             alpha: float = ALPHA_LAYER) -> dict:
    ctx = ScanContext(root)
    universe = sorted(ctx.registered)
    statuses: dict[str, list[str]] = {}
    cards: list[dict] = []
    loose = 0
    for h in universe:
        status, row = _filter_chain(ctx, h, alpha)
        statuses.setdefault(status, []).append(h)
        if status == "ok":
            row = dict(row)
            row["_ctx"] = ctx
            cards.append(row)
        elif status in ("p_mech_loose",) and row is not None:
            loose += 1
    cards.sort(key=lambda r: (r["p_mech"] is None, r["p_mech"] or 9.9, r["hash"]))
    fam = _family_dedup(cards)
    for row in cards:
        f = fam.get(row["hash"]) or {"rep": row["hash"], "n": 1}
        row["family_rep"] = f["rep"]
        row["family_n"] = f["n"]
        row.pop("_ctx", None)

    lines = []
    lines.append(f"# 假阴性人审回扫卡片——{ctx.root}")
    lines.append("")
    lines.append(
        f"- 墙：bar_main={ctx.bar_main:.3f}σ（M={int(ctx.stats_main['n_trials'])}）"
        f" / bar_mech={ctx.bar_mech:.3f}σ（M={int(ctx.stats_mech['n_trials'])}）"
        f" / pool_std={_fmt(ctx.pool_std)}")
    lines.append(
        f"- 注册候选 {len(universe)} 只；过滤落点："
        + "，".join(f"{k}={len(v)}" for k, v in sorted(
            statuses.items(), key=lambda kv: -len(kv[1]))))
    lines.append(
        f"- 层价放行档 p_mech≤{alpha}：{len(cards)} 只（0.05 灵敏度档含 loose 另 {loose} 只）")
    if not cards:
        lines.append("")
        lines.append("（本轮无候选卡片）")
    else:
        lines.append("")
        lines.append("| hash | \\|sr\\| | p_main | p_mech | a4_dd_resid(null分位) "
                     "| famsize(caliber) | max_rho | 注册时间 | 首评时间 "
                     "| 族代表(族内数) | origin | journal 摘录 | 首评 red_flags |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in cards:
            a4c = ("不适用(尾轨)" if r["calibrated_for"] == "tail_track" else
                   f"{_fmt(r['a4_dd_resid'])}"
                   f"(null p50={_fmt(r['a4_null_p50'], 3)},pct={_fmt(r['a4_null_pct'], 0)})")
            famc = f"{r['famsize']}({r['famsize_caliber']})"
            fl = "; ".join(str(x)[:40] for x in (r["first_red_flags"] or [])) or "-"
            lines.append(
                f"| {r['hash']} | {_fmt(r['sr_abs'], 3)} | {_fmt(r['p_main'])} "
                f"| {_fmt(r['p_mech'])} | {a4c} | {famc} | {_fmt(r['max_rho'], 3)} "
                f"| {r['registered_ts']} | {r['first_eval_ts']} "
                f"| {r['family_rep']}({r['family_n']}) | {r['origin']} "
                f"| {r['journal_excerpt'] or '-'} | {fl} |")
    md = "\n".join(lines)
    print(md)

    if out_dir:
        od = Path(out_dir)
        od.mkdir(parents=True, exist_ok=True)
        label = time.strftime("%Y%m%dT%H%M%S")
        (od / f"rescue_card_{label}.md").write_text(md + "\n", encoding="utf-8")
        jl = od / f"rescue_card_{label}.jsonl"
        with open(jl, "w", encoding="utf-8") as f:
            for r in cards:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        print(f"\n[scan] 卡片已落盘：{jl}", file=sys.stderr)
    return {"n_card": len(cards), "statuses": {k: len(v) for k, v in statuses.items()},
            "bar_main": ctx.bar_main, "bar_mech": ctx.bar_mech,
            "pool_std": ctx.pool_std}


# ---------------------------------------------------------------------------
# admit（写 registry + rescue_scans 台账；缺省 dry-run）
# ---------------------------------------------------------------------------
def _next_round(root: Path) -> int:
    rows = _read_jsonl(root / SCANS_FILE)
    nums = [int(r["round"]) for r in rows
            if isinstance(r.get("round"), int)]
    return (max(nums) + 1) if nums else 1


def cmd_admit(root: str, hashes: list[str], yes: bool = False,
              quota: int = DEFAULT_QUOTA,
              basis: str = "human_rescue_layer", trigger: str = "manual",
              mode: str = "human", label: str = "人审回扫",
              extra_snapshots: dict | None = None,
              deferred: list | None = None) -> dict:
    """放行（人审与自动共用写路径；自动轮经 basis/mode/trigger 区分溯源）。

    extra_snapshots: hash → 追加进 rescue_snapshot 的判据块（auto_v1 用）。
    deferred: 缓议名单（如尾轨签名候选），只进台账不进 registry。
    """
    from .filelock import state_write_lock
    from .state import read_registry, write_registry
    from . import __version__

    quota = max(1, int(quota))
    ctx = ScanContext(root)
    hashes = list(dict.fromkeys(hashes))  # 去重保序
    rejected: list[dict] = []
    admitted: list[dict] = []
    plan_rows: list[dict] = []

    def _reject(h, reason):
        rejected.append({"hash": h, "reason": reason})
        plan_rows.append({"hash": h, "admit": False, "reason": reason})

    def _materials_ok(h, row):
        src_p = ctx.root / "sources" / h[:2] / f"{h}.py"
        if not src_p.exists():
            _reject(h, "材料缺失：sources 外挂库无该 hash 源码")
            return False
        return True

    # 预检查（锁外；quota/已入册/正交在锁内终审）
    for h in hashes:
        if h not in ctx.registered:
            _reject(h, "非注册候选：registered_ledger.jsonl 无此 hash")
            continue
        r = ctx.reps.get(h)
        if r is None:
            _reject(h, "材料缺失：trail_engine 无该 hash 的充分统计量条目")
            continue
        if str(ctx.registered[h]) >= str(r["first_ts"]):
            _reject(h, f"时序非法（红线 4）：registered_ts={ctx.registered[h]} "
                       f"≥ 首评 ts={r['first_ts']}（先评估后注册=事后补票）")
            continue
        if _hard_red_flags(r["entry"].get("red_flags")):
            _reject(h, "未清硬拒 red_flags（红线 6：只救单纯折减挂线——"
                     "噪声/展平/价差/前视等硬拒不放行；deflated p 不显著"
                     "与缺池分布基线属折减族挂线标记，不算硬拒）")
            continue
        plan_rows.append({"hash": h, "admit": None})

    pending = [p["hash"] for p in plan_rows if p["admit"] is None]
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    round_id = _next_round(ctx.root)

    if not yes:
        print(f"[admit][dry-run] round={round_id} quota={quota} root={ctx.root}")
        for p in plan_rows:
            if p["admit"] is False:
                print(f"  拒 {p['hash']}  {p['reason']}")
            else:
                row = ctx.card_row(p["hash"], ALPHA_LAYER)
                mr = row["max_rho"] if isinstance(row["max_rho"], float) else -1.0
                note = ""
                if mr >= ORTHO_REJECT_TH:
                    note = f"（max|ρ|≥{ORTHO_REJECT_TH:.2f}：--yes 将被正交硬拒）"
                elif mr >= ORTHO_ANNOTATE_TH:
                    note = "（正交标警 0.60-0.80：人裁量）"
                print(f"  可放行 {p['hash']}  p_main={_fmt(row['p_main'])} "
                      f"p_mech={_fmt(row['p_mech'])} max_rho={_fmt(row['max_rho'], 3)}{note}")
        print("确认放行加 --yes（写 registry + rescue_scans.jsonl；缺省不落盘）")
        return {"dry_run": True, "rejected": rejected, "pending": pending}

    with state_write_lock(str(ctx.root)):
        registry = read_registry(str(ctx.root))
        accepted_now = {e.get("source_hash") for e in registry if isinstance(e, dict)
                        and e.get("accepted")}
        names_now = {e.get("name") for e in registry if isinstance(e, dict)
                     and e.get("name")}
        round_id = _next_round(ctx.root)  # 锁内终审轮次号
        n_admitted = 0
        for h in pending:
            r = ctx.reps[h]
            row = ctx.card_row(h, ALPHA_LAYER)
            if n_admitted >= quota:
                _reject(h, f"超配额（红线 5）：本轮已放行 {n_admitted}/{quota}")
                continue
            if h in accepted_now:
                _reject(h, "已入册：registry 存在 accepted 条目（tracks.ic/tail）")
                continue
            # 正交硬阈值照走（红线 9）：与在册全体 sketches 逐日秩相关口径复算
            ortho_v = row["max_rho"] if row["max_rho_source"] == "sketch_recompute" else None
            if ortho_v is None:
                ortho_v, _ = ctx._max_rho_to_registry(
                    h, r["entry"].get("ic_series_sketch") or [])
            if isinstance(ortho_v, (int, float)) and abs(float(ortho_v)) >= ORTHO_REJECT_TH:
                _reject(h, (f"正交硬拒（红线 9）：与在册 max|ρ|={float(ortho_v):.2f} "
                            f"≥ {ORTHO_REJECT_TH:.2f}"))
                continue
            if not _materials_ok(h, row):
                continue
            if isinstance(ortho_v, (int, float)) and abs(float(ortho_v)) >= ORTHO_ANNOTATE_TH:
                print(f"[admit] 正交标警：{h} max|ρ|={float(ortho_v):.2f} "
                      f"(0.60-0.80 人裁量区间)——照常放行", file=sys.stderr)
            # ---- 写入 registry 条目（红线 8：可整批识别）----
            reg_e = ctx.registry_by_hash.get(h) or {}
            name = reg_e.get("name") or f"rescue_{h[:8]}"
            if name in names_now:
                name = f"{name}_r{round_id}"
            names_now.add(name)
            src_p = ctx.root / "sources" / h[:2] / f"{h}.py"
            source = src_p.read_text(encoding="utf-8")
            snap = {"p_main": row["p_main"], "p_mech": row["p_mech"],
                    "bar_main": round(ctx.bar_main, 4),
                    "bar_mech": round(ctx.bar_mech, 4),
                    "pool_std": (round(ctx.pool_std, 4)
                                 if isinstance(ctx.pool_std, float) else None),
                    "alpha_layer": ALPHA_LAYER, "round_id": round_id,
                    "scan_ts": now}
            snap.update((extra_snapshots or {}).get(h) or {})
            entry = {
                "name": name, "ts": now,
                "signal": reg_e.get("signal")
                          or (f"({label}放行；journal 预期："
                              f"{row['journal_excerpt'] or '无'})"),
                "accepted": True,
                "reason": (f"{label}放行 round {round_id}：p_main={_fmt(row['p_main'])} "
                           f"> {P_MAIN_GATE} 且 p_mech={_fmt(row['p_mech'])} ≤ {ALPHA_LAYER}"),
                "ic_ir_train": (r["entry"].get("dsr_stats") or {}).get("sr_hat"),
                "verified": True,  # 数字源=trail_engine 硬账本重放，非模型自报
                "diagnosis": None,
                "admit_basis": basis,
                "rescue_round": round_id,
                "tracks": {"ic": {"accepted": True,
                                  "reason": f"{basis} round {round_id}"},
                           "tail": {"accepted": False,
                                    "reason": f"{label}不判尾轨"}},
                "dual_pass": False,
                "source": source, "source_hash": h,
                "origin": r["entry"].get("origin"),
                "engine_version": __version__,
                "verdict": r["entry"].get("verdict"),
                "red_flags": [],
                "rescue_snapshot": snap,
            }
            registry.append(entry)
            accepted_now.add(h)
            names_now.add(name)
            n_admitted += 1
            admitted.append({"hash": h, "name": name})
            for p in plan_rows:
                if p["hash"] == h and p["admit"] is None:
                    p["admit"] = True
                    p["name"] = name
        if admitted:
            write_registry(registry, str(ctx.root))
        # 台账（红线 7/8）：只增不减；candidates=全列含被否；mode/trigger 区分人审与自动
        scan_row = {
            "round": round_id, "ts": now, "root": str(ctx.root),
            "candidates": [dict(p, reason=p.get("reason") or "放行") for p in plan_rows],
            "admitted": admitted, "rejected": rejected,
            "operator": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
            "quota": quota,
            "mode": mode, "trigger": trigger, "basis": basis,
            "deferred_human": list(deferred or []),
        }
        with open(ctx.root / SCANS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(scan_row, ensure_ascii=False) + "\n")
    print(f"[admit] round={round_id}：放行 {len(admitted)} / 拒绝 {len(rejected)} "
          f"（quota={quota}，台账 {ctx.root / SCANS_FILE}）")
    for a in admitted:
        print(f"  + {a['hash']} → registry 「{a['name']}」")
    for rj in rejected:
        print(f"  - {rj['hash']}  {rj['reason']}")
    return {"round": round_id, "admitted": admitted, "rejected": rejected}


# ---------------------------------------------------------------------------
# evidence-refit（拟合版本化冻结；残差读时导出）
# ---------------------------------------------------------------------------
def cmd_evidence_refit(root: str) -> dict:
    from .factor import evidence as ev

    root_p = Path(root)
    trail = _read_json_list(root_p / "trail_engine.json")
    # 拟合集=random 出身池，试验单元 (hash, horizon) 去重取最新条目
    reps: dict[tuple, dict] = {}
    for e in trail:
        if not isinstance(e, dict) or e.get("origin") != "random":
            continue
        h = e.get("source_hash")
        sk = e.get("ic_series_sketch")
        d = e.get("dsr_stats") or {}
        sr = d.get("sr_hat")
        if not (isinstance(sr, (int, float)) and math.isfinite(sr)):
            sr = e.get("ic_ir")
        if not h or not (isinstance(sk, list) and len(sk) >= 5):
            continue
        if not (isinstance(sr, (int, float)) and math.isfinite(sr)):
            continue
        key = (h, e.get("horizon"))
        ts = str(e.get("ts") or "")
        if key not in reps or ts >= str(reps[key]["ts"]):
            reps[key] = {"ts": ts, "sr": float(sr), "sk": sk}
    items = list(reps.values())
    a4s = []
    srs = []
    for it in items:
        a4 = ev.a4_dd_ratio(it["sk"])
        if a4 is None:
            continue
        a4s.append(a4)
        srs.append(abs(it["sr"]))
    coef = ev.ols_fit_a4(srs, a4s)
    if coef is None:
        raise SystemExit(
            f"[evidence-refit] 拟合集不足（random 出身有效样本 {len(a4s)} < 10）"
            "——拒绝拟合（红线 11：不带病冻结）")
    old_fit = ev.load_fit(str(root_p))
    old_v = ev.latest_fit_version(old_fit)
    old_coef = (ev.get_version(old_fit, old_v) or {}).get("coef")
    old_n = (ev.get_version(old_fit, old_v) or {}).get("n_fit")
    new_v = f"v{(int(old_v[1:]) + 1) if old_v else 1}"
    payload = {
        "coef": [round(c, 8) for c in coef],
        "fit_set": f"random_origin@{time.strftime('%Y-%m-%d')}",
        "n_fit": len(a4s),
        "formula": "a4 ~ 1 + sr_abs + sr_abs^2 (OLS, random-origin pool)",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if old_v:
        payload["supersedes"] = old_v
    ev.write_fit_version(str(root_p), new_v, payload)
    print(f"[evidence-refit] {root_p}")
    if old_v:
        print(f"  旧 {old_v}: coef={[round(c, 6) for c in old_coef]} n_fit={old_n}")
    print(f"  新 {new_v}: coef={[round(c, 6) for c in coef]} n_fit={len(a4s)}")
    print(f"  文件：{ev.fit_path(root_p)}（旧档留存，版本只增）")
    return {"version": new_v, "coef": coef, "n_fit": len(a4s)}


# ---------------------------------------------------------------------------
# auto（2026-10-06 特质奖励全自动 P2：判据机械选只 + 放行）
# 红线 14：判据与阈值对模型不可见、不进任何 prompt 面。
# 红线 15：enabled=false（缺省）=逐比特现状——maybe_round 直接 None。
# ---------------------------------------------------------------------------
RESCUE_AUTO_DEFAULTS = {
    "enabled": False,
    "quota_per_round": 10,       # 与人审轮同顶（红线 5）
    "min_interval_h": 24,        # tick 触发的最小轮间隔（小时）
    "a4_null_pct_max": 10.0,     # A4 证据门：null 分位 ≤ 此值（越小越极端）
    "require_a4": True,          # False=退化为纯层价判据（不建议）
    "exclude_tail_track": True,  # 尾轨签名候选不自动放（记 deferred_human）
}
AUTO_BASIS = "auto_rescue_layer_v1"


def rescue_auto_config(mining_state: dict) -> dict:
    """RESCUE_AUTO_DEFAULTS ← mining_state["rescue_auto"] 平铺覆盖。"""
    cfg = dict(RESCUE_AUTO_DEFAULTS)
    over = (mining_state or {}).get("rescue_auto")
    if isinstance(over, dict):
        for k, v in over.items():
            if k in cfg:
                cfg[k] = v
    return cfg


def _set_rescue_auto_flag(root: Path, enabled: bool) -> dict:
    from .state import read_mining_state
    from .filelock import state_write_lock
    with state_write_lock(str(root)):
        st = read_mining_state(str(root))
        cfg = st.get("rescue_auto")
        st["rescue_auto"] = {**(cfg if isinstance(cfg, dict) else {}),
                             "enabled": bool(enabled)}
        p = root / "mining_state.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
    return st["rescue_auto"]


def _auto_candidates(ctx: "ScanContext", cfg: dict):
    """全机械选型：0.0253 档(scan 过滤链) + |max_rho|<0.80(绝对值!) +
    IC 轨 + A4 null 分位 ≤ 阈值 且 残差<0 → 每族一只(A4 最深) → 配额。
    返回 (selected_rows, deferred_tail_hashes, n_after_filters)。"""
    rows = []
    for h in sorted(ctx.registered):
        status, row = _filter_chain(ctx, h, ALPHA_LAYER)
        if status != "ok" or row is None:
            continue
        row = dict(row)
        row["_ctx"] = ctx
        rows.append(row)
    # 正交硬门（绝对值——2026-10-06 stock 人审轮实测：-0.82 按原始值漏过、
    # admit 门按 |ρ| 拦下；自动选型必须同口径前置）
    rows = [r for r in rows
            if not (isinstance(r.get("max_rho"), (int, float))
                    and abs(float(r["max_rho"])) >= ORTHO_REJECT_TH)]
    deferred_tail = [r["hash"] for r in rows
                     if r.get("calibrated_for") == "tail_track"
                     and cfg.get("exclude_tail_track", True)]
    rows = [r for r in rows
            if not (r.get("calibrated_for") == "tail_track"
                    and cfg.get("exclude_tail_track", True))]
    if cfg.get("require_a4", True):
        def _a4_ok(r):
            pct, resid = r.get("a4_null_pct"), r.get("a4_dd_resid")
            return (isinstance(resid, (int, float)) and resid < 0
                    and isinstance(pct, (int, float))
                    and pct <= float(cfg.get("a4_null_pct_max", 10.0)))
        rows = [r for r in rows if _a4_ok(r)]
    n_after = len(rows)
    rows.sort(key=lambda r: (r["a4_dd_resid"]
                             if isinstance(r.get("a4_dd_resid"), (int, float))
                             else 0.0, r["hash"]))
    fam = _family_dedup(rows)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        f = fam.get(r["hash"]) or {"rep": r["hash"], "n": 1}
        groups.setdefault(f["rep"], []).append(r)
    picked = []
    for rep, members in groups.items():
        members.sort(key=lambda r: (r["a4_dd_resid"]
                                    if isinstance(r.get("a4_dd_resid"), (int, float))
                                    else 0.0, r["hash"]))
        picked.append(members[0])
    picked.sort(key=lambda r: (r["a4_dd_resid"]
                               if isinstance(r.get("a4_dd_resid"), (int, float))
                               else 0.0, r["hash"]))
    quota = max(1, int(cfg.get("quota_per_round", DEFAULT_QUOTA)))
    return picked[:quota], deferred_tail, n_after


def _auto_criteria_block(r: dict, cfg: dict) -> dict:
    return {"criteria": "auto_v1",
            "a4_null_pct": r.get("a4_null_pct"),
            "a4_null_pct_max": cfg.get("a4_null_pct_max", 10.0),
            "a4_dd_resid": r.get("a4_dd_resid"),
            "famsize": r.get("famsize"),
            "famsize_caliber": r.get("famsize_caliber"),
            "max_rho": r.get("max_rho")}


def run_auto_round(root: str, trigger: str = "manual",
                   dry_run: bool = True, quiet: bool = False) -> dict:
    """一轮自动放行。quiet=True 时 stdout 重定向到 stderr（桥内 RPC 通道
    严禁混入打印——P3 挂点的 tick 路径必须静默）。"""
    from .state import read_mining_state
    cfg = rescue_auto_config(read_mining_state(root))
    ctx = ScanContext(root)
    selected, deferred_tail, n_after = _auto_candidates(ctx, cfg)
    quota = max(1, int(cfg.get("quota_per_round", DEFAULT_QUOTA)))
    if dry_run:
        print(f"[auto][dry-run] root={root} 判据=auto_v1：过滤后合格 {n_after} 只，"
              f"按族+A4 深度选 {len(selected)}/{quota}，尾轨缓议 {len(deferred_tail)} 只")
        for r in selected:
            print(f"  可放行 {r['hash']} p_main={_fmt(r['p_main'])} "
                  f"p_mech={_fmt(r['p_mech'])} max_rho={_fmt(r['max_rho'], 3)} "
                  f"a4_resid={_fmt(r['a4_dd_resid'])} pct={_fmt(r['a4_null_pct'], 0)}")
        if deferred_tail:
            print(f"  尾轨缓议: {', '.join(deferred_tail)}")
        print("确认执行加 --yes（写 registry + rescue_scans.jsonl）")
        return {"dry_run": True, "selected": [r["hash"] for r in selected],
                "deferred_tail": deferred_tail}
    extra = {r["hash"]: _auto_criteria_block(r, cfg) for r in selected}
    if quiet:
        import contextlib
        with contextlib.redirect_stdout(sys.stderr):
            return cmd_admit(root, [r["hash"] for r in selected], yes=True,
                             quota=quota, basis=AUTO_BASIS, trigger=trigger,
                             mode="auto", label="自动回扫",
                             extra_snapshots=extra, deferred=deferred_tail)
    return cmd_admit(root, [r["hash"] for r in selected], yes=True,
                     quota=quota, basis=AUTO_BASIS, trigger=trigger,
                     mode="auto", label="自动回扫",
                     extra_snapshots=extra, deferred=deferred_tail)


def maybe_round(bridge) -> dict | None:
    """桥内节流钩子（P3 挂点：_loop_directive 尾部调用）。

    enabled=false → None（红线 15：零行为）；节流按 rescue_scans.jsonl 末轮
    ts（本地时区，与写入 time.strftime 同口径）；任何失败吃掉不阻断。"""
    try:
        root = getattr(bridge, "state_root", None)
        if not root:
            return None
        from .state import read_mining_state
        cfg = rescue_auto_config(read_mining_state(root))
        if not cfg.get("enabled"):
            return None
        rows = _read_jsonl(Path(root) / SCANS_FILE)
        if rows:
            try:
                from datetime import datetime
                last = datetime.fromisoformat(str(rows[-1].get("ts") or ""))
                if ((datetime.now() - last).total_seconds() / 3600.0
                        < float(cfg.get("min_interval_h", 24))):
                    return None
            except Exception:
                pass
        return run_auto_round(root, trigger="tick", dry_run=False, quiet=True)
    except Exception as e:
        try:
            sys.stderr.write(f"[rescue_auto] tick 失败(不阻断): {e!r}\n")
        except Exception:
            pass
        return None


def cmd_auto(root: str, yes: bool = False, enable: bool = False,
             disable: bool = False) -> dict:
    root_p = Path(root)
    if enable or disable:
        cfg = _set_rescue_auto_flag(root_p, enable)
        print(f"[auto] rescue_auto.enabled={cfg.get('enabled')} 已写入 mining_state"
              f"（tick 触发需宿主重启后生效；--yes 可立即手动跑一轮）")
    return run_auto_round(root, trigger="manual", dry_run=not yes)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m dsh_factor_mining.rescue_ops",
        description="假阴性救助线运维（人触发；scan 只读 / admit 放行 / evidence-refit 拟合）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="只读回扫：注册候选双价重放 + 人审卡片")
    s.add_argument("--root", required=True, help="stateRoot（生产账本根）")
    s.add_argument("--out", default=None, help="卡片落盘目录（缺省只打印 stdout）")
    s.add_argument("--alpha", type=float, default=ALPHA_LAYER,
                   help=f"层价放行档（缺省 {ALPHA_LAYER}，层间 Šidák）")
    a = sub.add_parser("admit", help="人审放行（缺省 dry-run；--yes 落盘）")
    a.add_argument("--root", required=True)
    a.add_argument("--hashes", required=True, help="逗号分隔的 source_hash 列表")
    a.add_argument("--yes", action="store_true", help="实际写 registry+台账")
    a.add_argument("--quota", type=int, default=DEFAULT_QUOTA,
                   help=f"本轮放行硬顶（缺省 {DEFAULT_QUOTA}，红线 5）")
    e = sub.add_parser("evidence-refit", help="A4 残差化拟合冻结/重拟合（版本递增）")
    e.add_argument("--root", required=True)
    au = sub.add_parser("auto", help="特质奖励自动轮（判据机械；缺省 dry-run，--yes 执行；--enable/--disable 持久开关）")
    au.add_argument("--root", required=True)
    au.add_argument("--yes", action="store_true", help="实际写 registry+台账")
    au.add_argument("--enable", action="store_true", help="持久开启 tick 自动轮（写 mining_state）")
    au.add_argument("--disable", action="store_true", help="关闭 tick 自动轮（急停，回人审模式）")
    args = ap.parse_args(argv)
    if args.cmd == "scan":
        cmd_scan(args.root, args.out, float(args.alpha))
    elif args.cmd == "admit":
        cmd_admit(args.root, [h.strip() for h in args.hashes.split(",") if h.strip()],
                  yes=bool(args.yes), quota=int(args.quota))
    elif args.cmd == "evidence-refit":
        cmd_evidence_refit(args.root)
    elif args.cmd == "auto":
        cmd_auto(args.root, yes=bool(args.yes),
                 enable=bool(args.enable), disable=bool(args.disable))
    return 0


if __name__ == "__main__" and __package__ in (None, ""):
    # 直接脚本运行自举包上下文（python .../rescue_ops.py）
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from dsh_factor_mining.rescue_ops import main as _main  # noqa: E402
    raise SystemExit(_main())
elif __name__ == "__main__":
    raise SystemExit(main())
