# coding=utf-8
"""B1 提交顺序强制（2026-10-14 任务书 §三 B1）——registry.submit 前置校验。

要堵的洞：模型可以先写出公式、事后补故事——门口无法区分回忆路径
（搬经典构造）与推导路径（机制叙述先行）。本门要求：机制叙述+可
检验预测必须**先于评估**存在于推理日志（journal registered 条目带
ref:<source_hash>，引擎盖章 ts 为时序证据）。

红线：
- 零语义解析——journal 正文只做构造块词法匹配（navigation.extract_blocks）；
- 统计门链一行不动——本门是新增前置顺序校验，不修改任何既有校验；
- 双档运行：shadow（只记录违规）/ enforce（事务性拒绝：不落盘、不占
  名字——校验发生在 submit 任何写入之前）。切档按任务书内嵌自动规则
  （2026-10-14 用户授权，无需人工拍板），切档事件落 gate_audit.jsonl。

豁免面：origin ∈ {random, collision} 的试验是引擎骰子产物（随机树/
碰撞卡），构造上不存在模型推导叙述，天然豁免；literature/mechanism/
hypothesis 出身是模型推导路径的产物，照常受门约束（mechanism 假设
按其通道纪律本就应先落 journal——门与通道纪律同构，不是新增负担）。
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any

from .filelock import state_write_lock
from .state import MINING_CONFIG, read_mining_state, _atomic_write_json, _path
from .navigation import extract_blocks

#: 豁免出身（引擎骰子产物，无模型叙述可言）
EXEMPT_ORIGINS = {"random", "collision"}
#: 无时序证据时可作为预注册在场证明的状态（镜像机制的状态机痕迹）
_PREREG_STATUSES = {"registered", "adjudicated"}
_STATE_KEY = "submission_order_gate_state"
_RECENT_CAP = 500
_MISSING_BLOCKS_CAP = 8


class SubmissionOrderViolation(ValueError):
    """预注册顺序违规（附机器可读 detail；enforce 档映射为事务性拒绝）。"""

    def __init__(self, kind: str, detail: dict):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


def _audit_append(state_root: "str | Path", event: str, detail: dict) -> None:
    """切档/诊断事件审计日志（append-only JSONL；写失败静默不阻塞提交）。"""
    try:
        p = Path(state_root) / "gate_audit.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "event": event, "detail": detail},
                               ensure_ascii=False) + "\n")
    except Exception:
        pass


def _parse_ts(s: str) -> "float | None":
    try:
        return _dt.datetime.fromisoformat(str(s)).timestamp()
    except Exception:
        return None


def find_preregistration(journal_dir: "str | Path",
                         source_hash: str) -> "dict | None":
    """在 journal 线目录（live + arcs）里找 ref==hash 的预注册条目。

    返回 {"status", "ts", "text"}；未找到 → None。墓志（cold-index）只存
    hash 前 12 位，不作为精确匹配源（resolved 旧条目的再次提交本就会被
    同 hash 铁律拦下，不需要本门重复把关）。
    """
    from .journal import split_entries
    d = Path(journal_dir)
    files = [d / "live.md"]
    arcs = d / "arcs"
    if arcs.exists():
        files.extend(sorted(arcs.glob("arc-*.md")))
    for f in files:
        if not f.exists():
            continue
        try:
            entries = split_entries(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in entries:
            if not e["parsed"] or e["ref"] != str(source_hash).lower():
                continue
            return {"status": e["parsed"]["status"], "ts": e.get("ts"),
                    "text": e["text"]}
    return None


def check_violation(source: str, source_hash: str,
                    trail_entries_for_hash: list[dict],
                    journal_dir: "str | Path") -> "dict | None":
    """纯函数判定（不落账）：None=通过；dict=违规（kind/detail）。"""
    pre = find_preregistration(journal_dir, source_hash)
    if pre is None:
        return {"kind": "no_preregistration",
                "detail": {"hash": str(source_hash)[:12]}}
    # 时序证据：journal ts（引擎首写盖章）必须先于本 hash 首次评估
    eval_ts = min((t for t in (_parse_ts(e.get("ts"))
                               for e in trail_entries_for_hash) if t),
                  default=None)
    reg_ts = _parse_ts(pre.get("ts") or "")
    if eval_ts is not None and reg_ts is not None and reg_ts > eval_ts:
        return {"kind": "late_registration",
                "detail": {"hash": str(source_hash)[:12],
                           "registered_ts": pre.get("ts")}}
    if reg_ts is None and pre["status"] not in _PREREG_STATUSES:
        # 无时序证据时只认镜像状态机的痕迹（registered/adjudicated）
        return {"kind": "no_preregistration",
                "detail": {"hash": str(source_hash)[:12],
                           "status": pre["status"]}}
    # 推导链对账：源码构造块逐一在条目正文词法出现
    src_blocks = extract_blocks(str(source or ""), code=True)
    txt_blocks = extract_blocks(pre["text"])
    missing = sorted(src_blocks - txt_blocks)
    if missing:
        return {"kind": "unexplained_blocks",
                "detail": {"hash": str(source_hash)[:12],
                           "missing_blocks": missing[:_MISSING_BLOCKS_CAP],
                           "n_missing": len(missing)}}
    return None


def _gate_state(st: dict) -> dict:
    g = st.get(_STATE_KEY)
    if not isinstance(g, dict):
        g = {}
    g.setdefault("mode", str(MINING_CONFIG.get("submission_order_gate",
                                               "shadow")))
    g.setdefault("first_seen", time.strftime("%Y-%m-%dT%H:%M:%S"))
    g.setdefault("n", 0)
    g.setdefault("recent", [])
    g.setdefault("violations_total", 0)
    g.setdefault("diagnostic_emitted", False)
    return g


def _recent_rate(g: dict, window: int) -> "float | None":
    rec = [int(x) for x in (g.get("recent") or [])][-window:]
    if len(rec) < window:
        return None
    return sum(rec) / len(rec)


def evaluate_and_record(state_root: "str | Path", source: str,
                        source_hash: "str | None",
                        journal_dir: "str | Path",
                        *, now: "float | None" = None) -> dict:
    """submit 入口调用：判定 + 记账 + 自动切档（锁内原子，一律重读盘）。

    返回 {"mode", "exempt", "violation", "n", "recent_rate",
    "switched", "diagnostic"}。enforce 档违规由调用方映射为事务性
    拒绝（本函数不 raise——记账先于拒绝，shadow/enforce 共用一条账）。
    """
    now = now if now is not None else time.time()
    with state_write_lock(Path(state_root)):
        st = read_mining_state(state_root)
        g = _gate_state(st)
        trail = _read_trail(state_root)
        entries_for_hash = [e for e in trail
                            if str(e.get("source_hash") or "")
                            == str(source_hash or "")]
        origins = {str(e.get("origin") or "") for e in entries_for_hash}
        exempt = bool(origins) and origins <= EXEMPT_ORIGINS
        violation = None if exempt else check_violation(
            str(source or ""), str(source_hash or ""), entries_for_hash,
            journal_dir)
        # 记账（豁免与违规都计观察数——观察窗按提交计，不按出身计）
        g["n"] = int(g.get("n", 0)) + 1
        rec = [int(x) for x in (g.get("recent") or [])]
        rec.append(1 if violation else 0)
        g["recent"] = rec[-_RECENT_CAP:]
        if violation:
            g["violations_total"] = int(g.get("violations_total", 0)) + 1
        # ---- 自动切档（任务书 §三 B1.3 内嵌规则；阈值可被
        #      mining_state 同名键覆盖）----
        min_n = int(st.get("gate_min_window_n",
                           MINING_CONFIG.get("gate_min_window_n", 200)))
        min_age = float(st.get("gate_min_window_s",
                               MINING_CONFIG.get("gate_min_window_s",
                                                 2419200)))
        rate_max = float(st.get("gate_rate_max",
                                MINING_CONFIG.get("gate_rate_max", 0.10)))
        rate_win = int(st.get("gate_rate_window",
                              MINING_CONFIG.get("gate_rate_window", 50)))
        giveup_n = int(st.get("gate_giveup_n",
                              MINING_CONFIG.get("gate_giveup_n", 500)))
        giveup_age = float(st.get("gate_giveup_s",
                                  MINING_CONFIG.get("gate_giveup_s",
                                                    4838400)))
        first_ts = _parse_ts(g.get("first_seen") or "")
        age = (now - first_ts) if first_ts else 0.0
        rate = _recent_rate(g, rate_win)
        switched = False
        diagnostic = False
        if g["mode"] == "shadow":
            window_met = g["n"] >= min_n or age >= min_age
            giveup_met = g["n"] >= giveup_n or age >= giveup_age
            if window_met and rate is not None and rate <= rate_max:
                g["mode"] = "enforce"
                switched = True
                _audit_append(state_root, "gate_switch_enforce", {
                    "n": g["n"], "age_s": round(age, 0),
                    "recent_rate": round(rate, 4),
                    "violations_total": g["violations_total"],
                    "rule": (f"观察窗满（n>={min_n} 或 {int(min_age//86400)}天）"
                             f"且最近 {rate_win} 次违规率 {rate:.3f}<={rate_max}"
                             "——shadow 自动切 enforce"),
                })
            elif giveup_met and rate is not None and rate > rate_max \
                    and not g.get("diagnostic_emitted"):
                diagnostic = True
                g["diagnostic_emitted"] = True
                _audit_append(state_root, "gate_switch_diagnostic", {
                    "n": g["n"], "age_s": round(age, 0),
                    "recent_rate": round(rate, 4),
                    "rule": (f"累计 n>={giveup_n} 或 {int(giveup_age//86400)}天"
                             f" 而违规率 {rate:.3f}>{rate_max}——维持 shadow，"
                             "先修纪律文案/journal 流程可读性再等下一窗"),
                })
        st[_STATE_KEY] = g
        _atomic_write_json(_path("mining_state", state_root), st)
    return {"mode": g["mode"], "exempt": exempt, "violation": violation,
            "n": g["n"], "recent_rate": (round(rate, 4)
                                         if rate is not None else None),
            "switched": switched, "diagnostic": diagnostic}


def _read_trail(state_root: "str | Path") -> list[dict]:
    p = Path(state_root) / "trail_engine.json"
    if not p.exists():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def rejection_message(violation: dict) -> str:
    """enforce 档教学式自足拒收文案（过黑名单：零黑话/零历史引用/零日期）。"""
    kind = violation.get("kind")
    d = violation.get("detail") or {}
    if kind == "no_preregistration":
        return (
            "提交被拒：这份因子源码没有先于评估的预注册记录。提交流程要求"
            "「机制叙述+可检验预测」必须先于第一次评估写入推理日志：写好"
            "源码后、第一次 factor_evaluate 之前，调用 factor_journal_append"
            " 开一条 registered 条目（content 写机制推理与可检验预测），并把"
            "源码原文通过 source 参数一并传入——引擎按源码盖 ref 关联章。"
            "补注册后原样重新提交即可。")
    if kind == "late_registration":
        return (
            "提交被拒：预注册条目写入时间晚于本因子第一次评估——先评估后"
            "补写的预测不是预测。正确顺序：写源码 → factor_journal_append"
            "（registered + source 参数）→ factor_evaluate → 提交。下一个"
            "假设按此顺序操作即可。")
    if kind == "unexplained_blocks":
        blocks = "、".join(str(b) for b in d.get("missing_blocks") or [])
        return (
            f"提交被拒：推导链对账未通过——源码中的构造块 [{blocks}] 在预注册"
            "条目正文里没有出现。预注册不是仪式：条目正文必须把构造使用的"
            "算子与字段逐一说明（用它们的名称，如 ts_rank、cs_rank、close、"
            "volume），让推导链可核对。用 factor_journal_update 给对应条目"
            "补 note 说明这些构造块后原样重新提交。")
    return "提交被拒：提交顺序校验未通过（预注册缺失或推导链不可对账）。"


def gate_status(state_root: "str | Path") -> dict:
    """运维视图（scheduler_ops 同款只读入口风格）。"""
    st = read_mining_state(state_root)
    g = _gate_state(st)
    rate_win = int(MINING_CONFIG.get("gate_rate_window", 50))
    rec = [int(x) for x in (g.get("recent") or [])]
    tail = rec[-rate_win:]
    rate = (round(sum(tail) / len(tail), 4) if len(tail) >= rate_win
            else None)
    return {"mode": g["mode"], "n": g.get("n", 0),
            "violations_total": g.get("violations_total", 0),
            "recent_rate_window": rate_win, "recent_rate": rate}
