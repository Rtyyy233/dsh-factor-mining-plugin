# coding=utf-8
"""D1 发散相多底并行——降级形态：跨 lane 模型混布（2026-10-14 任务书 §三 D1）。

宿主勘探结论（任务书 §1.3 第 4 点回填）：插件面只有对既有会话的
followup 注入，无创建会话/选底模接口——「一次发散阶段并行唤起多个
底模」不可编排。按任务书预案降级：**不同 lane 绑不同底模**（用户在
宿主侧配置），防多线涌向同一空箱靠覆盖地图的实时性（短 TTL 重算）。

本模块交付 D1 的机械件（全部可测试、判据可机械执行）：
- selection_overlap：两份偏好指纹（top-5 构造族集合）的 Jaccard——
  选型依据的计算器（阈值 ≤0.3 进配置；「价值来自偏好差异，不来自
  模型数量」的机械化身）；
- check_preconditions：开启三前提（普查已产出 / 偏好重叠足够低 /
  混布已就位——最后者是宿主侧事实，由 mining_state 配置键声明）；
- contrast：对照期判据执行器（多底臂 vs 对照臂：产出两两相似度中位
  数 + 新域命中率；任一不满足 → 自动关回）；
- 启停事件一律落 gate_audit.jsonl（与 B1 切档同款审计通道）。

开关 multi_model_divergence 默认 False；开启只改遥测与对照工具的
可用性——算力落点仍由各 lane 的引擎指令决定（零行为突变）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .filelock import state_write_lock
from .state import MINING_CONFIG, read_mining_state, _atomic_write_json, _path
from .navigation import (OP_NAMES, bin_key, coverage_bins, jaccard_overlap,
                         load_block_index, read_engine_trail)
from .ordergate import _audit_append

#: 普查产物约定路径（analysis 侧脚本产出；check_preconditions 消费）
CENSUS_PATHS = {
    "stock": "attractor_census_stock.json",
    "etf": "attractor_census_etf.json",
}
D1_STATE_KEY = "multi_model_divergence_state"


def selection_overlap(top5_a: list[str], top5_b: list[str]) -> float:
    """两候选底模偏好指纹（top-5 构造族）的 Jaccard 重合度。"""
    return jaccard_overlap(set(top5_a or []), set(top5_b or []))


def census_top5(census_json: dict) -> "list[str] | None":
    """从 attractor_census.py 产物取 top-5 构造族；缺字段 → None。"""
    t5 = census_json.get("top5_family_sets")
    if isinstance(t5, list) and t5:
        return [str(x) for x in t5]
    return None


def load_census(census_file: "str | Path") -> "dict | None":
    try:
        d = json.loads(Path(census_file).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def check_preconditions(state_root: "str | Path",
                        census_files: "list[str] | None" = None,
                        *, lanes_bound: "bool | None" = None) -> dict:
    """开启三前提核对（纯读；返回逐项结论 + 是否全部满足）。

    1. 两线普查产物已产出且可读（路径=mining_state 配置 d1_census_files
       覆盖 > 约定默认 stateRoot 同级 attractor_census_{stock,etf}.json）；
    2. 候选偏好重叠 ≤ 阈值（默认 0.3，配置 d1_overlap_max）；
    3. 跨 lane 模型混布已就位（宿主侧事实：mining_state 配置键
       multi_model_lanes_bound=true 由用户在绑定完成后声明——引擎
       无法自证宿主配置，声明制是唯一诚实通道）。
    """
    cfg = read_mining_state(state_root)
    th = float(cfg.get("d1_overlap_max",
                       MINING_CONFIG.get("d1_overlap_max", 0.3)))
    if census_files is None:
        configured = cfg.get("d1_census_files")
        census_files = ([str(f) for f in configured]
                        if isinstance(configured, list) and configured
                        else [str(Path(state_root).parent / f)
                              for f in CENSUS_PATHS.values()])
    files = census_files[:2]
    censuses = [load_census(f) for f in files]
    have_census = all(c is not None for c in censuses)
    overlap = None
    if have_census:
        t5a = census_top5(censuses[0])
        t5b = census_top5(censuses[1])
        if t5a is not None and t5b is not None:
            overlap = round(selection_overlap(t5a, t5b), 4)
    if lanes_bound is None:
        lanes_bound = bool(cfg.get("multi_model_lanes_bound", False))
    ok = {"census_ready": have_census,
          "overlap_ok": (overlap is not None and overlap <= th),
          "lanes_bound": lanes_bound}
    return {"ok": all(ok.values()), "checks": ok,
            "overlap": overlap, "overlap_threshold": th,
            "census_files": files[:2]}


def maybe_enable(state_root: "str | Path") -> dict:
    """三前提全满足 → 置 multi_model_divergence=true + 审计事件（幂等）。

    由运维脚本/后续 tick 调用；前提不满足时静默不动开关。
    """
    pre = check_preconditions(state_root)
    if not pre["ok"]:
        return {"enabled": False, "preconditions": pre}
    with state_write_lock(Path(state_root)):
        st = read_mining_state(state_root)
        if st.get("multi_model_divergence"):
            return {"enabled": True, "already": True, "preconditions": pre}
        g = st.get(D1_STATE_KEY) if isinstance(st.get(D1_STATE_KEY), dict) \
            else {}
        st[D1_STATE_KEY] = {**g,
                            "enabled_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        st["multi_model_divergence"] = True
        _atomic_write_json(_path("mining_state", state_root), st)
    _audit_append(state_root, "d1_enabled", {
        "preconditions": pre["checks"], "overlap": pre["overlap"],
        "form": "cross-lane-model-mixing(降级形态)",
        "contrast_rule": "4 周或每臂 ≥300 次提交：两两相似度中位数不高于"
                         "对照臂且新域命中不低于对照臂，任一不满足自动关回",
    })
    return {"enabled": True, "preconditions": pre}


def disable(state_root: "str | Path", reason: str, detail: "dict | None" = None):
    """对照期判据不满足/人工叫停 → 关回 + 审计事件（幂等）。"""
    with state_write_lock(Path(state_root)):
        st = read_mining_state(state_root)
        if not st.get("multi_model_divergence"):
            return
        st["multi_model_divergence"] = False
        _atomic_write_json(_path("mining_state", state_root), st)
    _audit_append(state_root, "d1_disabled", {"reason": reason,
                                              **(detail or {})})


# ---- 对照期判据执行器 ----
def _lane_submissions(engine_trail: list[dict], lane: str) -> list[dict]:
    return [e for e in engine_trail if e.get("lane") == lane
            and e.get("stage") in ("development", "batch")
            and e.get("origin") not in ("random", "collision")]


def _pairwise_similarity_median(block_index: dict, entries: list[dict],
                                cap: int = 60) -> "float | None":
    """臂内产出两两构造块相似度（Jaccard 重合度）中位数。

    模型-authored 试验（origin 非 random/collision）才算「产出」——
    骰子批的相似度度量的是生成器，不是底模偏好。
    """
    import random as _r
    from statistics import median
    if len(entries) < 2:
        return None
    es = _r.Random(11).sample(entries, min(cap, len(entries)))
    vals = []
    for i in range(len(es)):
        bi = block_index.get(str(es[i].get("source_hash") or ""))
        if not bi:
            continue
        for j in range(i + 1, len(es)):
            bj = block_index.get(str(es[j].get("source_hash") or ""))
            if not bj:
                continue
            vals.append(jaccard_overlap(bi, bj))
    return round(median(vals), 4) if vals else None


def _new_domain_hit_rate(block_index: dict, entries: list[dict],
                         baseline_bins: set) -> "float | None":
    """臂内试验落入（对照期开始前）未占用箱的比率（新域命中）。"""
    if not entries:
        return None
    hits = 0
    n = 0
    for e in entries:
        b = block_index.get(str(e.get("source_hash") or ""))
        if not b:
            continue
        n += 1
        if bin_key(b) not in baseline_bins:
            hits += 1
    return round(hits / n, 4) if n else None


def contrast(state_root: "str | Path", multi_lane: str,
             baseline_lane: str, *, baseline_cutoff_ts: "str | None" = None,
             engine_trail: "list[dict] | None" = None,
             block_index: "dict | None" = None) -> dict:
    """对照判据执行器：多底臂 vs 对照臂。

    判据（任务书 D1.3）：保留条件 = 多底臂产出两两相似度中位数 ≤ 对照臂
    且 新域命中 ≥ 对照臂；任一不满足 → 自动关回（disable + 事件）。
    baseline_bins = 对照期开始前（baseline_cutoff_ts 之前，缺省=全部
    历史）已占用箱集合——新域命中以此为准。样本不足（<2 或无块）时
    判据记 insufficient，不动开关（诚实不虚判）。
    """
    trail = engine_trail if engine_trail is not None \
        else read_engine_trail(state_root)
    bidx = block_index if block_index is not None \
        else load_block_index(state_root)
    cutoff = str(baseline_cutoff_ts or "")
    base_pool = [e for e in trail if not cutoff or str(e.get("ts") or "") <= cutoff]
    # 已占用箱按 cutoff 前的试验计（非全库源码——源码库可能含未评估草稿）
    baseline_bins = set()
    for e in base_pool:
        b = bidx.get(str(e.get("source_hash") or ""))
        if b:
            baseline_bins.add(bin_key(b))
    multi = _lane_submissions(trail, multi_lane)
    base = _lane_submissions(trail, baseline_lane)
    sim_m = _pairwise_similarity_median(bidx, multi)
    sim_b = _pairwise_similarity_median(bidx, base)
    hit_m = _new_domain_hit_rate(bidx, multi, baseline_bins)
    hit_b = _new_domain_hit_rate(bidx, base, baseline_bins)
    verdict = "insufficient"
    if None not in (sim_m, sim_b, hit_m, hit_b):
        verdict = ("keep" if (sim_m <= sim_b and hit_m >= hit_b)
                   else "shutdown")
    out = {"verdict": verdict, "multi_lane": multi_lane,
           "baseline_lane": baseline_lane,
           "n_multi": len(multi), "n_baseline": len(base),
           "pairwise_sim_median": {"multi": sim_m, "baseline": sim_b},
           "new_domain_hit_rate": {"multi": hit_m, "baseline": hit_b},
           "baseline_bins": len(baseline_bins)}
    if verdict == "shutdown":
        disable(state_root, "contrast-failed", out)
    elif verdict == "keep":
        _audit_append(state_root, "d1_contrast_keep", out)
    return out


def d1_status(state_root: "str | Path") -> dict:
    st = read_mining_state(state_root)
    g = st.get(D1_STATE_KEY) if isinstance(st.get(D1_STATE_KEY), dict) else {}
    return {"enabled": bool(st.get("multi_model_divergence")),
            "since": g.get("enabled_at"),
            "preconditions": check_preconditions(state_root)["checks"]}
