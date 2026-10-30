# coding=utf-8
"""派生图存储 + 分支级事件日志（调度器票 1a/1b，2026-09-29 任务书）。

把「进行中研究」统一成带族谱的 frontier 的底座：节点 = 试验过的因子
表达式（source_hash 即键），边 = 派生操作。存储沿用包内模式：追加式
jsonl + 内存索引 + filelock 单写者（红线 6）。

文件（全部 stateRoot 下）：
- derivation_graph.jsonl   节点/边一行一条（幂等：hash 首见即冻结）
- derivation_events.jsonl  事件流：expand（试验派发入账）/ terminal
  （registry 判定）/ score（自 3a 起由 scheduler 落）

边来源三纪律（任务书 1a）：
- order      点名回执挂边——深化点名/调度器选点触发的试验，parent=点名目标
- farm_seed  农场种子根节点——origin=random 的试验 parent=null（farm 供数）
- declared   模型自产按构造台账声明挂边（_entries_linked 双信号推断的
             同族前驱）；未声明/推断不出者诚实记根 root_unlinked

五不变量（check_invariants 可查，golden 测试锁死）：
I1 图无环——父链有限步到根（ancestry 环检测）
I2 子表达式可复现——可重放 op（注册表内的确定性变换）重放哈希一致；
   declared/root 边只验证 parent 存在（自由构造机械上不可重放，诚实记）
I3 单写者——全部写路径过 state_write_lock（含嵌套重入）
I4 打分确定性——同输入+同 fn_version → 同 A（scheduler.py 回归锁）
I5 关闭等价——scheduler.enabled=false 时本模块照记事实账（图+事件是
   事实层，独立于调度器开关），但不碰任何既有判定路径：挂点全部
   try/except 包裹，失败只写 stderr 不阻断评估

红线 5（活动计数隔离）：本模块零调用 lane_activity_tick / 试验计数——
记账不是挖掘活动，不得喂饱增长双轴。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .filelock import state_write_lock

GRAPH_FILE = "derivation_graph.jsonl"
EVENTS_FILE = "derivation_events.jsonl"

#: 边来源枚举（任务书三纪律 + 诚实根）
EDGE_SOURCES = ("order", "farm_seed", "declared", "root_unlinked")

#: 节点状态机：frontier（活跃）→ incubating（停车，可重返）→
#: dead|admitted|falsified（终态）。退场进孵化池可重返——拒绝非终点。
NODE_STATUSES = ("frontier", "incubating", "dead", "admitted", "falsified")

#: 可重放 op 注册表：op 名 → (parent_source, op_params) -> child_source。
#: 只有确定性变换可注册（I2 的机械基础）；自由构造不注册（走 declared）。
_REPLAY: dict[str, Callable[[str, dict], str]] = {}


def register_replay(op: str, fn: Callable[[str, dict], str]) -> None:
    """注册可重放变换（drop_leg 等）。同名覆盖=显式换实现（须随版本声明）。"""
    _REPLAY[op] = fn


def _graph_path(root) -> Path:
    return Path(root) / GRAPH_FILE


def _events_path(root) -> Path:
    return Path(root) / EVENTS_FILE


def _append_line(path: Path, obj: dict) -> None:
    """锁内追加一行 jsonl（调用方持锁或经本模块公开函数进入锁）。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


# 进程内只读缓存：{(root, file): (mtime_ns, payload)}——写路径主动失效
_CACHE: dict[tuple, tuple] = {}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue  # 半行（崩溃残留）跳过——追加式账本读端容错
    except Exception:
        return []
    return out


def _cached_load(root, which: str) -> list[dict]:
    path = _graph_path(root) if which == "graph" else _events_path(root)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    key = (str(Path(root).resolve()), which)
    hit = _CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    data = _load_jsonl(path)
    _CACHE[key] = (mtime, data)
    return data


def _invalidate(root, which: str) -> None:
    key = (str(Path(root).resolve()), which)
    _CACHE.pop(key, None)


def load_events(root) -> list[dict]:
    """事件流原样（只读快照）。"""
    return _cached_load(root, "events")


def load_children(root) -> dict[str, list[str]]:
    """父 → 子列表索引（load_nodes 的派生视图）。"""
    ch: dict[str, list[str]] = {}
    for h, n in load_nodes(root).items():
        p = n.get("parent")
        if isinstance(p, str) and p:
            ch.setdefault(p, []).append(h)
    return ch


# ------------------------------------------------------------------ 写路径

def note_node(root, hash: str, parent: str | None, op: str | None,
              edge_source: str, ts: str | None = None, lane: str | None = None,
              origin: str | None = None, gen_layer: str | None = None,
              op_params: dict | None = None,
              status: str = "frontier") -> dict:
    """记节点（幂等：同 hash 首见冻结，后见不覆盖——族谱不可事后改写）。

    edge_source 三纪律：order（点名回执）/ farm_seed（农场种子根）/
    declared（构造台账声明推断）；root_unlinked = 推断不出的诚实根。
    返回 {"hash", "dedup": bool}。"""
    if edge_source not in EDGE_SOURCES:
        raise ValueError(f"edge_source 须为 {EDGE_SOURCES} 之一: {edge_source}")
    root = Path(root)
    with state_write_lock(root):
        if hash in load_nodes(root):
            return {"hash": hash, "dedup": True}
        rec = {
            "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "hash": hash,
            "parent": parent if isinstance(parent, str) and parent else None,
            "op": op,
            "op_params": op_params or {},
            "edge_source": edge_source,
            "lane": lane,
            "origin": origin,
            "gen_layer": gen_layer,
            "status": status,
        }
        _append_line(_graph_path(root), rec)
        _invalidate(root, "graph")
        return {"hash": hash, "dedup": False}


def update_status(root, hash: str, status: str) -> dict:
    """更新节点状态机（admitted/dead/falsified/incubating/frontier）。

    首见冻结只冻边（族谱），状态是活的——退场可重返（incubating→
    frontier）是「拒绝非终点」的机械形态。"""
    if status not in NODE_STATUSES:
        raise ValueError(f"status 须为 {NODE_STATUSES} 之一: {status}")
    root = Path(root)
    with state_write_lock(root):
        nodes = load_nodes(root)
        if hash not in nodes:
            return {"hash": hash, "updated": False, "reason": "节点不存在"}
        # jsonl 追加式：状态变更追加新行，load_nodes 首见冻结会忽略它——
        # 故状态走「尾行覆盖」：读端以最后一条状态行为准。见 _status_view。
        rec = dict(nodes[hash])
        rec["status"] = status
        rec["status_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _append_line(_graph_path(root), rec)
        _invalidate(root, "graph")
        return {"hash": hash, "updated": True, "status": status}


def _status_view(nodes_raw: list[dict]) -> dict[str, dict]:
    """图文件的读端视图：同 hash 多行时，边字段取首见、status 取尾行。"""
    view: dict[str, dict] = {}
    for rec in nodes_raw:
        h = rec.get("hash")
        if not isinstance(h, str) or not h:
            continue
        if h in view:
            cur = view[h]
            cur["status"] = rec.get("status") or cur.get("status")
            cur["status_ts"] = rec.get("status_ts")
        else:
            view[h] = dict(rec)
    return view


def load_nodes(root) -> dict[str, dict]:
    """全部节点（边字段首见冻结 + status 尾行覆盖）。"""
    return _status_view(_cached_load(root, "graph"))


def note_expand(root, child: str, parent: str | None, op: str | None,
                edge_source: str, compute: dict | None = None,
                new_candidate: bool = True, lane: str | None = None,
                origin: str | None = None, ts: str | None = None,
                horizon: int | None = None, stage: str | None = None,
                extra: dict | None = None) -> dict:
    """expand 事件：一次试验派发入账（1b——全项目唯一越拖越贵的票）。

    compute 含算力消耗（cpu_s 实测、slots=1/次派发、tokens_est 可空）；
    new_candidate = 该 (hash, horizon) 首次出现（dedup-miss）——统计记账
    开关：只有新候选消耗 B_s，缓存命中只付算力钱。

    extra：评估时点快照（dsr 四元组 sr/skew/kurt/n、池参数 bar_sigma/
    pool_std、p_def/t_def、墙单位增益 gain、a_parent、near_miss_flag、
    family）——r̂ 累积（F7 冻结 null 计价）、周对账（F6 同池态差分）、
    重放（F10 反事实池态）全部自足于事件流，不依赖事后 trail 重读。"""
    ev = {
        "ev": "expand", "ts": ts or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hash": parent, "child": child, "op": op,
        "edge_source": edge_source,
        "compute": compute or {},
        "new_candidate": bool(new_candidate),
        "lane": lane, "origin": origin,
        "horizon": horizon, "stage": stage,
    }
    if extra:
        for k, v in extra.items():
            if k not in ev:
                ev[k] = v
    root = Path(root)
    with state_write_lock(root):
        _append_line(_events_path(root), ev)
        _invalidate(root, "events")
    return ev


def note_terminal(root, hash: str, verdict: str, t_def=None, p_def=None,
                  lane: str | None = None, extra: dict | None = None) -> dict:
    """terminal 事件：registry 判定（admitted/rejected）/ 判死。"""
    ev = {
        "ev": "terminal", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hash": hash, "verdict": verdict,
        "t_def": t_def, "p_def": p_def, "lane": lane,
    }
    if extra:
        ev.update(extra)
    root = Path(root)
    with state_write_lock(root):
        _append_line(_events_path(root), ev)
        _invalidate(root, "events")
    return ev


def note_score(root, hash: str, terms: dict, rank: int | None = None,
               fn_version: str = "", policy: str = "shadow",
               executed: bool = False, lane: str | None = None) -> dict:
    """score 事件（3a 起）：原始分项全量入账——权重离线反事实搜索的基础。

    fn_version 非空是验收硬条件（打分可复现的钥匙）。"""
    if not fn_version:
        raise ValueError("score 事件必须带 fn_version（重放可复现的钥匙）")
    ev = {
        "ev": "score", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fn_version": fn_version, "hash": hash,
        "terms": terms, "rank": rank, "policy": policy,
        "executed": bool(executed), "lane": lane,
    }
    root = Path(root)
    with state_write_lock(root):
        _append_line(_events_path(root), ev)
        _invalidate(root, "events")
    return ev


# ------------------------------------------------------------------ 查询

def ancestry(root, hash: str, nodes: dict | None = None) -> list[str]:
    """父链 [hash, parent, ..., root]。环 → ValueError（I1 读端防御）。"""
    nodes = nodes if nodes is not None else load_nodes(root)
    chain = []
    cur = hash
    seen = set()
    while cur is not None:
        if cur in seen:
            raise ValueError(f"派生图存在环: {' -> '.join(chain + [cur])}")
        seen.add(cur)
        chain.append(cur)
        node = nodes.get(cur)
        if node is None:
            break  # 父不在图内（首见冻结前的历史/外部节点）——链到可考为止
        cur = node.get("parent")
    return chain


def family_root(root, hash: str, nodes: dict | None = None) -> str:
    """族根（父链末端）。"""
    return ancestry(root, hash, nodes)[-1]


def descendants(root, hash: str, nodes: dict | None = None) -> list[str]:
    """全部后代（BFS；含自身）。"""
    nodes = nodes if nodes is not None else load_nodes(root)
    ch = load_children(root)
    out, stack = [], [hash]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.append(cur)
        stack.extend(ch.get(cur, []))
    return out


# ------------------------------------------------------------------ 不变量

def replay_child(op: str, parent_source: str, op_params: dict) -> str | None:
    """应用注册表内的确定性变换（I2 重放）。未注册 op → None。"""
    fn = _REPLAY.get(op)
    if fn is None:
        return None
    return fn(parent_source, op_params or {})


def check_invariants(root, nodes: dict | None = None) -> dict:
    """五不变量中可机械检查的三件（I3 是代码纪律、I4 在 scheduler 测试）。

    返回 {"acyclic", "cycles", "replay", "replay_failures", "orphan_edges",
    "edge_sources", "node_count"}。"""
    nodes = nodes if nodes is not None else load_nodes(root)
    cycles: list[str] = []
    for h in nodes:
        try:
            ancestry(root, h, nodes)
        except ValueError:
            cycles.append(h)
    replay_failures = []
    orphan_edges = []
    for h, n in nodes.items():
        p = n.get("parent")
        if p and p not in nodes:
            orphan_edges.append({"hash": h, "parent": p})
        # I2 只对可重放 op 校验（declared/root 边机械上不可重放——诚实记）
        op = n.get("op")
        if op in _REPLAY:
            src_path = None
            try:
                from .state import factor_source_path
                src_path = factor_source_path(p, root)
                parent_src = src_path.read_text(encoding="utf-8") \
                    if src_path and src_path.exists() else None
            except Exception:
                parent_src = None
            if parent_src is None:
                continue  # 父源码不在外挂库（历史节点）——重放校验跳过
            try:
                child = replay_child(op, parent_src, n.get("op_params") or {})
                from .discipline import source_fingerprint
                if child is not None and source_fingerprint(child) != h:
                    replay_failures.append({"hash": h, "op": op})
            except Exception as e:
                replay_failures.append({"hash": h, "op": op, "error": str(e)[:120]})
    return {
        "acyclic": not cycles,
        "cycles": cycles,
        "replay": not replay_failures,
        "replay_failures": replay_failures,
        "orphan_edges": orphan_edges,
        "edge_sources": sorted({n.get("edge_source") for n in nodes.values()
                                if n.get("edge_source")}),
        "node_count": len(nodes),
    }


# ------------------------------------------------------------------ 台账投影

def export_ledger_view(root, out_path=None, limit_families: int = 50) -> str:
    """构造台账人类可读投影（D1 合流：一底座两投影——图是底座，本视图
    替代手写 construction_ledger.md 的记忆负担，导出不改账）。

    按族根分组：根 → 边（op/来源/ts/lane）→ 终态（admitted 标注）。"""
    nodes = load_nodes(root)
    roots = {h: n for h, n in nodes.items() if not n.get("parent")}
    fam_members: dict[str, list] = {}
    for h in nodes:
        try:
            fam_members.setdefault(family_root(root, h, nodes), []).append(h)
        except ValueError:
            fam_members.setdefault(h, []).append(h)
    lines = [
        "# 派生图台账投影（自动导出——只读视图，账本以 jsonl 为准）",
        "",
        f"导出时间: {time.strftime('%Y-%m-%dT%H:%M:%S')}  "
        f"节点: {len(nodes)}  族根: {len(fam_members)}",
        "",
    ]
    ranked = sorted(fam_members.items(),
                    key=lambda kv: (-len(kv[1]), kv[0]))[:limit_families]
    for root_hash, members in ranked:
        n0 = nodes.get(root_hash, {})
        lines.append(f"## 族 {root_hash[:12]}（{len(members)} 节点，"
                     f"根 origin={n0.get('origin') or '?'}，"
                     f"边来源={n0.get('edge_source')}）")
        for m in sorted(members, key=lambda h: nodes[h].get("ts") or ""):
            n = nodes[m]
            tail = (f" → {n.get('status')}"
                    if n.get("status") not in (None, "frontier") else "")
            pfx = f"{n.get('parent', '')[:12]} --{n.get('op') or '?'}--> " \
                if n.get("parent") else "根 "
            lines.append(f"- {pfx}{m[:12]} [{n.get('ts', '')[:16]}] "
                         f"lane={n.get('lane') or '?'}{tail}")
        lines.append("")
    text = "\n".join(lines)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    return text
