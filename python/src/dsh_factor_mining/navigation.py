# coding=utf-8
"""推导优先 + 导航层（2026-10-14 任务书 PLAN-derivation-first-and-navigation）。

本模块是 A1/B2/C1/C2/C3/C4 六个工作项的机械底座。红线：一切计算限于
词法/字符串集合层（正则、集合运算、IC 序列相关的既有数学复用）——
零语义解析，零门禁判定（B1 门在 ordergate.py；统计门链一行不动）。

词汇表（术语表「构造块」的机械定义）：
- 算子名：OPERATOR_REGISTRY 全部函数名（6 族：ts/cs/binary/unary/cmp/cond）
- 叶子字段：open/high/low/close/volume/amount（源码侧 env.o→open 等短名
  归一到全名；文本侧只匹配全名与 vol/amt 别名——单字母在散文里是噪声）

素材格式红线：纯名词、零分数（无小数/IC/名次）、零构造方向指令、
零历史引用；占用密度以整数试验数呈现（分配事实），不出现日期。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .factor.ops import OPERATOR_REGISTRY

# ---- 构造块词表（A1） ----
OP_NAMES: dict[str, str] = {}          # 算子名 → 族
for _fam, _ops in OPERATOR_REGISTRY.items():
    for _name in _ops:
        OP_NAMES[_name] = _fam
OP_RE = re.compile(r"\b(" + "|".join(sorted(OP_NAMES, key=len, reverse=True))
                   + r")\b")

# 叶子归一：源码访问形态与文本词形 → 规范名
_LEAF_CANON = {
    "o": "open", "h": "high", "l": "low", "c": "close",
    "v": "volume", "vol": "volume", "amount": "amount", "amt": "amount",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "volume": "volume", "turnover": "amount",
}
_ENV_ACCESS_RE = re.compile(r"\benv\.([a-zA-Z_][a-zA-Z0-9_]*)")
_LEAF_WORD_RE = re.compile(
    r"\b(open|high|low|close|volume|amount|vol|amt|turnover)\b")

# 素材面禁数字：小数（评估分数形态）不得出现在素材里
_SCORE_NUM_RE = re.compile(r"\d+\.\d+")

LEAF_NAMES = ("open", "high", "low", "close", "volume", "amount")
FAMILIES = tuple(sorted(OPERATOR_REGISTRY.keys()))


def extract_blocks(source_or_text: str, *, code: bool = False) -> set[str]:
    """构造块提取（纯词法）。

    code=True（因子源码）：算子调用名 + env.<leaf> 访问（归一到全名）。
    code=False（journal 推导正文）：算子名 + 叶子全名/别名词。
    任何一侧空文本 → 空集（距离层记 null 不记 0）。
    """
    text = str(source_or_text or "")
    if not text.strip():
        return set()
    blocks = set(OP_RE.findall(text))
    if code:
        for m in _ENV_ACCESS_RE.finditer(text):
            leaf = _LEAF_CANON.get(m.group(1).lower())
            if leaf:
                blocks.add(leaf)
    else:
        for m in _LEAF_WORD_RE.finditer(text.lower()):
            blocks.add(_LEAF_CANON[m.group(1).lower()])
    return blocks


def jaccard_distance(a: set[str], b: set[str]) -> "float | None":
    """Jaccard 距离 1 − |∩|/|∪|；任一侧空集 → None（不可比，不记 0）。"""
    if not a or not b:
        return None
    return 1.0 - len(a & b) / len(a | b)


def jaccard_overlap(a: set[str], b: set[str]) -> float:
    """Jaccard 重合度（B2 取材过滤用；双空集约定 1.0=完全重合）。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---- A1 第一反应段（词法约定，零语义） ----
_FIRST_REACTION_MARK_RE = re.compile(r"第一反应|第一念头|不假思索")
_META_PREFIXES = ("ref:", "ts:", "> [update ")


def _entry_paragraphs(entry_text: str) -> list[str]:
    """条目正文 → 段落列表（跳头行/ref/ts/note/标题行；空行分段）。"""
    lines = str(entry_text or "").splitlines()
    if lines and lines[0].startswith("## "):
        lines = lines[1:]          # 头行不是正文
    paras: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(_META_PREFIXES) or s.startswith("#"):
            continue
        if not s:
            if cur:
                paras.append(cur)
                cur = []
            continue
        cur.append(s)
    if cur:
        paras.append(cur)
    return ["\n".join(p) for p in paras]


def first_reaction_segment(entry_text: str) -> "str | None":
    """条目正文的第一反应段。

    优先显式标记段（含「第一反应/第一念头/不假思索」的首个段落）；
    无标记时回落约定=正文首段（写作纪律「动笔先写第一念头」的机械
    近似——历史日志无显式标记段，首段即最接近的代理）。无正文 → None。
    """
    paras = _entry_paragraphs(entry_text)
    if not paras:
        return None
    for p in paras:
        if _FIRST_REACTION_MARK_RE.search(p):
            return p
    return paras[0]


def escape_distance(entry_text: str, final_source: str) -> "float | None":
    """A1 逃逸距离：第一反应段构造块 vs 最终源码构造块的 Jaccard 距离。

    无第一反应段 / 任一侧空集 → None（不可比）。只做遥测，不进门禁。
    """
    seg = first_reaction_segment(entry_text)
    if seg is None:
        return None
    return jaccard_distance(extract_blocks(seg),
                            extract_blocks(final_source, code=True))


# ---- B2 不相似示例选择（取材过滤器） ----
def dissimilar_filter(candidates: list[dict], recent_sources: list[str],
                      k: int = 10, prune: float = 0.5) -> list[dict]:
    """剔除与最近 k 次提交构造块重合度最高的前 prune 比例候选。

    candidates: [{"source": 源码, ...原序}]；recent_sources: 最近提交源码
    列表（新→旧，取前 k）。重合度 = 候选块集对每个近期提交块集的
    Jaccard 最大值。剔除后保持候选原序。空候选/空近期 → 原样返回。
    """
    if not candidates or not recent_sources or prune <= 0:
        return list(candidates)
    rec_blocks = [b for b in (extract_blocks(s, code=True)
                              for s in recent_sources[:max(0, int(k))]) if b]
    if not rec_blocks:
        return list(candidates)
    scored = []
    for i, c in enumerate(candidates):
        cb = extract_blocks(str(c.get("source") or ""), code=True)
        ov = max((jaccard_overlap(cb, rb) for rb in rec_blocks), default=0.0)
        scored.append((ov, i))
    scored.sort()                          # 重合度升序=最不相似在前；序号稳定
    keep = len(scored) - int(len(scored) * min(prune, 1.0))
    kept_idx = {i for _, i in scored[:max(0, keep)]}
    return [c for i, c in enumerate(candidates) if i in kept_idx]


# ---- 状态根读取（C 系列数据源） ----
def iter_sources(state_root: "str | Path") -> Iterable[tuple[str, str]]:
    """(source_hash, 源码) 全库迭代；sources/ 缺失 → 空。"""
    base = Path(state_root) / "sources"
    if not base.exists():
        return
    for shard in base.iterdir():
        if not shard.is_dir():
            continue
        for f in shard.glob("*.py"):
            try:
                yield f.stem, f.read_text(encoding="utf-8")
            except Exception:
                continue


def read_engine_trail(state_root: "str | Path") -> list[dict]:
    p = Path(state_root) / "trail_engine.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def load_block_index(state_root: "str | Path",
                     engine_trail: "list[dict] | None" = None
                     ) -> "dict[str, set[str]]":
    """source_hash → 构造块集合（单次源码库扫描，C 系列共用）。"""
    return {h: extract_blocks(s, code=True)
            for h, s in iter_sources(state_root)}


# ---- C1 覆盖地图 ----
def bin_key(blocks: set[str]) -> tuple[str, str]:
    """分箱键 =（字段集合 × 出现的算子族集合）。纯名词、零分数。"""
    fields = sorted(b for b in blocks if b in LEAF_NAMES)
    fams = sorted({OP_NAMES[b] for b in blocks if b in OP_NAMES})
    return "+".join(fields) or "(无字段)", "+".join(fams) or "(无算子)"


def coverage_bins(block_index: dict[str, set[str]],
                  engine_trail: list[dict]) -> list[dict]:
    """按（字段集 × 算子族）给历史试验分箱；每箱占用密度+新近度。

    试验→源码→构造块（词法）；无源码可读的试验跳过（诚实计数）。
    返回按（密度降序，新近度降序）排序的箱列表。
    """
    bins: dict[tuple[str, str], dict] = {}
    for e in engine_trail:
        h = e.get("source_hash")
        blocks = block_index.get(str(h or "")) if h else None
        if not blocks:
            continue
        bk = bin_key(blocks)
        b = bins.setdefault(bk, {"fields": bk[0], "families": bk[1],
                                 "density": 0, "last_ts": ""})
        b["density"] += 1
        if str(e.get("ts") or "") > b["last_ts"]:
            b["last_ts"] = str(e.get("ts") or "")
    return sorted(bins.values(),
                  key=lambda b: (-b["density"], b["last_ts"]))


def bin_entries(block_index: dict[str, set[str]],
                engine_trail: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """箱键 → 该箱的试验条目（C2 档位判据的输入）。"""
    out: dict[tuple[str, str], list[dict]] = {}
    for e in engine_trail:
        h = e.get("source_hash")
        blocks = block_index.get(str(h or "")) if h else None
        if not blocks:
            continue
        out.setdefault(bin_key(blocks), []).append(e)
    return out


# ---- C2 域级采尽读数（三档：前缘/边际衰减/采尽） ----
def _sketch(e: dict) -> "list[float] | None":
    s = e.get("ic_series_sketch")
    return list(s) if isinstance(s, (list, tuple)) and len(s) >= 5 else None


def _corr(a, b) -> "float | None":
    try:
        from .factor.evaluate import _tail_aligned_corr
        return _tail_aligned_corr(a, b)
    except Exception:
        return None


def _mean_pairwise_abs_corr(entries: list[dict],
                            other: "list[dict] | None" = None,
                            cap: int = 20, seed: int = 7) -> "float | None":
    """箱内两两平均 |ρ|（other=None）或箱与池外平均 |ρ|。

    复用族链判定的 IC 序列相关数学（同源既有实现）；样本截幅 cap 防
    大箱平方爆炸；None=样本不足（档位判据按前缘处理，不冒充采尽）。
    """
    import random as _r
    rng = _r.Random(seed)
    if other is None:
        if len(entries) < 2:
            return None
        es = rng.sample(entries, min(cap, len(entries)))
        vals = []
        for i in range(len(es)):
            for j in range(i + 1, len(es)):
                sa, sb = _sketch(es[i]), _sketch(es[j])
                if sa is None or sb is None:
                    continue
                c = _corr(sa, sb)
                if c is not None:
                    vals.append(abs(c))
        return (sum(vals) / len(vals)) if len(vals) >= 3 else None
    if not entries or not other:
        return None
    es = rng.sample(entries, min(cap, len(entries)))
    eos = rng.sample(other, min(cap, len(other)))
    vals = []
    for a in es:
        sa = _sketch(a)
        if sa is None:
            continue
        for b in eos:
            sb = _sketch(b)
            if sb is None:
                continue
            c = _corr(sa, sb)
            if c is not None:
                vals.append(abs(c))
    return (sum(vals) / len(vals)) if len(vals) >= 3 else None


def _recency_days(last_ts: str, now: "float | None" = None) -> "float | None":
    if not last_ts:
        return None
    try:
        import datetime as _dt
        t = _dt.datetime.fromisoformat(str(last_ts)).timestamp()
        return max(0.0, (now or time.time()) - t) / 86400.0
    except Exception:
        return None


def exhaustion_tier(bin_es: list[dict], outside_es: list[dict],
                    *, th_int: float = 0.6, th_int_marginal: float = 0.4,
                    th_ext: float = 0.5, min_density: int = 10,
                    recency_days_threshold: float = 14.0,
                    now: "float | None" = None) -> str:
    """三档标签（前缘/边际衰减/采尽）——只给档位不给数字（既有裁决：
    分配事实可以给，数字目标不可以给）。

    判据（阈值全部进配置，可对历史数据回测校准）：
    - 采尽：箱内冗余 ≥ th_int，或（与池外重叠 ≥ th_ext 且密度 ≥ min_density）
    - 边际衰减：箱内冗余 ≥ th_int_marginal，或与池外重叠 ≥ th_ext，
      或密度 ≥ min_density 且新近度超窗（久无新试验）
    - 其余：前缘（含样本不足——不冒充采尽）
    """
    if len(bin_es) >= 2:
        intra = _mean_pairwise_abs_corr(bin_es)
        if intra is not None and intra >= th_int:
            return "采尽"
        if intra is not None and intra >= th_int_marginal:
            return "边际衰减"
    if len(bin_es) >= 3:
        inter = _mean_pairwise_abs_corr(bin_es, outside_es)
        if inter is not None and inter >= th_ext:
            return "采尽" if len(bin_es) >= min_density else "边际衰减"
    last_ts = max((str(e.get("ts") or "") for e in bin_es), default="")
    rd = _recency_days(last_ts, now)
    if len(bin_es) >= min_density and rd is not None \
            and rd > recency_days_threshold:
        return "边际衰减"
    return "前缘"


# ---- C3 构造禁忌表 / C4 吸引子普查（同一挖掘的两产物） ----
def pattern_of(blocks: set[str]) -> tuple:
    """参数抽象模式 = 构造块集合本身（丢窗口/阈值数值，留算子名骨架+
    字段集——表达式里的 ts_mean[5] 与 ts_mean[20] 同模式）。"""
    return tuple(sorted(blocks))


def mine_patterns(block_index: dict[str, set[str]],
                  engine_trail: list[dict],
                  *, min_support: int = 12, top_k: int = 8,
                  pair_patterns: bool = True) -> dict:
    """频繁构造模式挖掘：整集模式 top-k + 高频成对共现。

    随机树与手写代码同 funnel（源码词法提取）；支持度 = 出现试验数
    （同 hash 多 horizon 只计一次）。返回 {"sets","pairs","n_*"}。
    """
    seen: set[tuple] = set()
    set_counts: dict[tuple, int] = {}
    pair_counts: dict[tuple, int] = {}
    n_used = 0
    for e in engine_trail:
        h = str(e.get("source_hash") or "")
        blocks = block_index.get(h)
        if not blocks:
            continue
        pat = pattern_of(blocks)
        key = (h, pat)
        if key in seen:
            continue
        seen.add(key)
        n_used += 1
        set_counts[pat] = set_counts.get(pat, 0) + 1
        if pair_patterns:
            names = sorted(pat)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    pk = (names[i], names[j])
                    pair_counts[pk] = pair_counts.get(pk, 0) + 1
    sets = sorted(set_counts.items(), key=lambda x: (-x[1], x[0]))
    pairs = sorted(pair_counts.items(), key=lambda x: (-x[1], x[0]))
    return {"sets": [s for s in sets if s[1] >= min_support][:top_k],
            "pairs": [p for p in pairs if p[1] >= min_support][:top_k],
            "n_patterns": len(set_counts), "n_trials": n_used}


def attractor_census(block_index: dict[str, set[str]],
                     engine_trail: list[dict],
                     *, bucket_days: int = 14) -> dict:
    """C4 吸引子普查：构造族频率的时间序列 + 集中度走势。

    族 = 主导算子族（块集中占比最高的族；并列取字母序稳定）；桶 =
    bucket_days 天。集中度 = top-3 模式份额（频率比，非评估分数）。
    """
    import datetime as _dt
    epochs: dict[int, dict] = {}
    all_counts: dict[tuple, int] = {}
    fam_counts: dict[str, int] = {}
    seen: set[tuple] = set()
    n_used = 0
    for e in engine_trail:
        h = str(e.get("source_hash") or "")
        blocks = block_index.get(h)
        if not blocks:
            continue
        pat = pattern_of(blocks)
        key = (h, pat)
        if key in seen:
            continue
        seen.add(key)
        n_used += 1
        all_counts[pat] = all_counts.get(pat, 0) + 1
        fams: dict[str, int] = {}
        for x in blocks:
            if x in OP_NAMES:
                fams[OP_NAMES[x]] = fams.get(OP_NAMES[x], 0) + 1
        dom = max(sorted(fams), key=lambda f: fams[f]) if fams else "none"
        fam_counts[dom] = fam_counts.get(dom, 0) + 1
        ts = str(e.get("ts") or "")
        try:
            d = _dt.datetime.fromisoformat(ts)
            bkt = int(d.timestamp() // (bucket_days * 86400))
        except Exception:
            bkt = -1
        ep = epochs.setdefault(bkt, {"patterns": {}, "families": {}})
        ep["patterns"][pat] = ep["patterns"].get(pat, 0) + 1
        ep["families"][dom] = ep["families"].get(dom, 0) + 1
    buckets = []
    for k in sorted(epochs):
        ep = epochs[k]
        pats = sorted(ep["patterns"].items(), key=lambda x: (-x[1], x[0]))
        total = sum(c for _, c in pats) or 1
        buckets.append({
            "epoch": k,
            "n": total,
            "top_patterns": [{"pattern": list(p), "count": c}
                             for p, c in pats[:5]],
            "top3_share": round(sum(c for _, c in pats[:3]) / total, 3),
            "families": dict(sorted(ep["families"].items(),
                                    key=lambda x: -x[1])),
        })
    overall = sorted(all_counts.items(), key=lambda x: (-x[1], x[0]))
    return {
        "n_trials_used": n_used,
        "top_patterns": [{"pattern": list(p), "count": c}
                         for p, c in overall[:10]],
        "family_totals": dict(sorted(fam_counts.items(), key=lambda x: -x[1])),
        "buckets": buckets,
        "bucket_days": bucket_days,
        "top5_family_sets": _top5_families(all_counts),
    }


def _top5_families(all_counts: dict[tuple, int]) -> list[str]:
    """D1 选型依据：最高频算子族 top-5（偏好指纹，供 Jaccard 对比）。"""
    fam_tot: dict[str, int] = {}
    for pat, c in all_counts.items():
        for x in pat:
            if x in OP_NAMES:
                fam_tot[OP_NAMES[x]] = fam_tot.get(OP_NAMES[x], 0) + c
    return [f for f, _ in sorted(fam_tot.items(),
                                 key=lambda x: (-x[1], x[0]))[:5]]


# ---- C 系列素材渲染（纯名词、零分数、预算截断） ----
def navigation_material_block(state_root: "str | Path",
                              budget_chars: int = 1200,
                              *, cfg: "dict | None" = None,
                              engine_trail: "list[dict] | None" = None,
                              block_index: "dict[str, set[str]] | None" = None,
                              cache_ttl_s: "float | None" = None) -> str:
    """导航素材块：覆盖地图（空箱优先）+ 采尽档位 + 构造禁忌。

    红线自检：渲染结果不得含小数（评估分数形态）——命中即抛（模板或
    词表坏了，宁可无素材不可漏分数）。超预算按段优先级截断（空箱段
    最优先）。短 TTL 缓存——一线填箱后他线下次注入即见。
    """
    cfg = cfg or {}
    ttl = (cache_ttl_s if cache_ttl_s is not None
           else float(cfg.get("navigation_cache_ttl_s", 120)))
    key = str(Path(state_root).resolve())
    cached = _CACHE.get(key)
    if cached is not None and time.time() - cached[0] < max(0.0, ttl):
        parts = cached[1]
    else:
        trail = engine_trail if engine_trail is not None \
            else read_engine_trail(state_root)
        bidx = block_index if block_index is not None \
            else load_block_index(state_root)
        parts = _compute_material_parts(state_root, trail, bidx, cfg)
        _CACHE[key] = (time.time(), parts)
    text = _render_material_parts(parts, max(0, int(budget_chars)))
    hit = _SCORE_NUM_RE.search(text)
    if hit:
        raise ValueError(f"导航素材含数字形态 {hit.group(0)!r}——渲染红线")
    return text


_CACHE: dict[str, tuple[float, list[str]]] = {}


def taboo_material_block(state_root: "str | Path", budget_chars: int = 320,
                         *, cfg: "dict | None" = None,
                         cache_ttl_s: "float | None" = None) -> str:
    """灵感卡通道的负约束素材（C3 子集）：仅构造禁忌段。

    与 navigation_material_block 共用 TTL 缓存（同一次挖掘产物，两通道
    不同预算截取）。小数红线自检同款。
    """
    cfg = cfg or {}
    ttl = (cache_ttl_s if cache_ttl_s is not None
           else float(cfg.get("navigation_cache_ttl_s", 120)))
    key = str(Path(state_root).resolve())
    cached = _CACHE.get(key)
    if cached is None or time.time() - cached[0] >= max(0.0, ttl):
        trail = read_engine_trail(state_root)
        bidx = load_block_index(state_root)
        _CACHE[key] = (time.time(),
                       _compute_material_parts(state_root, trail, bidx, cfg))
        cached = _CACHE[key]
    taboo = [p for p in cached[1] if p.startswith("构造禁忌")]
    text = _render_material_parts(taboo, max(0, int(budget_chars)))
    hit = _SCORE_NUM_RE.search(text)
    if hit:
        raise ValueError(f"导航素材含数字形态 {hit.group(0)!r}——渲染红线")
    return text


def _compute_material_parts(state_root: "str | Path", trail: list[dict],
                            bidx: dict[str, set[str]], cfg: dict) -> list[str]:
    parts: list[str] = []
    body: list[str] = []
    # C1 地图：空箱（可见字段×可见族的未占用组合）优先，然后高占用箱
    try:
        bins = coverage_bins(bidx, trail)
    except Exception:
        bins = []
    if bins:
        fams_seen = sorted({f for b in bins
                            for f in b["families"].split("+")
                            if not f.startswith("(")})
        flds_seen = sorted({f for b in bins
                            for f in b["fields"].split("+")
                            if not f.startswith("(")})
        occupied = {(b["fields"], b["families"]) for b in bins}
        empty = [f"{f}×{g}" for f in flds_seen for g in fams_seen
                 if (f, g) not in occupied]
        if empty:
            body.append("未占用构造域（字段×算子族）：" + "、".join(empty[:12]))
        top = "、".join(f"{b['fields']}×{b['families']}" for b in bins[:6])
        if top:
            body.append("高占用构造域（已多试）：" + top)
    # C2 采尽档位（只档位无数字）
    try:
        if bins and len(trail) > 20:
            grouped = bin_entries(bidx, trail)
            labels = []
            for bk, es in sorted(grouped.items(),
                                 key=lambda x: -len(x[1]))[:8]:
                outside = [e for e in trail if e not in es]
                tier = exhaustion_tier(
                    es, outside,
                    th_int=float(cfg.get("exhaustion_th_int", 0.6)),
                    th_int_marginal=float(
                        cfg.get("exhaustion_th_int_marginal", 0.4)),
                    th_ext=float(cfg.get("exhaustion_th_ext", 0.5)),
                    min_density=int(cfg.get("exhaustion_min_density", 10)),
                    recency_days_threshold=float(
                        cfg.get("exhaustion_recency_days", 14.0)))
                if tier != "前缘":
                    labels.append(f"{bk[0]}×{bk[1]}={tier}")
            if labels:
                body.append("域级采尽读数（档位）：" + "、".join(labels[:8]))
    except Exception:
        pass
    # C3 禁忌表（负约束素材：模式抽象形式，不附成绩）
    try:
        mined = mine_patterns(
            bidx, trail,
            min_support=int(cfg.get("taboo_min_support", 12)),
            top_k=int(cfg.get("taboo_top_k", 8)))
        if mined["sets"]:
            pats = "、".join("+".join(p) for p, _ in mined["sets"][:6])
            body.append("构造禁忌（下列模式已被大量使用，"
                        "不得再作为提交主体）：" + pats)
    except Exception:
        pass
    if not body:
        return []
    return (["导航素材（名词性参考，不含评分，不是方向指令）："] + body)


def _render_material_parts(parts: list[str], budget_chars: int) -> str:
    if not parts:
        return ""
    out: list[str] = []
    used = 0
    for p in parts:
        if used + len(p) > budget_chars:
            break
        out.append(p)
        used += len(p) + 1
    return "\n".join(out)
