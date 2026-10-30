# coding=utf-8
"""引擎 loop 三原语判定层（continue / expand / rescue，2026-10-02 总任务书任务 A）。

设计公理（PLAN-unified-loop-deadline-prompt-2026-10-02 §0.3）：
- 提议权归模型，续行权归引擎：内容判断（假设/构造/归因）模型全权；
  续行判断（够了/完了/没了）模型零权。
- push 与图配合：何时推=机械（回合边界/停滞/死线），推什么=图算
  （三类边查询+统一排序）。状态机=图的投影。
- 永续不变量：除死线与用户指令外无任何终态机会。回退链结构性保证：
  expand 前沿空→井口→随机生成（无限）。
- 事实而非训令：状态页=账目+地图+水情；指令=坐标+为什么。

三原语：
- continue（欠账存在性；最高优先级）：四类欠账任一存在即触发，最旧者
  优先。C1 结算 / C2 派发 / C3 轮询 / C4 记录。
- expand（前沿×排序；无欠账时）：E1 前沿枚举（派生图变体方向 − 已试
  节点 ∧ 家族预算>0）→ E2 统一排序（调度器五项分，ε 底保留）→
  E3 载荷（坐标+分数分解+同族已证伪兄弟+预算余量+平台事实）。
  E4 空前沿回退：井口水情句 + 三源轮转（literature→random→mechanism，
  lit_search_count%3 确定性）。
- rescue（条件成熟）：R1 再定价 / R2 组合 / R3 孵化判负。push 计算时
  顺带全查。

影子纪律（任务书 §四.4）：新判定上线只记账（primitives_shadow.jsonl），
指令仍由旧阶梯（bridge._pick_strategy）产出；对账新旧差异=迁移映射表
（§A.2）所列，超出即 bug；回退=配置开关 loop_primitives.exec=false。

红线：本模块零调用 lane_activity_tick（记账不是挖掘活动）；零计价/
门禁/图本体写入；一切入口吃异常返回 None——挂点零影响 loop 主路径
（I5 关闭等价）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SHADOW_FILE = "primitives_shadow.jsonl"

#: 配置缺省（MINING_CONFIG["loop_primitives"] 的缺省体；mining_state 同名
#: 键覆盖——与 scheduler 同一覆盖口径）。时间阈值初值，影子期标定。
PRIMITIVES_DEFAULTS: dict[str, Any] = {
    "enabled": True,           # 影子记账开关（纯记账，默认开）
    "exec": False,             # 指令产出接管开关（切换期用；默认关=旧阶梯）
    "c2_dispatch_gap_s": 900,  # C2：声明 pending 后无评估派发判窗（15min）
    "c3_poll_gap_s": 300,      # C3：本线在途作业未被轮询判窗（5min）
    "c4_record_gap_s": 600,    # C4：引擎评估未被叙事 trail 追上判窗（10min）
    "escalate_after_pushes": 3,  # 防复读护栏：同欠账推 N 次未消账→升级告警（不停推）
    "tick_min_interval_s": 60,   # 影子记账节流（读侧便宜，比调度器 tick 细）
    "drift_window": 20,          # 漂移报警：本线最近 N 试验窗口
    "drift_circle_min_cluster": 8,  # 绕圈：最大同构簇规模阈值
    "frontier_top_n": 10,        # E1 前沿枚举的节点上限（对齐调度器遥测预算纪律）
}


def primitives_config(mining_state: dict | None = None) -> dict:
    """合并配置：PRIMITIVES_DEFAULTS ← mining_state["loop_primitives"] 覆盖。"""
    cfg = json.loads(json.dumps(PRIMITIVES_DEFAULTS))
    over = (mining_state or {}).get("loop_primitives")
    if isinstance(over, dict):
        for k, v in over.items():
            cfg[k] = v
    return cfg


def _parse_ts(s: Any) -> float | None:
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _fmt_age(sec: float | None) -> str:
    if sec is None:
        return "?"
    return f"{int(max(sec, 0) // 60)}min"


# ---------------------------------------------------------------- C 类欠账

def scan_debts(ctx: dict) -> list[dict]:
    """四类欠账扫描（纯读；返回按「最旧者优先」排序）。

    ctx 需含：state_root / lane / now / pending / engine_own / agent_trail /
    growth_ts / running_jobs（[(job_id, t0)]）/ last_poll_ts。
    每条欠账 {code, subject, age_s, settle（消账动作）, directive（指令要点）}。
    """
    out: list[dict] = []
    root = ctx["state_root"]
    now = float(ctx.get("now") or time.time())

    # C1 结算：journal 里镜像已出未结算的条目（adjudicated 待结算——镜像
    # hook 在评估完成时把 registered 机械翻转为 adjudicated 并注「待结算」；
    # 以及镜像漏翻的 registered∧已评估残余）。消账=factor_journal_update
    # →resolved/stale。
    try:
        from .journal import _lane_dir, resolve_journal_lane, split_entries
        d = _lane_dir(root, resolve_journal_lane())
        lp = d / "live.md"
        if lp.exists():
            eval_hashes = {e.get("source_hash") for e in ctx.get("engine_own")
                           or [] if isinstance(e, dict) and e.get("source_hash")}
            for e in split_entries(lp.read_text(encoding="utf-8")):
                p = e.get("parsed")
                if not p:
                    continue
                st = p["status"]
                unsettled = (st == "adjudicated") or (
                    st == "registered" and e.get("ref") in eval_hashes)
                if not unsettled:
                    continue
                ts = _parse_ts(e.get("ts"))
                age = (now - ts) if ts is not None else None
                out.append({
                    "code": "C1", "subject": f"journal:{p['id']}",
                    "age_s": age,
                    "settle": "factor_journal_update → resolved/stale（写明实测教了什么）",
                    "directive": (f"推理日志条目 {p['id']}（{p['title'][:40]}）的"
                                  "预期已对上实测但尚未结算——用 factor_journal_update"
                                  " 结算（resolved/stale）并写一句结论"),
                })
    except Exception:
        pass

    # C2 派发：loop pending 非空 ∧ 声明构造后超过判窗无 evaluate/causality
    # 派发。活动轴代理 = 增长时钟（试验落账/评估派发任一刷新 growth_ts）。
    try:
        pending = ctx.get("pending")
        if pending:
            gap = float(ctx.get("c2_dispatch_gap_s",
                                PRIMITIVES_DEFAULTS["c2_dispatch_gap_s"]))
            gts = ctx.get("growth_ts")
            if isinstance(gts, (int, float)):
                age = now - float(gts)
                if age > gap:
                    out.append({
                        "code": "C2", "subject": f"pending:{str(pending)[:40]}",
                        "age_s": age,
                        "settle": "派发 evaluate/causality（cache-miss 派发刷新活动轴）",
                        "directive": (f"上一轮声明的下一步假设已超过 "
                                      f"{int(gap // 60)} 分钟没有"
                                      "对应的评估派发——把该假设落到具体因子源码并"
                                      "提交 factor_check_causality / factor_evaluate，"
                                      "或用 factor_record_explored 明确证伪"),
                    })
    except Exception:
        pass

    # C3 轮询：本线异步作业 running/queued ∧ 距最后一次 job_id 轮询超判窗。
    # last_poll_ts 由 bridge._async_result 维护（本进程内）；重启后未知 →
    # 以最旧在途作业的启动时刻为参考（fresh 作业不算欠账）。
    try:
        jobs = [(str(j), float(t)) for j, t in (ctx.get("running_jobs") or [])
                if isinstance(t, (int, float))]
        if jobs:
            gap = float(ctx.get("c3_poll_gap_s",
                                PRIMITIVES_DEFAULTS["c3_poll_gap_s"]))
            lpt = ctx.get("last_poll_ts")
            ref = float(lpt) if isinstance(lpt, (int, float)) else min(
                t for _, t in jobs)
            age = now - ref
            if age > gap:
                oldest = min(jobs, key=lambda x: x[1])[0]
                out.append({
                    "code": "C3", "subject": f"job:{str(oldest)[:16]}",
                    "age_s": age,
                    "settle": "带 job_id 调 factor_evaluate 取结果（完成即入账）",
                    "directive": (f"本线有 {len(jobs)} 个在途异步作业已超过 "
                                  f"{int(gap // 60)} 分钟"
                                  "未被轮询——把 job_id 传回 factor_evaluate 取结果"
                                  "（running/queued=未完成；同参数重交会自动并入"
                                  "同一作业，不会重复计算）"),
                })
    except Exception:
        pass

    # C4 记录：引擎 trail 本 lane 最新评估 − 叙事 trail 最新条目 >10min。
    try:
        eng_ts = None
        for e in reversed(ctx.get("engine_own") or []):
            if isinstance(e, dict) and e.get("ts"):
                eng_ts = _parse_ts(e.get("ts"))
                if eng_ts is not None:
                    break
        nar_ts = None
        for e in reversed(ctx.get("agent_trail") or []):
            if isinstance(e, dict) and e.get("ts"):
                nar_ts = _parse_ts(e.get("ts"))
                if nar_ts is not None:
                    break
        if eng_ts is not None:
            lag = eng_ts - (nar_ts if nar_ts is not None else 0.0)
            gap = float(ctx.get("c4_record_gap_s",
                                PRIMITIVES_DEFAULTS["c4_record_gap_s"]))
            if lag > gap:
                out.append({
                    "code": "C4", "subject": "trail_lag",
                    "age_s": lag,
                    "settle": "factor_record_trail 补一条完整叙事条目",
                    "directive": (f"引擎已落账的评估比叙事日志落后超过 "
                                  f"{int(gap // 60)} 分钟——"
                                  "用 factor_record_trail 把最近试验（信号/评估/"
                                  "归因/下一步假设）补记成完整条目"),
                })
    except Exception:
        pass

    out.sort(key=lambda d: (d.get("age_s") is None, -(d.get("age_s") or 0)))
    return out


# ---------------------------------------------------------------- E1 前沿枚举

def enumerate_frontier(root, family_streak: int, max_cluster_trials: int,
                       lane: str | None = None,
                       top_n: int | None = None) -> dict:
    """E1：派生图逐家族的变体方向格点 − 已试节点，家族预算>0 才入列。

    变体方向格点（v1 = 算子轴）：scheduler.legal_ops(parent_source) 给出
    该节点合法变换轴；已试节点 = 图上该父节点已有同 op 子边。家族预算
    继承 max_cluster_trials 语义（图化）：本方向段同族链长 vs 上限。

    top_n：前沿节点上限（最新 ts 优先）——与调度器遥测预算同纪律，防
    大图上每 tick 的源码读取与打分无界膨胀；None=不限（测试用）。

    返回 {candidates: [{parent, op, family, source_in_store}],
    budget_left, frontier_n, note}。
    """
    from . import derivation, scheduler
    nodes = derivation.load_nodes(root)
    children = derivation.load_children(root)
    budget_left = max(0, int(max_cluster_trials) - int(family_streak or 0))
    frontier_all = [(h, n) for h, n in nodes.items()
                    if n.get("status") in (None, "frontier")
                    and (not lane or n.get("lane") in (None, lane))]
    if budget_left <= 0:
        # 家族预算耗尽（继承 max_cluster_trials 语义，图化）：当前方向段
        # 的同族试验配额已用完——前沿对 expand 为空，E4 回退接管
        return {"candidates": [], "budget_left": 0,
                "frontier_n": len(frontier_all),
                "note": "家族预算耗尽——前沿空，E4 回退（换源供数）接管"}
    if top_n is not None and len(frontier_all) > top_n:
        frontier_all.sort(key=lambda hn: str(hn[1].get("ts") or ""))
        frontier_all = frontier_all[-top_n:]
    cands: list[dict] = []
    for h, n in frontier_all:
        try:
            from .state import factor_source_path
            sp = factor_source_path(h, root)
            src = sp.read_text(encoding="utf-8") if sp and sp.exists() else None
        except Exception:
            src = None
        ops = scheduler.legal_ops(src)
        used = {nodes[c].get("op") for c in children.get(h, [])
                if c in nodes}
        for op in ops:
            if op in used:
                continue
            cands.append({
                "parent": h, "op": op,
                "family": derivation.family_root(root, h, nodes) if h in nodes
                else h,
                "source_in_store": src is not None,
            })
    return {
        "candidates": cands,
        "budget_left": budget_left,
        "frontier_n": len([1 for n in nodes.values()
                           if n.get("status") in (None, "frontier")]),
        "note": ("变体方向=合法变换轴（drop_leg/reweight 多腿因子专用；"
                 "add_leg/winsorize/regime_condition/compose_with/lag_decay/"
                 "deepen_variant 通用）−该父节点已试轴；家族预算="
                 "max_cluster_trials−本方向段同族链长；节点取最新 "
                 f"{top_n if top_n is not None else '∞'} 个"),
    }


# ---------------------------------------------------------------- E2 排序

def rank_candidates(root, candidates: list[dict], cfg: dict,
                    top_k: int = 5) -> list[dict]:
    """E2：调度器五项分对全体候选统一排序（含 rescue 成熟项由调用方合入）。

    ε 底保留：排序只决定「先推哪个」，ε 份额（模型可带变奏并声明，
    偏离落账喂排序器）不因排序靠后而被剥夺——载荷中如实说明。
    """
    from . import derivation, scheduler
    if not candidates:
        return []
    events = derivation.load_events(root)
    scfg = _scheduler_cfg(root)
    tick_key = f"primitives:{int(time.time())}"
    scored = []
    for c in candidates:
        node = {"hash": c["parent"]}
        sctx = scheduler._node_ctx(None, node, [], events, scfg)
        terms = scheduler.score_node(node, sctx, events, scfg,
                                     f"{tick_key}:{c['parent']}:{c['op']}")
        entry = dict(c)
        entry["score"] = terms
        scored.append(entry)
    ranked = sorted(scored, key=lambda e: (
        e["score"].get("A") is None,
        -(e["score"].get("A") or 0.0)))
    for i, e in enumerate(ranked):
        e["rank"] = i
    return ranked[:top_k]


def _scheduler_cfg(root) -> dict:
    from . import scheduler
    from .state import read_mining_state
    return scheduler.scheduler_config(read_mining_state(root))


# ---------------------------------------------------------------- E3 载荷

def expand_payload(top: dict, fam_marg: dict | None, plateau: bool,
                   budget_left: int, engine_trail: list,
                   events: list | None = None) -> dict:
    """E3：expand 指令载荷=坐标+分数分解+同族已证伪兄弟+预算余量+平台事实。

    坐标 = 根哈希（族根）+ trail 条目号（父节点最新评估在引擎 trail 的
    序号）+ 变体方向（op）。平台事实=最近 20 条未超族最佳 0.05。
    """
    parent = top["parent"]
    fam = top.get("family") or parent
    trail_no = None
    for i, e in enumerate(engine_trail):
        if isinstance(e, dict) and e.get("source_hash") == parent:
            trail_no = i  # 取最新一次
    siblings = []
    for e in engine_trail:
        if not isinstance(e, dict):
            continue
        h = e.get("source_hash")
        if not h or h == parent:
            continue
        # 同族已证伪：expand 事件同 family ∧ 引擎判定 fail/reject
        if events is not None and not any(
                ev.get("ev") == "expand" and ev.get("child") == h
                and ev.get("family") == fam for ev in events):
            continue
        if e.get("verdict") in ("fail", "reject"):
            siblings.append({"hash": str(h)[:12], "verdict": e.get("verdict"),
                             "ic_ir": e.get("ic_ir")})
    m = fam_marg or {}
    return {
        "coordinate": {
            "root_hash": str(fam)[:12],
            "trail_entry": trail_no,
            "variant_direction": top["op"],
            "parent_hash": str(parent)[:12],
        },
        "score_breakdown": {k: top.get("score", {}).get(k)
                            for k in ("A", "Q", "U", "D", "E", "C")},
        "falsified_siblings": siblings[-5:],
        "budget_left": budget_left,
        "plateau": {
            "flag": bool(plateau or (m.get("enough_data")
                                     and isinstance(m.get("marginal"),
                                                    (int, float))
                                     and m["marginal"] < -0.05)),
            "detail": (f"最近窗口边际 {m.get('marginal')}，族最佳 "
                       f"{m.get('family_best')}，{m.get('plateau_trials')}"
                       " 个试验未刷新" if m.get("enough_data")
                       else "族内样本 <5，不判定"),
        },
        "epsilon_note": ("排序只定先后：你可以带自己的变奏并声明（落 trail"
                         " 即喂排序器），排序靠后不剥夺变奏权"),
    }


# ---------------------------------------------------------------- R 类成熟检查

def check_rescue(bridge, ctx: dict) -> dict:
    """R1/R2/R3 成熟检查（push 计算时顺带全查；全部纯读）。

    R1 再定价：近失/候补条目 ∧ 现价 deflated p 自评估时点后下移
    （墙或池参数变了——当初差一点，现在可能够了）。
    R2 组合：候补池内存在两两 |ρ|<0.3 的 k 只 ∧ 规则 dof 可接受
    （无未消费的同组合点名）。
    R3 孵化判负：cohort 窗满 ∧ 环境新数据 ≥ window_days。
    """
    root = Path(ctx["state_root"])
    out: dict = {"r1": [], "r2": None, "r3": []}
    # ---- R1：现价重算的近失队列 vs 评估时点 p_def 快照 ----
    try:
        from . import derivation, scheduler
        queue = (ctx.get("deepen_tel") or {}).get("queue") or []
        events = derivation.load_events(root)
        for row in queue:
            h = row.get("hash")
            e = row.get("entry") if isinstance(row, dict) else None
            if not h or not isinstance(e, dict):
                continue
            snap = scheduler._latest_snapshot(events, h)
            p_now = row.get("p") if isinstance(row.get("p"), (int, float)) \
                else None
            p_then = (snap or {}).get("p_def")
            if (isinstance(p_now, (int, float))
                    and isinstance(p_then, (int, float)) and p_then > 0
                    and p_now < float(p_then) - 1e-9):
                out["r1"].append({
                    "hash": str(h)[:12],
                    "missed_gate": ("deflated p（多重检验校正后显著性）"
                                    f"当初 {float(p_then):.3f} > 0.05"),
                    "missed_by": round(float(p_then) - 0.05, 3),
                    "changed": (f"现价重算 p 已降到 {float(p_now):.3f}"
                                "（墙/池参数重校准）"),
                })
    except Exception:
        pass
    # ---- R2：候补池正交组 dry-run（引擎选好成员；不派发评估）----
    try:
        from . import standby
        probe = standby.combine(root, k=4, ortho_th=0.3, min_abs_sr=0.75)
        if probe.get("ok"):
            comp = str(probe.get("composite_hash"))
            fired = _r2_fired_list(root, ctx.get("lane"))
            pending_keys = _pending_pick_keys(root)
            if (comp[:12] not in fired
                    and not any(comp[:12] in k for k in pending_keys)):
                out["r2"] = {
                    "members": probe.get("members"),
                    "composite_hash": comp[:12],
                    "rule": probe.get("rule"),
                    "consumption_key": f"pick:{comp[:12]}:{int(time.time())}",
                }
    except Exception:
        pass
    # ---- R3：孵化窗满 ∧ 环境新数据足 ----
    try:
        from . import incubate
        cohorts = incubate._read_cohorts(root)
        env = _primary_env(bridge)
        if env is not None:
            import pandas as pd
            dates = pd.DatetimeIndex(env.dates)
            for c in cohorts:
                if c.get("status") != "open":
                    continue
                start = c.get("start_date") or str(c.get("ts", ""))[:10]
                n_fresh = int((dates >= pd.Timestamp(start)).sum())
                need = int(c.get("window_days") or 120)
                if n_fresh >= need:
                    out["r3"].append({
                        "cohort_id": c.get("id"),
                        "fresh_days": n_fresh, "window_days": need,
                        "n_members": len(c.get("members") or []),
                    })
    except Exception:
        pass
    return out


def _pending_pick_keys(root) -> list[str]:
    try:
        from . import scheduler
        st = scheduler.read_scheduler_state(root)
        return [str(p.get("key")) for p in (st.get("pending_picks") or [])
                if isinstance(p, dict)]
    except Exception:
        return []


def _r2_fired_list(root, lane) -> list[str]:
    """已发射过 R2 组合点名的 composite hash 列表（边沿触发纪律：同一
    组合只提示一次——继承 compose_fired 定律：电平触发+会变键=重注机）。
    注意与 _after_emit 同层：写在**线桶**级（lanes.<lane>.prim_r2_fired），
    不是桶内嵌的 drive_bk 子字典。"""
    try:
        from .state import read_mining_state
        st = read_mining_state(root)
        bucket = (st.get("lanes") or {}).get(lane or "default") or {}
        v = bucket.get("prim_r2_fired")
        return list(v) if isinstance(v, list) else []
    except Exception:
        return []


def _after_emit(ctx, decision: dict) -> None:
    """exec 指令发射后的引擎侧边沿消费——**键幂等**（review 修复）：
    同一指令键只消费一次（登记优先：先记键后消费，崩溃最坏丢一次消费、
    绝不双计配额）。覆盖四类边沿：
    - R2 组合点名：同组合 hash 不重挂（prim_r2_fired）
    - query 里程碑：lane 桶 milestone 记账（同旧阶梯消费语义）
    - random/mechanism：发射配额记账（同旧阶梯）
    非边沿类型（continue/expand/E4）零消费——C 欠账的推动计数在
    _bump_push_count（另一键空间）。
    """
    if not isinstance(decision, dict):
        return
    key = str(decision.get("key") or "")
    if not key:
        return
    t = decision.get("type")
    is_r2 = decision.get("code") == "R2"
    if t not in ("query", "random", "mechanism") and not is_r2:
        return
    root = Path(ctx["state_root"])
    lane = ctx.get("lane")
    # 键幂等登记（独立小事务）
    try:
        from .filelock import state_write_lock
        from .state import _atomic_write_json, _lane_bucket_mut, _path
        with state_write_lock(root):
            st, bucket = _lane_bucket_mut(root, lane)
            consumed = bucket.setdefault("prim_consumed_keys", [])
            if key in consumed:
                return
            consumed.append(key)
            consumed[:] = consumed[-200:]
            _atomic_write_json(_path("mining_state", root), st)
    except Exception:
        return  # 登记失败→不消费（宁漏一次不双计）
    # 消费动作（各公共记账入口自持锁）
    try:
        if t == "query":
            from .state import lane_drive_bk_update
            lane_drive_bk_update(root, lane,
                                 milestone=int(ctx.get("accepted_n") or 0))
        elif t == "random":
            from .state import random_emit_count
            random_emit_count(root, lane, int(ctx.get("agent_rounds") or 0))
        elif t == "mechanism":
            from .state import mechanism_emit_count
            mechanism_emit_count(root, lane,
                                 int(ctx.get("agent_rounds") or 0))
        elif is_r2:
            comp = key.split(":")[1] if ":" in key else ""
            if comp:
                with state_write_lock(root):
                    st, bucket = _lane_bucket_mut(root, lane)
                    fired = bucket.setdefault("prim_r2_fired", [])
                    if comp not in fired:
                        fired.append(comp)
                        fired[:] = fired[-50:]
                    _atomic_write_json(_path("mining_state", root), st)
    except Exception:
        pass


def _primary_env(bridge):
    try:
        envs = getattr(bridge, "envs", None) or {}
        env_id = next(iter(envs), None)
        if env_id is None:
            return None
        e = envs[env_id]
        return e.get("env") if isinstance(e, dict) else e
    except Exception:
        return None


# ---------------------------------------------------------------- 井口/穷尽/漂移

def water_situation(bridge, ctx: dict) -> dict:
    """井口水情：近失/候补/孵化待判/农场新种/文献新鲜——地图的事实层。"""
    root = Path(ctx["state_root"])
    out: dict = {}
    try:
        out["near_miss_n"] = len((ctx.get("deepen_tel") or {}).get("queue")
                                 or [])
    except Exception:
        out["near_miss_n"] = None
    try:
        from . import standby
        out["standby_n"] = standby.standby_view(root, top_n=1)["count_family_dedup"]
    except Exception:
        out["standby_n"] = None
    try:
        from . import incubate
        out["incubation_open_n"] = sum(
            1 for c in incubate._read_cohorts(root)
            if c.get("status") == "open")
    except Exception:
        out["incubation_open_n"] = None
    try:
        from . import derivation
        cutoff = time.time() - 86400
        out["farm_new_n"] = sum(
            1 for n in derivation.load_nodes(root).values()
            if n.get("edge_source") == "farm_seed"
            and (_parse_ts(n.get("ts")) or 0) >= cutoff)
    except Exception:
        out["farm_new_n"] = None
    try:
        papers = bridge._read_papers()
        seen = papers.get("papers") if isinstance(papers, dict) else None
        if isinstance(seen, list):
            out["arxiv_fresh_n"] = sum(
                1 for p in seen if isinstance(p, dict)
                and not p.get("exhausted"))
        else:
            out["arxiv_fresh_n"] = None
    except Exception:
        out["arxiv_fresh_n"] = None
    return out


def exhaustion_check(debts: list, frontier: dict, rescue: dict,
                     water: dict, pending) -> dict:
    """穷尽谓词：C 空 ∧ E1 空 ∧ R1-R3 空 ∧ 井干 → 告警用户（不停机不静默）。

    井干 = 近失空 ∧ 候补空 ∧ 孵化无待判 ∧ 无未消化假设。**None=未知不
    算干**（水情子查询失败时按保守方向处理——不触发穷尽告警）。E4 回退
    链仍在（文献/随机不依赖井），告警语义=「建议数据集轮换」而非停机。
    """
    dry = (water.get("near_miss_n") == 0
           and water.get("standby_n") == 0
           and water.get("incubation_open_n") == 0
           and pending is None)
    exhausted = (not debts and not (frontier.get("candidates") or [])
                 and not rescue.get("r1") and rescue.get("r2") is None
                 and not rescue.get("r3") and dry)
    if not exhausted:
        return {"exhausted": False}
    return {
        "exhausted": True,
        "alarm": ("全部续行来源为空：无欠账、派生图前沿空、三条复活通道"
                  "（再定价/组合/孵化判负）无一成熟、井口干涸（无近失、"
                  "无候补、无待判孵化、无未消化假设）。引擎不会停机——"
                  "三源轮转（文献/随机/自提机制）照常供数；但本面板的"
                  "信息增量已挖尽，建议用户做数据集轮换"),
        "suggestion": "数据集轮换（用户操作）",
    }


def drift_alarms(engine_own: list, events: list | None = None,
                 window: int = 20, min_cluster: int = 8) -> list[dict]:
    """双向漂移报警（图度量，影子期标定；只红字不停顿）。

    ① 绕圈：本 lane 最近 20 试验按构造指纹聚簇，最大簇 ≥8 且产出零
    （无 pass/入册）。
    ② 草率收弧：连续 3 段同族链试验数严格递减且低于历史中位一半
    （弧≈同族链段；链断=弧边界）。
    ③ 家族产出率：滑窗（最近 window 条）零产出 ∧ 该家族预算消耗 ≥50%。
    """
    out: list[dict] = []
    try:
        trials = [e for e in engine_own if isinstance(e, dict)
                  and e.get("source_hash")]
        # ① 绕圈（构造指纹聚类）
        tail = trials[-window:]
        by_fp: dict[tuple, list[dict]] = {}
        for e in tail:
            fp = e.get("construction_fp")
            key = tuple(fp) if isinstance(fp, list) and fp else (
                str(e.get("source_hash")),)
            by_fp.setdefault(key, []).append(e)
        if by_fp:
            biggest = max(by_fp.values(), key=len)
            if len(biggest) >= min_cluster and not any(
                    e.get("verdict") == "pass" for e in biggest):
                out.append({
                    "code": "D-circle", "severity": "red",
                    "detail": (f"最近 {len(tail)} 个试验中 {len(biggest)} 个"
                               "构造同构且零产出——疑似绕圈（重复试已证伪的"
                               "路径）。建议：factor_query_paths("
                               "layer='trail_similarity') 查重后再构造"),
                })
        # ②③ 弧段与家族产出率（同族链段 = 弧）
        fam_seq: list[tuple[str, int, int]] = []  # (family, n, pass_n)
        cur_fam, cur_n, cur_pass = None, 0, 0
        fam_of = {}
        if events:
            for ev in events:
                if ev.get("ev") == "expand" and ev.get("child"):
                    fam_of[ev["child"]] = ev.get("family") or ev.get("hash")
        for e in trials:
            fam = fam_of.get(e.get("source_hash")) or "?"
            if fam != cur_fam:
                if cur_fam is not None:
                    fam_seq.append((cur_fam, cur_n, cur_pass))
                cur_fam, cur_n, cur_pass = fam, 0, 0
            cur_n += 1
            cur_pass += 1 if e.get("verdict") == "pass" else 0
        if cur_fam is not None:
            fam_seq.append((cur_fam, cur_n, cur_pass))
        if len(fam_seq) >= 3:
            lens = [x[1] for x in fam_seq]
            med = sorted(lens)[len(lens) // 2]
            last3 = lens[-3:]
            strictly_dec = all(last3[i] > last3[i + 1]
                               for i in range(len(last3) - 1))
            if strictly_dec and med > 0 and all(x < med / 2 for x in last3):
                out.append({
                    "code": "D-hasty-arc", "severity": "red",
                    "detail": (f"最近 3 段同族链试验数 {last3} 严格递减且低于"
                               f"中位 {med} 的一半——疑似草率收弧（方向还没"
                               "挖透就换向）。建议：对当前族补足变体轴再断链"),
                })
        # ③ 家族产出率（最近 window 条零 pass ∧ 当前家族消耗过半预算）
        if tail and not any(e.get("verdict") == "pass" for e in tail):
            cur_family_n = fam_seq[-1][1] if fam_seq else 0
            from .state import MINING_CONFIG
            budget = int(MINING_CONFIG.get("max_cluster_trials", 200))
            if cur_family_n >= budget * 0.5:
                out.append({
                    "code": "D-family-yield", "severity": "red",
                    "detail": (f"最近 {len(tail)} 个试验零产出且当前家族已消耗"
                               f"{cur_family_n}/{budget} 预算——建议把该家族"
                               "移出前沿（rescue/换源），预算留给新族"),
                })
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 影子 tick

#: 节流缓存——键 (root, lane)：同账本双线并行时互不串页（review 修复 2）
_LAST_TICK: dict[tuple, float] = {}
_cached_page: dict[tuple, dict] = {}


def shadow_tick(bridge, ctx: dict) -> dict | None:
    """三原语影子 tick（挂 bridge._loop_directive，调度器 tick 之后）。

    计算三原语判定 + 三段式状态页（账目/地图/指令坐标），写一行对账日志
    primitives_shadow.jsonl（新旧对照）。exec=true 时同时返回接管指令
    （decide()）；exec=false（默认）指令仍由旧阶梯产出——逐比特现状。
    任何失败吃掉返回 None（挂点零影响）。

    节流语义：每 (root, lane) 至多每 tick_min_interval_s 重算一次；节流
    窗内返回**页拷贝**（exec 模式从缓存 decision 重新附挂接管指令——
    调用方 pop 不会破坏缓存；边沿消费只在全量重算时发生，键幂等防双计）。
    """
    try:
        return _shadow_impl(bridge, ctx)
    except Exception as e:
        try:
            import sys
            sys.stderr.write(f"[primitives] tick 失败(不阻断): {e!r}\n")
        except Exception:
            pass
        return None


def _shadow_impl(bridge, ctx: dict) -> dict | None:
    from .state import read_mining_state
    root = Path(ctx["state_root"])
    mining = read_mining_state(root)
    cfg = primitives_config(mining)
    if not cfg.get("enabled"):
        return None
    now = float(ctx.get("now") or time.time())
    key = (str(root.resolve()), str(ctx.get("lane") or "default"))
    if now - _LAST_TICK.get(key, 0.0) < float(cfg["tick_min_interval_s"]):
        c = _cached_page.get(key)
        if c is None:
            return None
        out = dict(c["page"])
        d = c.get("decision")
        if d is not None:
            out["policy"] = "exec"
            out["directive_coordinate"] = _coord_of(d)
            out["exec_strategy"] = d
        return out
    _LAST_TICK[key] = now

    ctx = dict(ctx)
    ctx.update({
        "c2_dispatch_gap_s": cfg["c2_dispatch_gap_s"],
        "c3_poll_gap_s": cfg["c3_poll_gap_s"],
        "c4_record_gap_s": cfg["c4_record_gap_s"],
    })
    debts = scan_debts(ctx)
    frontier = enumerate_frontier(
        root, int(ctx.get("streak") or 0),
        int(ctx.get("max_cluster_trials") or 200), ctx.get("lane"),
        top_n=int(cfg.get("frontier_top_n") or 10))
    ranked = rank_candidates(root, frontier.get("candidates") or [],
                             cfg, top_k=5) if not debts else []
    rescue = check_rescue(bridge, ctx)
    water = water_situation(bridge, ctx)
    exh = exhaustion_check(debts, frontier, rescue, water, ctx.get("pending"))
    from . import derivation
    drift = drift_alarms(
        ctx.get("engine_own") or [], derivation.load_events(root),
        window=int(cfg["drift_window"]),
        min_cluster=int(cfg["drift_circle_min_cluster"]))

    page = _status_page(bridge, ctx, cfg, debts, frontier, ranked, rescue,
                        water, exh, drift)
    decision = None
    if cfg.get("exec"):
        decision = decide(bridge, ctx, cfg, debts, frontier, ranked, rescue,
                          exh, water)
    # 缓存只存「基页 + decision」——exec_strategy 挂在返回拷贝上，调用方
    # pop 不改缓存（review 修复 4：节流回放不再回落旧阶梯）
    _cached_page[key] = {"page": page, "decision": decision}
    out = dict(page)
    if decision is not None:
        out["policy"] = "exec"
        out["directive_coordinate"] = _coord_of(decision)
        out["exec_strategy"] = decision
        _after_emit(ctx, decision)
    # 对账日志（新旧对照；对账口径=任务书 A.2 迁移映射表，超出即 bug）
    old = ctx.get("strategy") or {}
    try:
        with open(root / SHADOW_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "lane": ctx.get("lane"), "policy": out["policy"],
                "old_ladder": {"type": old.get("type"),
                               "key": old.get("key")},
                "primitive": ({"kind": decision["kind"],
                               "code": decision.get("code"),
                               "key": decision.get("key")}
                              if decision else _shadow_choice(
                                  debts, ranked, rescue, exh, frontier)),
                "debts_n": len(debts), "frontier_n": frontier["frontier_n"],
                "rescue_n": (len(rescue.get("r1") or [])
                             + (1 if rescue.get("r2") else 0)
                             + len(rescue.get("r3") or [])),
                "exhausted": exh.get("exhausted", False),
                "drift": [d["code"] for d in drift],
            }, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    return out


def _shadow_choice(debts, ranked, rescue, exh, frontier) -> dict:
    """影子期不产出指令，但记录「新判定会选什么」供对账。"""
    if debts:
        d = debts[0]
        return {"kind": "continue", "code": d["code"],
                "subject": d["subject"]}
    if rescue.get("r1"):
        return {"kind": "rescue", "code": "R1"}
    if rescue.get("r2"):
        return {"kind": "rescue", "code": "R2"}
    if rescue.get("r3"):
        return {"kind": "rescue", "code": "R3"}
    if ranked:
        return {"kind": "expand", "code": "E3",
                "parent": ranked[0]["parent"][:12], "op": ranked[0]["op"]}
    return {"kind": "fallback", "code": "E4"}


def _status_page(bridge, ctx, cfg, debts, frontier, ranked, rescue, water,
                 exh, drift) -> dict:
    """三段式状态页（图投影；受自足性铁律约束——账目/地图/指令坐标）。"""
    from . import scheduler
    n_trials = int(ctx.get("n_trials") or 0)
    return {
        "policy": "shadow" if not cfg.get("exec") else "exec",
        "lane": ctx.get("lane"),
        "account": {
            "trials_global": n_trials,
            "trials_lane": int(ctx.get("n_trials_lane") or 0),
            "wall_b_N": round(scheduler.b_wall(max(n_trials, 1)), 3),
            "family_budget_left": frontier.get("budget_left"),
            "arc_rounds": f"{ctx.get('arc', 0)}/{ctx.get('max_rounds', '?')}",
        },
        "map": {
            "frontier_top": [
                {"parent": c["parent"][:12], "op": c["op"],
                 "A": (c.get("score") or {}).get("A")}
                for c in ranked[:3]],
            "debts": [{"code": d["code"], "subject": d["subject"],
                       "age": _fmt_age(d.get("age_s"))} for d in debts],
            "water": water,
        },
        "directive_coordinate": _coord_of(ctx.get("strategy") or {}),
        "alarms": ([exh] if exh.get("exhausted") else []) + drift,
        "note": ("三原语影子记账（指令仍由现行阶梯产出；新旧对照见 "
                 f"{SHADOW_FILE}，对账口径=迁移映射表）；欠账最旧者优先，"
                 "无欠账才 expand，rescue 成熟即插队"),
    }


def _coord_of(strategy: dict) -> dict:
    if not strategy:
        return {}
    return {"type": strategy.get("type"), "key": strategy.get("key"),
            "why": strategy.get("why")}


# ---------------------------------------------------------------- exec 指令产出

def decide(bridge, ctx, cfg, debts, frontier, ranked, rescue, exh,
           water) -> dict | None:
    """exec=true 时的指令产出（迁移映射 §A.2；策略 dict 形状与旧阶梯一致，
    注入器/runner 零改动消费）。

    优先序：灵感重置声明（直通强制版）→ C 欠账（最旧者优先）→ 里程碑
    审计（旧 R6 的 C 类化）→ R1/R2/R3 → E3 前沿载荷 → E4 回退。
    永续：任何分支都不返回 None——E4 回退链结构性存在（随机生成无限）。
    """
    rounds = int(ctx.get("agent_rounds") or 0)
    # 0 灵感重置（agent 声明，直通强制版）
    ir = ctx.get("inspiration_reset")
    if ir is not None:
        from . import bridge as _b
        t = ir.get("mode") if ir.get("mode") in ("random", "mechanism") \
            else "literature"
        return {"type": t, "kind": "fallback",
                "why": f"灵感重置（agent 声明）：{str(ir.get('reason'))[:80]}",
                "key": f"ir:{t}:{rounds}:{int(ctx.get('n_trials') or 0)}",
                "directive": _b._STRATEGY_DIRECTIVES[t]}
    # C 欠账（最旧者优先；防复读护栏=同码推 3 次未消账→升级告警+继续推）
    if debts:
        d = debts[0]
        pushes = _bump_push_count(ctx, d["code"],
                                  {x["code"] for x in debts})
        directive = d["directive"]
        why = f"{d['code']} 欠账（{_fmt_age(d.get('age_s'))}）：{d['subject']}"
        if pushes >= int(cfg.get("escalate_after_pushes", 3)):
            directive += ("。【引擎告警】同一欠账已提示 "
                          f"{pushes} 次未消账——请立即执行上述消账动作；"
                          "引擎会继续提醒直到欠账消除")
            try:
                import sys
                sys.stderr.write(
                    f"[primitives] 防复读护栏：{d['code']} 已推 {pushes} 次"
                    "未消账——升级告警（不停推）\n")
            except Exception:
                pass
        return {"type": "continue", "kind": "continue", "code": d["code"],
                "why": why,
                "key": f"{d['code'].lower()}:{rounds}:{int(ctx.get('n_trials') or 0)}",
                "directive": directive}
    # 里程碑审计（旧 8 号分支的 C 类化：入册数跨 3 倍数边沿触发一次即消费）
    try:
        accepted_n = int(ctx.get("accepted_n") or 0)
        fired = ctx.get("milestone_fired")
        if (accepted_n >= 3 and accepted_n % 3 == 0 and accepted_n != fired):
            from . import bridge as _b
            s = {"type": "query",
                 "why": f"已入册 {accepted_n} 个因子——里程碑正交性审计（一次即消费）",
                 "key": f"query:{accepted_n}:{rounds}",
                 "directive": _b._STRATEGY_DIRECTIVES["query"]}
            return {"kind": "continue", "code": "milestone", **s}
    except Exception:
        pass
    # R1 再定价 → R2 组合 → R3 孵化判负
    if rescue.get("r1"):
        r = rescue["r1"][0]
        return {
            "type": "rescue", "kind": "rescue", "code": "R1",
            "why": f"再定价成熟：{r['hash']}（{r['changed']}）",
            "key": f"r1:{r['hash']}:{rounds}",
            "directive": (f"候补因子 {r['hash']} 的显著性现价重算已跨过门槛"
                          f"（{r['missed_gate']}；{r['changed']}）。"
                          "把它按标准管线重新验证（causality→evaluate），"
                          "过线则走 factor_registry_submit——评估时点的拒绝"
                          "不是结论，墙在动，现价说了算"),
        }
    if rescue.get("r2"):
        r = rescue["r2"]
        members = "、".join(str(m.get("hash", ""))[:12]
                            for m in (r.get("members") or []))
        return {
            "type": "rescue", "kind": "rescue", "code": "R2",
            "why": f"组合成熟：候补池存在两两|ρ|<0.3 的正交组（{members}）",
            "key": f"r2:{r['composite_hash']}:{rounds}",
            "directive": ("候补池正交组已成熟（引擎已选好成员："
                          f"{members}；组合规则 dof=1 预注册）。"
                          "触发 factor_standby_combine 让引擎组稿并验证"
                          "组合（评估走标准管线；本组合一次点名即消费，"
                          "不重复提示）"),
        }
    if rescue.get("r3"):
        r = rescue["r3"][0]
        return {
            "type": "rescue", "kind": "rescue", "code": "R3",
            "why": (f"孵化判负成熟：{r['cohort_id']}（新鲜数据 "
                    f"{r['fresh_days']}/{r['window_days']} 交易日已满）"),
            "key": f"r3:{r['cohort_id']}:{rounds}",
            "directive": (f"孵化 cohort {r['cohort_id']} 的前向窗口已满"
                          f"（{r['n_members']} 个成员，{r['window_days']}"
                          "交易日新鲜数据）——执行 factor_incubate_judge "
                          "一次性判决（幸存者可 submit 并豁免开发段"
                          "多重检验校正；未过线的如实入账，候补线拒绝"
                          "记录的是「此刻不够」不是「永远不行」）"),
        }
    # E3 expand 载荷
    if ranked:
        top = ranked[0]
        payload = expand_payload(
            top, ctx.get("fam_marg"), bool(ctx.get("plateau")),
            int(frontier.get("budget_left") or 0),
            ctx.get("engine_trail") or [])
        coord = payload["coordinate"]
        sb = payload["score_breakdown"]
        sib = "、".join(s["hash"] for s in payload["falsified_siblings"]) or "无"
        return {
            "type": "expand", "kind": "expand", "code": "E3",
            "why": (f"前沿最高分：{coord['parent_hash']} 的 "
                    f"{coord['variant_direction']} 方向（A={sb.get('A')}）"),
            "key": (f"expand:{coord['parent_hash']}:{coord['variant_direction']}"
                    f":{rounds}"),
            "directive": (f"下一个试验坐标：在 {coord['parent_hash']}"
                          f"（族根 {coord['root_hash']}，引擎 trail 第 "
                          f"{coord['trail_entry']} 条）上做 "
                          f"{coord['variant_direction']} 变体"
                          f"（统一排序分 A={sb.get('A')}，分解 Q={sb.get('Q')}"
                          f"/U={sb.get('U')}/D={sb.get('D')}/E={sb.get('E')}"
                          f"/C={sb.get('C')}）。同族已证伪兄弟：{sib}"
                          f"（构造前先 factor_query_paths 查重）。"
                          f"家族预算余量 {payload['budget_left']}；"
                          + ("族内已平台（最近窗口未超族历史最佳）——变体"
                             "应换轴而非参数微调。"
                             if payload["plateau"]["flag"] else "")
                          + payload["epsilon_note"]),
        }
    # E4 回退：井口水情句 + 三源确定性轮转（literature→random→mechanism）
    from . import bridge as _b
    lit_n = int(ctx.get("lit_search_count") or 0)
    slot = lit_n % 3
    rnd_ok = bool(ctx.get("random_available"))
    mech_ok = bool(ctx.get("mechanism_available"))
    t = "literature"
    if slot == 1 and rnd_ok:
        t = "random"
    elif slot == 2 and mech_ok:
        t = "mechanism"
    water_txt = "、".join(f"{k}={v}" for k, v in (water or {}).items())
    return {
        "type": t, "kind": "fallback", "code": "E4",
        "why": (f"派生图前沿空（家族预算或已试轴耗尽）——回退到换源供数"
                f"（三源轮转第 {lit_n % 3 + 1} 位）"),
        "key": f"fallback:{t}:{rounds}:{int(ctx.get('n_trials') or 0)}",
        "directive": (f"派生图已无可枚举的变体方向。井口水情：{water_txt}。"
                      + _b._STRATEGY_DIRECTIVES[t]),
    }


def _bump_push_count(ctx, code: str, present_codes: "set[str]") -> int:
    """防复读护栏计数（exec 模式）：同欠账码每推一次 +1；本轮不存在的码
    =已消账，清零。计数落 mining_state 线桶 prim_pushes（跨回合持久；
    影子期不写——由 shadow log 承担观察）。任何失败返回 1（不阻断推送）。
    """
    try:
        from .filelock import state_write_lock
        from .state import (_atomic_write_json, _lane_bucket_mut,
                            _path)
        root = Path(ctx["state_root"])
        lane = ctx.get("lane")
        with state_write_lock(root):
            st, bucket = _lane_bucket_mut(root, lane)
            counts = bucket.setdefault("prim_pushes", {})
            for k in list(counts):
                if k not in present_codes:
                    counts.pop(k)
            counts[code] = int(counts.get(code) or 0) + 1
            n = int(counts[code])
            _atomic_write_json(_path("mining_state", root), st)
            return n
    except Exception:
        return 1
