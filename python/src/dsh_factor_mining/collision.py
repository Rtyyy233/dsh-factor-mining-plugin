# coding=utf-8
"""概念碰撞采样器（2026-09-22 灵感源二代 WS-2C，PLAN-inspiration-mechanism）。

职责边界（用户四轮纠偏定形，红线级别）：
- 骰子选内容（内容盲=独立采样），模型管意义（连接/变形/弃权）——
  合法性与 factor_random_generate 的随机表达式生成同一等级；
- 词表零人工策划：从 journal 全语料自举（含墓志与失败——死方向也是
  邻近可能的合法素材），发散阀抽 papers 台账标题。人工金融概念清单
  = 轴库换皮，禁止；
- 卡是遥测不是指令：弃权免费；碰撞喂推导层不做独立搜索（farm 312
  随机 0 入册教训——骰子递火柴，模型点火）。

日志链：collision_log.jsonl 记每次抽卡（seed/刺激/线）；回应与结局
不经本模块——模型在 trail 条目写 collision_card 字段（card_id）即可
让引擎 trail 标 origin=collision，归因由 card_id 关联分析。
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,6}|[a-zA-Z_]{3,}")
#: 词条入表最低出现次数（过犹不及：=1 的错切分噪声占词表大头）
MIN_WORD_COUNT = 5
#: 上下文锚截断（防语义浆糊：每词携带它在语料里的实际用法一行）
_CONTEXT_CHARS = 96
#: 日志尾部扫描上限（origin 的 card_id 校验；全文件扫描不必要）
_LOG_TAIL = 500


def _stable_hash(s: str) -> int:
    import hashlib
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)


def _iter_journal_texts(state_root: str | Path):
    """journal 全语料：所有线的 live + raw 快照 + arcs 归档（含墓志——
    失败语料是合法素材，这是与"只学成功"的引力对抗的结构性设计）。"""
    base = Path(state_root) / "journal"
    if not base.exists():
        return
    for lane_dir in sorted(base.iterdir()):
        if not lane_dir.is_dir():
            continue
        candidates = [lane_dir / "live.md", *sorted((lane_dir / "raw").glob("*.md")),
                      *sorted((lane_dir / "arcs").glob("*.md"))]
        for f in candidates:
            try:
                yield f.read_text(encoding="utf-8")
            except Exception:
                continue


def build_vocabulary(state_root: str | Path,
                     min_count: int = MIN_WORD_COUNT) -> "dict[str, dict]":
    """词表自举：journal 语料分词计数 → {word: {count, context}}。

    context = 首个含该词的非标题行片段（出处锚，随卡携带）。"""
    counts: "dict[str, int]" = {}
    context: "dict[str, str]" = {}
    for txt in _iter_journal_texts(state_root):
        for raw_line in txt.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            toks = set(_TOKEN_RE.findall(line))
            if not toks:
                continue
            snippet = line[:_CONTEXT_CHARS]
            for w in toks:
                counts[w] = counts.get(w, 0) + 1
                if w not in context:
                    context[w] = snippet
    return {w: {"count": c, "context": context.get(w, "")}
            for w, c in counts.items() if c >= min_count}


def _paper_stimuli(state_root: str | Path,
                   rng: random.Random) -> "dict | None":
    """发散阀：papers 台账随机一条 {text: 标题, source: "papers:<id>"}。
    台账空/损坏 → None（回落纯词表卡）。"""
    try:
        d = json.loads((Path(state_root) / "papers.json").read_text(
            encoding="utf-8"))
        papers = d.get("papers") if isinstance(d, dict) else None
        if not isinstance(papers, dict) or not papers:
            return None
        pid = rng.choice(sorted(papers.keys()))
        meta = papers.get(pid) or {}
        title = str(meta.get("title") or "").strip()
        if not title:
            return None
        return {"text": title[:160], "origin": f"papers:{pid}",
                "context": f"papers 台账标题（first_seen="
                           f"{meta.get('first_seen', '?')}）"}
    except Exception:
        return None


def draw_card(state_root: str | Path, seed: int | None = None, k: int = 2,
              use_papers: bool = True) -> "dict[str, Any]":
    """抽一张概念刺激卡。

    seed 显式 → 完全可复现（同 seed 同卡，card_id 稳定）；缺省 → 运行时
    熵（time+urandom 派生），seed_used 随卡记录（可复现原则与 explore
    的 seed_used 同款）。构成：35% 概率 1 论文标题 × 1 词表概念（跨源
    碰撞，先验尾部最大熵入口），其余 k 个词表概念两两碰撞。"""
    seed_used = int(seed) if seed is not None else (
        time.time_ns() ^ random.SystemRandom().getrandbits(48))
    rng = random.Random(seed_used)
    vocab = build_vocabulary(state_root)
    words = sorted(vocab.keys())
    if not words:
        return {"error": "词表为空（journal 语料不足）——先正常挖几轮再抽卡",
                "seed_used": seed_used}
    stimuli: "list[dict]" = []
    paper = _paper_stimuli(state_root, rng) if use_papers else None
    if paper is not None:
        stimuli.append(paper)
        k = max(1, k - 1)
    for w in rng.sample(words, min(max(1, k), len(words))):
        stimuli.append({"text": w, "origin": "journal-vocab",
                        "context": vocab[w]["context"]})
    rng.shuffle(stimuli)
    card_id = "cc-" + f"{_stable_hash(json.dumps(stimuli, ensure_ascii=False) + '|' + str(seed_used)):010d}"
    # 构造禁忌负约束素材（2026-10-14 任务书 C3 接线三通道之一）：卡是
    # 发散工具——负约束随卡可见（纯名词零分数，预算独立配置）。渲染
    # 失败静默降级（卡本身不受素材通道故障牵连）。
    taboo_note = ""
    try:
        from . import navigation as _nav
        from .state import MINING_CONFIG, read_mining_state
        _ms = read_mining_state(state_root)
        if _ms.get("navigation_materials",
                   MINING_CONFIG.get("navigation_materials", True)):
            taboo_note = _nav.taboo_material_block(
                state_root,
                int(_ms.get("navigation_card_budget_chars",
                            MINING_CONFIG.get("navigation_card_budget_chars",
                                              320))),
                cfg={**MINING_CONFIG, **_ms})
    except Exception:
        taboo_note = ""
    note = ("这是一张随机概念刺激卡（内容盲抽样，不是方向指令）。"
            "连接（说清刺激→本面板收益的机制，落成 [灵感重置:mechanism] "
            "假设）、变形（扭动某个卡住的方向）或弃权都行——弃权免费，"
            "再抽即可。若连接后入账，trail 条目写 collision_card 字段"
            "（本卡 card_id），试验将标 origin=collision。")
    if taboo_note:
        note = note + "\n" + taboo_note
    card = {
        "card_id": card_id,
        "seed_used": seed_used,
        "drawn_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stimuli": stimuli,
        "note": note,
    }
    _append_log(state_root, card)
    return card


def _append_log(state_root: str | Path, card: dict) -> None:
    """抽卡日志（collision_log.jsonl；写失败静默——卡是遥测不是事务）。"""
    try:
        p = Path(state_root) / "collision_log.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": card.get("drawn_at"), "card_id": card.get("card_id"),
                "seed_used": card.get("seed_used"),
                "stimuli": card.get("stimuli"),
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def known_card_ids(state_root: str | Path, tail: int = _LOG_TAIL) -> "set[str]":
    """日志尾部 card_id 集合（origin=collision 的校验数据源；
    文件缺失/损坏 → 空集，origin 校验静默回落）。"""
    try:
        lines = (Path(state_root) / "collision_log.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        out = set()
        for ln in lines[-tail:]:
            try:
                cid = json.loads(ln).get("card_id")
                if isinstance(cid, str):
                    out.add(cid)
            except Exception:
                continue
        return out
    except Exception:
        return set()
