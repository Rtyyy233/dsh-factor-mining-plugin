"""推理日志(journal)— 构造面 v7/v7.2 设计(2026-09-12,RESEARCH-llm-thinking-space §3.5)。

双层记忆的解释层:trail_engine 是引擎的事实账本(不可变、权威),journal
是模型自有的推理轨迹(可变、可弃)。引擎在本模块里只做机械仓库管理员:
验证首行约定、保管快照、重建索引、按 source_hash 关联镜像——零语义
解析,不读懂内容,数字一律引用 trail 而非复制。

信息架构(SKILL 同构渐进披露,注入面与日志年龄无关):
  journal/{lane}/live.md              L1 模型追加/蒸馏重写(预算 ~4000 字符)
  journal/{lane}/INDEX.md             L0 机械重建(live 头行 + 墓志尾行)
  journal/{lane}/arcs/arc-N.md        L2 蒸馏下沉的已结条目全文(仅 pull)
  journal/{lane}/arcs/cold-index.md   墓志行(每条一行,永不注入全量)
  journal/{lane}/raw/                 L3 蒸馏前快照原样(写只审计,防洗账)
  journal/{lane}/stats.json           遥测计数(登记率/蒸馏次数)

条目首行约定(引擎只解析这一行,内容零模板):
  ## <id> <status> <标题>
  ref:<source_hash>                    # registered 状态必带(镜像关联键)

status 状态机(open→registered→adjudicated→resolved|stale;belief 族 active→retired):
  迁移经 journal.update;同状态更新(补 note)对全部状态合法。
"""

from __future__ import annotations

import json
import os
import re
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .filelock import state_write_lock
from .lane import DEFAULT_LANE, current_lane

# ---- 约定常量 ----
_HEADER_RE = re.compile(
    r"^## (?P<id>[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}) "
    r"(?P<status>open|registered|adjudicated|resolved|stale|active|retired|lesson) "
    r"(?P<title>\S.*)$")
_REF_RE = re.compile(r"^ref:(?P<h>[0-9a-fA-F]{8,64})\s*$", re.M)
_TS_RE = re.compile(r"^ts:(?P<t>\d{4}-\d{2}-\d{2}T[0-9:.+-]+)\s*$", re.M)
_STATUSES = ("open", "registered", "adjudicated", "resolved", "stale",
             "active", "retired", "lesson")
# 出生即终态(resolved/adjudicated/retired)非法——必须走迁移
_BORN_OK = {"open", "registered", "active", "lesson"}
# 合法迁移(同状态补 note 由 update 单独放行,不在此表)
_TRANSITIONS = {
    ("open", "registered"), ("open", "stale"),
    ("registered", "adjudicated"), ("registered", "stale"),
    ("adjudicated", "resolved"), ("adjudicated", "stale"),
    ("active", "retired"),
}
_ACTIVE = ("open", "registered", "adjudicated", "active")
LIVE_CHAR_CAP = 4000          # ≈2k token(CJK 为主);超出=警告+蒸馏压力信号
EPITAPH_TAIL = 40             # INDEX 里墓志只带尾部 N 行
_NOTE_PREFIX = "> [update "   # 引擎插入的 note 行前缀(与模型正文区分)


class JournalError(ValueError):
    """约定违规(首行不合法/ID 重复/非法迁移)——映射 -32602。"""


# ---- 路径与条目解析 ----
def _lane_dir(root: str | Path, lane: str | None) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(lane or DEFAULT_LANE)) or DEFAULT_LANE
    return Path(root) / "journal" / safe


def _live_path(d: Path) -> Path:
    return d / "live.md"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def parse_header(line: str) -> dict | None:
    m = _HEADER_RE.match(line.strip())
    return {"id": m.group("id"), "status": m.group("status"),
            "title": m.group("title").strip()} if m else None


def split_entries(text: str) -> list[dict]:
    """live/arcs 全文 → 条目列表(header 解析 + 正文块)。

    首个 `## ` 头行之前的内容(蒸馏后的信念区 preamble)不构成条目。
    非约定格式的 `## ` 行按原文保留在块里但记 unparsed=True——引擎不
    读懂它,但 INDEX/蒸馏不会把它当成可管理的条目。
    """
    out: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            h = parse_header(line)
            if cur is not None:
                out.append(cur)
            cur = {"header": line.rstrip(), "parsed": h,
                   "lines": [line.rstrip()]}
        elif cur is not None:
            cur["lines"].append(line.rstrip())
    if cur is not None:
        out.append(cur)
    for e in out:
        body = "\n".join(e["lines"][1:])
        e["text"] = "\n".join(e["lines"]).rstrip() + "\n"
        e["ref"] = (m.group("h").lower()
                    if e["parsed"] and (m := _REF_RE.search(body)) else None)
        e["ts"] = (m.group("t")
                   if e["parsed"] and (m := _TS_RE.search(body)) else None)
        e["notes"] = [ln for ln in e["lines"][1:] if ln.startswith(_NOTE_PREFIX)]
    return out


def _known_ids(d: Path, extra_live: list[dict] | None = None) -> set[str]:
    ids: set[str] = set()
    for e in extra_live or []:
        if e["parsed"]:
            ids.add(e["parsed"]["id"])
    live = split_entries(_live_path(d).read_text(encoding="utf-8")) \
        if _live_path(d).exists() else []
    ids.update(e["parsed"]["id"] for e in live if e["parsed"])
    ci = d / "arcs" / "cold-index.md"
    if ci.exists():
        for ln in ci.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^- (\S+) ", ln)
            if m:
                ids.add(m.group(1))
    return ids


# ---- 索引(L0,机械重建) ----
def rebuild_index(d: Path) -> dict:
    live = split_entries(_live_path(d).read_text(encoding="utf-8")) \
        if _live_path(d).exists() else []
    counts = {s: 0 for s in _STATUSES}
    rows = []
    for e in live:
        if not e["parsed"]:
            continue
        p = e["parsed"]
        counts[p["status"]] += 1
        tag = f" ref:{e['ref'][:8]}" if e["ref"] else ""
        rows.append(f"- {p['id']} {p['status']} {p['title']}{tag}")
    epitaphs = []
    ci = d / "arcs" / "cold-index.md"
    if ci.exists():
        epitaphs = [ln for ln in ci.read_text(encoding="utf-8").splitlines()
                    if ln.startswith("- ")][-EPITAPH_TAIL:]
    head = (f"# journal INDEX(L0 机械重建,勿手编;全文走 factor_journal_read)\n\n"
            f"更新:{time.strftime('%Y-%m-%dT%H:%M:%S')} | live "
            f"{len(rows)} 条(open {counts['open']}/registered "
            f"{counts['registered']}/adjudicated {counts['adjudicated']}/"
            f"active {counts['active']}) | 墓志累计 "
            f"{sum(1 for _ in chain_epitaphs(d))}\n\n## live\n"
            + ("\n".join(rows) if rows else "(空——本线尚未开立假设)")
            + f"\n\n## 墓志(尾 {len(epitaphs)})\n"
            + ("\n".join(epitaphs) if epitaphs else "(无)"))
    _atomic_write(d / "INDEX.md", head + "\n")
    return {"live_entries": len(rows), "counts": counts}


def chain_epitaphs(d: Path):
    ci = d / "arcs" / "cold-index.md"
    if ci.exists():
        for ln in ci.read_text(encoding="utf-8").splitlines():
            if ln.startswith("- "):
                yield ln


# ---- 遥测 ----
def _stats_path(d: Path) -> Path:
    return d / "stats.json"


def _read_stats(d: Path) -> dict:
    p = _stats_path(d)
    if p.exists():
        try:
            v = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    return {"evals": 0, "mirrored": 0, "appends": 0, "distills": 0}


def _bump_stats(d: Path, **kw) -> dict:
    st = _read_stats(d)
    for k, v in kw.items():
        st[k] = st.get(k, 0) + v
    _atomic_write(_stats_path(d), json.dumps(st, ensure_ascii=False))
    return st


def note_eval(root: str | Path) -> None:
    """每次 development evaluate 计数(登记率分母;无日志时静默)。"""
    d = _lane_dir(root, _current_lane())
    if not d.exists():
        return
    with state_write_lock(root):
        _bump_stats(d, evals=1)


# ---- 线名解析(2026-09-12 双入口修复)----
# DSH 的 TS 层把会话 lane 硬盖为会话 UUID(意图层隔离的代价)——日志若
# 跟会话 lane 走,每会话一本新日志,跨会话信念继承断。解法:journal 线名
# 独立解析,优先级 = 请求 journal_lane(TS 从插件配置盖,基础设施事实)
# > 进程 env DSH_FACTOR_JOURNAL_LANE > 会话 lane(直驱模式 = --lane,
# 天然稳定)。意图层(pending/族链/弧预算)不读本值,隔离语义零变化。
_journal_lane_ctx: ContextVar[str | None] = ContextVar(
    "dsh_factor_mining_journal_lane", default=None)
_JOURNAL_LANE_ENV = "DSH_FACTOR_JOURNAL_LANE"


def set_request_journal_lane(v: str | None):
    return _journal_lane_ctx.set(v)


def reset_request_journal_lane(token) -> None:
    _journal_lane_ctx.reset(token)


def resolve_journal_lane() -> str:
    v = _journal_lane_ctx.get() or os.environ.get(_JOURNAL_LANE_ENV)
    if v and str(v).strip():
        return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(v).strip())[:48] or DEFAULT_LANE
    return current_lane()


def _current_lane() -> str:
    """兼容别名:journal 内部一切线名解析走 resolve_journal_lane。"""
    return resolve_journal_lane()


# ---- 核心 RPC 语义(bridge 薄壳直接调用) ----
def append_entry(root: str | Path, content: str,
                 source: str | None = None) -> dict:
    """追加条目块(可多条)到 live.md。首行约定校验 + ID 唯一性 + ref 必填。

    source:registered 条目的因子源码——引擎算 source_fingerprint 作为
    ref(模型评估前拿不到 hash,这是注册闭环的唯一通道;显式 `ref:` 行
    与 source 同传时以 source 计算为准)。
    """
    d = _lane_dir(root, _current_lane())
    d.mkdir(parents=True, exist_ok=True)
    (d / "arcs").mkdir(exist_ok=True)
    (d / "raw").mkdir(exist_ok=True)
    text = str(content or "").strip()
    if not text:
        raise JournalError("journal 条目内容为空")
    if source is not None:
        from .discipline import source_fingerprint
        _h = source_fingerprint(str(source))
        # 头行下注入/覆盖 ref 行(已显式写 ref 的也以 source 计算为准)
        _lines = text.split("\n")
        _ins = 1 if _HEADER_RE.match(_lines[0].strip()) else 0
        _lines = [ln for ln in _lines if not ln.startswith("ref:")]
        _lines.insert(_ins, f"ref:{_h}")
        text = "\n".join(_lines)
    # 引擎盖章:首块头行下注入 ts 行(注册时机;registered_ledger 的数据
    # 源)。已有 ts:行被覆盖——时机由引擎首次写入锚定,不可事后改写
    _lines = text.split("\n")
    _ins = 1 if _HEADER_RE.match(_lines[0].strip()) else 0
    _lines = [ln for ln in _lines if not ln.startswith("ts:")]
    _lines.insert(_ins, f"ts:{time.strftime('%Y-%m-%dT%H:%M:%S')}")
    text = "\n".join(_lines)
    # 逐块校验:块 = 以合法头行起始的连续行
    blocks = split_entries(text)
    if not blocks:
        raise JournalError(
            "首行约定不符:条目必须以 `## <id> <status> <标题>` 开头"
            "(如 `## H-07 open 被动调仓截面梯度`);id 字母数字开头,"
            f"status ∈ {list(_BORN_OK) + ['(迁移态)']}。首个 `##` 行不匹配")
    parsed_all = []
    for b in blocks:
        if not b["parsed"]:
            raise JournalError(
                f"首行约定不符:`{b['header'][:60]}`——必须是 "
                "`## <id> <status> <标题>`(id 唯一、status 见文档)"
                "或去掉非法的 `## ` 行")
        p = b["parsed"]
        if p["status"] not in _BORN_OK:
            raise JournalError(
                f"{p['id']}:status={p['status']} 是迁移态/终态,不能作为出生态"
                "——出生只允许 open/registered/active/lesson,"
                "状态推进走 factor_journal_update")
        if p["status"] == "registered" and not b["ref"]:
            raise JournalError(
                f"{p['id']}:registered(预期注册)必须带 `ref:<source_hash>` 行"
                "——镜像按 hash 关联,没有 ref 的先 open,拿到因子源码再注册")
        parsed_all.append(p)
    with state_write_lock(root):
        known = _known_ids(d)
        dup = [p["id"] for p in parsed_all if p["id"] in known]
        if dup:
            raise JournalError(
                f"ID 重复:{dup}(live 或墓志中已存在)——每个假设/信念终身一个 ID,"
                "新假设换新 ID;重开已死假设 = 机制层复读,引擎拒绝")
        lp = _live_path(d)
        old = lp.read_text(encoding="utf-8") if lp.exists() else ""
        _atomic_write(lp, (old.rstrip() + "\n\n" if old.strip() else "") + text + "\n")
        _bump_stats(d, appends=len(parsed_all))
        idx = rebuild_index(d)
    live_chars = len(_live_path(d).read_text(encoding="utf-8"))
    return {"appended": [p["id"] for p in parsed_all],
            "index": idx, "live_chars": live_chars,
            "cap_warn": live_chars > LIVE_CHAR_CAP,
            "hint": ("live 区超预算——下次换向前先 factor_journal_distill 蒸馏"
                     if live_chars > LIVE_CHAR_CAP else None)}


def update_entry(root: str | Path, entry_id: str, status: str,
                 note: str | None = None) -> dict:
    """头行状态迁移(同状态=补 note 合法) + 可选 note 行插入头行下方。"""
    d = _lane_dir(root, _current_lane())
    if status not in _STATUSES:
        raise JournalError(f"未知 status: {status} ∈ {list(_STATUSES)}")
    with state_write_lock(root):
        lp = _live_path(d)
        if not lp.exists():
            raise JournalError("本线日志为空——先 factor_journal_append 开立条目")
        entries = split_entries(lp.read_text(encoding="utf-8"))
        hit = next((e for e in entries
                    if e["parsed"] and e["parsed"]["id"] == entry_id), None)
        if hit is None:
            raise JournalError(
                f"live 区找不到 {entry_id}(已归档条目不可迁移——墓志是终点;"
                "新证据开新条目并引用旧 ID)")
        cur = hit["parsed"]["status"]
        if status != cur and (cur, status) not in _TRANSITIONS:
            raise JournalError(
                f"非法迁移 {cur}→{status}。合法迁移:"
                "open→registered→adjudicated→resolved|stale,"
                "active→retired;同状态调用仅补 note")
        new_header = re.sub(
            r"^(## \S+) \S+ ", rf"\1 {status} ", hit["header"], count=1)
        lines = hit["lines"]
        lines[0] = new_header
        if note:
            lines.insert(1, f"{_NOTE_PREFIX}{status} "
                            f"{time.strftime('%m-%d %H:%M')}] {str(note)[:300]}")
        hit["lines"] = lines
        hit["text"] = "\n".join(lines).rstrip() + "\n"
        rebuilt = _rewrite_live_with(d, entries)
        idx = rebuild_index(d)
    return {"id": entry_id, "from": cur, "to": status,
            "note": bool(note), "live_chars": rebuilt}


def _rewrite_live_with(d: Path, entries: list[dict]) -> int:
    """按(可能被修改的)条目列表重写 live;首个头行前的 preamble 原样保留。"""
    lp = _live_path(d)
    raw = lp.read_text(encoding="utf-8")
    first_idx = next((i for i, ln in enumerate(raw.splitlines())
                      if ln.startswith("## ")), len(raw.splitlines()))
    preamble = "\n".join(raw.splitlines()[:first_idx]).rstrip()
    body = "\n\n".join(e["text"].rstrip("\n") for e in entries)
    text = (preamble + "\n\n" if preamble else "") + body + "\n"
    _atomic_write(lp, text)
    return len(text)


def read_journal(root: str | Path, level: int = 0, entry_id: str | None = None) -> dict:
    """渐进披露读:L0=INDEX / L1=live 全文 / L2=按 id 拉单条全文(live→arcs)。"""
    d = _lane_dir(root, _current_lane())
    if not d.exists():
        return {"exists": False,
                "hint": "本线尚无日志——首个假设用 factor_journal_append 开立"}
    level = int(level)
    if level == 0:
        return {"exists": True, "level": 0,
                "index": (d / "INDEX.md").read_text(encoding="utf-8")
                if (d / "INDEX.md").exists() else "(索引待重建——任意 append 触发)"}
    if level == 1:
        lp = _live_path(d)
        return {"exists": True, "level": 1, "live_chars":
                lp.stat().st_size if lp.exists() else 0,
                "live": lp.read_text(encoding="utf-8") if lp.exists() else "(空)"}
    # L2:按 id 拉全文
    if not entry_id:
        raise JournalError("level=2 需要 entry_id(INDEX 里查)")
    lp = _live_path(d)
    if lp.exists():
        hit = next((e for e in split_entries(lp.read_text(encoding="utf-8"))
                    if e["parsed"] and e["parsed"]["id"] == entry_id), None)
        if hit:
            return {"exists": True, "level": 2, "where": "live",
                    "entry": hit["text"]}
    for p in sorted((d / "arcs").glob("arc-*.md")) if (d / "arcs").exists() else []:
        hit = next((e for e in split_entries(p.read_text(encoding="utf-8"))
                    if e["parsed"] and e["parsed"]["id"] == entry_id), None)
        if hit:
            return {"exists": True, "level": 2, "where": f"arcs/{p.name}",
                    "entry": hit["text"]}
    ci = d / "arcs" / "cold-index.md"
    if ci.exists():
        for ln in ci.read_text(encoding="utf-8").splitlines():
            if ln.startswith(f"- {entry_id} "):
                return {"exists": True, "level": 2, "where": "cold-index",
                        "entry": ln,
                        "hint": "全文已被蒸馏压缩——墓志即全部保留信息,"
                                "raw/ 快照按审计流程手动取"}
    raise JournalError(f"entry_id {entry_id} 在 live/arcs/墓志中均未找到")


def distill(root: str | Path, new_live: str) -> dict:
    """弧末蒸馏:快照→分拣→重写 live→归档+墓志→重建 INDEX。

    分拣规则(机械,零语义):
    - 新 live 未包含的活条目(open/registered/adjudicated/active)→自动
      保留回 live(带警示)——蒸馏不允许丢活假设;
    - 未包含的终条目(resolved/stale/retired/lesson)→全文入 arcs/,墓志
      入 cold-index(死因取最后一条 update note);
    - 快照永远先落 raw/(防洗账:预测史不可悄悄改写)。
    """
    d = _lane_dir(root, _current_lane())
    if not d.exists():
        raise JournalError("本线尚无日志——没有可蒸馏的内容")
    with state_write_lock(root):
        lp = _live_path(d)
        old_text = lp.read_text(encoding="utf-8") if lp.exists() else ""
        if not old_text.strip():
            raise JournalError("live 区为空——没有可蒸馏的内容")
        (d / "raw").mkdir(exist_ok=True)
        (d / "arcs").mkdir(exist_ok=True)
        snap = d / "raw" / f"live-{time.strftime('%Y%m%d-%H%M%S')}.md"
        _atomic_write(snap, old_text)
        old_entries = split_entries(old_text)
        new_entries = split_entries(str(new_live or ""))
        new_ids = {e["parsed"]["id"] for e in new_entries if e["parsed"]}
        dropped = [e for e in old_entries
                   if e["parsed"] and e["parsed"]["id"] not in new_ids]
        kept_active, archived = [], []
        arc_no = len(list((d / "arcs").glob("arc-*.md"))) + 1
        arc_lines, epitaph_lines = [], []
        for e in dropped:
            p = e["parsed"]
            if p["status"] in _ACTIVE:
                kept_active.append(p["id"])
                continue
            arc_lines.append(e["text"].rstrip("\n"))
            last_note = e["notes"][-1][len(_NOTE_PREFIX):][:120] if e["notes"] else ""
            ref = f" | ref:{e['ref'][:12]}" if e["ref"] else ""
            epitaph_lines.append(
                f"- {p['id']} {p['status']} {p['title']}{ref}"
                + (f" | {last_note}" if last_note else ""))
            archived.append(p["id"])
        if arc_lines:
            _atomic_write(d / "arcs" / f"arc-{arc_no}.md",
                          f"# arc-{arc_no} 蒸馏归档 "
                          f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n\n"
                          + "\n\n".join(arc_lines) + "\n")
            ci = d / "arcs" / "cold-index.md"
            old_ci = ci.read_text(encoding="utf-8") if ci.exists() else ""
            _atomic_write(ci, (old_ci.rstrip() + "\n" if old_ci.strip() else "")
                          + "\n".join(epitaph_lines) + "\n")
        # 新 live(模型蒸馏稿)+ 自动保留的活条目
        kept_blocks = []
        if kept_active:
            kept_blocks = [e["text"].rstrip("\n") for e in dropped
                           if e["parsed"] and e["parsed"]["id"] in kept_active]
        body = str(new_live or "").strip("\n")
        if kept_blocks:
            body += ("\n\n## ——— 蒸馏遗漏自动保留(引擎)———\n\n"
                     + "\n\n".join(kept_blocks))
        _atomic_write(lp, body + "\n")
        _bump_stats(d, distills=1)
        idx = rebuild_index(d)
    return {"snapshot": snap.name, "archived": archived,
            "kept_active": kept_active,
            "warning": ("蒸馏稿遗漏了活条目,已自动保留——把它们的结论"
                        "写进下一版蒸馏稿再提交一次" if kept_active else None),
            "index": idx,
            "live_chars": len(_live_path(d).read_text(encoding="utf-8"))}


# ---- 镜像(机械并置:预期 vs 实测,引擎不判定) ----
def mirror_hook(root: str | Path, source_hash: str,
                actual: dict[str, Any]) -> dict | None:
    """development evaluate 完成后按 ref 关联 registered 条目。

    返回并置块(引擎不做任何判断,结算由模型在 update 里写);
    状态机械翻转为 adjudicated。无关联 → None(调用方静默)。
    """
    d = _lane_dir(root, _current_lane())
    lp = _live_path(d)
    if not lp.exists():
        return None
    matches = []
    with state_write_lock(root):
        entries = split_entries(lp.read_text(encoding="utf-8"))
        dirty = False
        for e in entries:
            if not e["parsed"] or e["parsed"]["status"] != "registered":
                continue
            if e["ref"] != str(source_hash).lower():
                continue
            p = e["parsed"]
            body_lines = [ln for ln in e["lines"][1:]
                          if not ln.startswith(_NOTE_PREFIX)
                          and not ln.startswith("ref:")]
            excerpt = " / ".join(ln.strip() for ln in body_lines
                                 if ln.strip())[:200] or "(预期正文为空)"
            actual_slim = {k: v for k, v in actual.items() if v is not None}
            matches.append({"id": p["id"], "title": p["title"],
                            "expectation": excerpt, "actual": actual_slim,
                            "registered_ts": e.get("ts"),
                            "task": ("对账预期与实测,用 factor_journal_update "
                                     "结算(resolved/stale)+写结论 note")})
            e["lines"][0] = re.sub(r"^(## \S+) \S+ ", r"\1 adjudicated ",
                                   e["header"], count=1)
            e["lines"].insert(
                1, f"{_NOTE_PREFIX}adjudicated "
                   f"{time.strftime('%m-%d %H:%M')}] 镜像:评估已完成,待结算")
            # lines 改动必须同步回 text(_rewrite_live_with 消费 text)
            e["text"] = "\n".join(e["lines"]).rstrip() + "\n"
            dirty = True
        if dirty:
            _rewrite_live_with(d, entries)
            rebuild_index(d)
            _bump_stats(d, mirrored=1)
    return {"entries": matches} if matches else None


def journal_pointer(root: str | Path) -> dict:
    """loop 响应的一行指针(预算 ~60 token;全文走 journal_read)。"""
    d = _lane_dir(root, _current_lane())
    if not d.exists():
        return {"exists": False,
                "hint": "推理日志未开立——首个假设 factor_journal_append"
                        "(评估前 registered+ref 注册预期,镜像自动对账)"}
    st = _read_stats(d)
    live = split_entries(_live_path(d).read_text(encoding="utf-8")) \
        if _live_path(d).exists() else []
    counts = {s: 0 for s in _ACTIVE}
    for e in live:
        if e["parsed"] and e["parsed"]["status"] in counts:
            counts[e["parsed"]["status"]] += 1
    live_chars = _live_path(d).stat().st_size if _live_path(d).exists() else 0
    return {"exists": True, **counts, "live_chars": live_chars,
            "registration_rate": (round(st["mirrored"] / st["evals"], 3)
                                  if st.get("evals") else None),
            "cap_warn": live_chars > LIVE_CHAR_CAP,
            "read": "factor_journal_read level=1 会话开场装载 live 区"}
