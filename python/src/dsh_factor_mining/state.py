# coding=utf-8
"""User-owned mining state.

All state (trail, explored paths, search paths, registries, mining state)
lives under a user-supplied state root.  The package directory is never
written to and never contains user data.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .discipline import source_fingerprint
from .filelock import state_write_lock
from .lane import DEFAULT_LANE

DEFAULT_STATE_ROOT = Path.cwd() / ".factor-mining"

#: 内容寻址源码库目录名（外挂库，2026-08-29 需求）：trail 条目已有
#: source_hash 作键，本库存 hash → 源码全文——账本不膨胀、源码可回溯
FACTOR_SOURCES_DIR = "sources"

FILES = {
    "trail": "trail.json",
    "explored": "explored_paths.json",
    "search_paths": "search_paths.json",
    "registry": "registry.json",
    "mining_state": "mining_state.json",
}

MINING_CONFIG = {
    "max_rounds": 50,
    "max_fail_streak": 10,
    # max_candidates 2026-08-24 降级为信息性计数：候选池满不再是停点
    # （session.jsonl 事故：导入的 stateRoot 自带 8 入册因子，池满条件
    # 从第一刻为真，may_stop 永久打开，agent 33 turn 全停等手动 push）。
    "max_candidates": 3,
    # 机械停点（2026-08-24 用户决策；当晚二次修正：试验上限按「当前簇」
    # 计，不按终身计——trail 只增不减，终身上限=一次到顶永久停机）。
    # 簇 = 尾部同族链（|ρ|≥0.6 连续尾块），换方向即断链重置。
    "max_cluster_trials": 200,  # 当前方向连续同族试验上限（失控 backstop）
    # 族内 IC 收敛（2026-08-26 规划书：替换全局收敛——纯 must_rotate 停点，
    # 族内滑窗对滑窗；旧键 ic_conv_window/ic_conv_delta 退役为死键不迁移）。
    # Wf=10 由生产回放校准定稿（规划书附录 B1）：119 族中近期族 20-25 条
    # 量级，Wf=20（门槛 40）下历史 0 触发=门形同虚设；Wf=10 的 5 个触发段
    # 全部是真实平台（amihud 0.72 平台×3 + dk 族 0.348 vs 0.364×2）
    "fam_conv_window": 10,      # 本族最近 Wf 条 vs 本族前一 Wf 条窗口（≤0 关闭）
    "fam_conv_delta": 0.05,     # 改善 < δf → 族收敛（强制换向，断链解除）
    # 尾部线收敛 delta（2026-08-27 规划书 D4）：与 IC 线同值同量纲
    # （spread_ir 与 |IC_IR| 同为 IR 尺度，bar≈0.43-0.51 语境下 0.05 ≈ 10%）。
    # 族收敛双轨 AND：两线都平才 must_rotate；本键 ≤0 = 尾部线退出判定
    # （回到 IC 单轨行为）
    "fam_conv_delta_tail": 0.05,
    # 换手率定价（2026-08-28 规划书 WS-T2 v1 双报）：false = 判定基毛
    # 口径（net 三件套只陪跑）；WS1 net 段重校 + bar 定档后切 true
    # （G1 本就 net 不动，G2/G3 同步切）。可被 mining_state 同名键覆盖
    "tail_net_basis": False,
    # 成本口径版本（flat:v1 = 单边固定 bps，读 env.calibration.cost）——
    # 进尾块/registry 条目/尾账本键；变更 = 新键重计（跨成本档不可比）
    "cost_model_version": "flat:v1",
    # 灵感源自主切换（2026-09-01 规划书 WS-A/B）：声明与 random 策略配额
    # 都按「方向段」计——断链（arc 归零）即重置。可被 mining_state 同名
    # 键覆盖（与 max_rounds 同一覆盖口径）
    "inspiration_quota": 2,      # 每方向段灵感重置声明上限（第 3 次起拒收）
    "random_strategy_quota": 1,  # 每方向段 random 策略发射上限（声明+R1 轮转共用）
    # 正交门（2026-09-06）：提交时对候选与在册账本做值相关检查（dev 区
    # 平均秩相关）。动机：2026-09-05 全量正交审计实证——量价相关域与历史
    # IDO 系同源（|ρ|≈0.70，符号构造性相反）、tail vol_of_vol 八连注册
    # 两两至 1.000。显著性门只回答"它自己是否真实"，本门回答"账本是
    # 否已知"：deflation 对试验次数定价，本门对账本有效维度定价。
    # 2026-09-10 用户决策：正交降级为提示——冗余度不再否决入册（深挖式
    # 重组绑已知强腿过 deflation 墙，与正交硬拒结构性冲突，前者恰是新域
    # 唯一现实入口；test 区为最终仲裁）。行为：reject_th 以上 → 放行入册
    # 但 orthogonality.action 记 annotate + hint（高冗余提示）；
    # annotate_th~reject_th → annotate；<annotate_th → 通过。
    # ortho_hard_gate=True 可恢复旧硬拒行为（默认 False）。
    # 缓存缺失/基础设施失败 → 跳过不拒绝不烧名（book.backfill 重建基线）。
    "orthogonality_gate": True,
    "ortho_hard_gate": False,
    "ortho_reject_th": 0.80,
    "ortho_annotate_th": 0.60,
    # ---- 推进治理（2026-09-19 定稿：超时换向制，无终态）----
    # 演进：初版"有界补推+收摊终态"被用户否决——收摊会让预定任务在死线前
    # 静默，违反"死线=最低期限非收摊点"纪律。终稿（用户方案）：一段时间
    # 无试验落账 → 引擎注入 rotate 重置灵感，无上限、无终态；死线收官与
    # finalize 是仅有的结束方式。增长只认本线引擎试验数（叙事回合不算）。
    "stuck_after_s": 1800,          # 超时换向窗口：无落账超此 → 注入 rotate
                                     #（兼作重发间隔：每窗最多一注）
    "drain_repush_spacing_s": 900,  # drain（库存清空供数指令）重发间隔
    "drain_max_emissions": 3,       # drain 发射预算，耗尽后由超时换向接管
}


def resolve_state_root(root: str | os.PathLike | None = None) -> Path:
    if root:
        return Path(root)
    env = os.environ.get("DSH_FACTOR_MINER_STATE_ROOT")
    if env:
        return Path(env)
    return DEFAULT_STATE_ROOT


def _path(kind: str, root: str | os.PathLike | None = None) -> Path:
    root = resolve_state_root(root)
    root.mkdir(parents=True, exist_ok=True)
    return root / FILES[kind]


def _atomic_write_json(path: Path, payload: Any, indent: int = 2) -> None:
    """原子写 JSON（tmp + os.replace）：中途崩溃不留半截文件——半截 JSON 会让
    read 端兜底成空列表，等于整个文件静默丢失（有界池/轨迹不可这样丢）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(str(tmp), str(path))


def read_json_list(kind: str, root: str | os.PathLike | None = None) -> list[Any]:
    path = _path(kind, root)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_json_entry(kind: str, entry: dict[str, Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    # 写锁内读改写（并行 P0）：无锁的读-追加-重写在多进程下互相覆盖丢条目
    with state_write_lock(resolve_state_root(root)):
        entries = read_json_list(kind, root)
        entries.append(entry)
        path = _path(kind, root)
        _atomic_write_json(path, entries)
    return {"kind": kind, "index": len(entries) - 1, "path": str(path)}


def write_json(kind: str, entries: list[Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    with state_write_lock(resolve_state_root(root)):
        path = _path(kind, root)
        _atomic_write_json(path, entries)
    return {"kind": kind, "count": len(entries), "path": str(path)}


def append_trail(entry: dict[str, Any], root: str | os.PathLike | None = None):
    return append_json_entry("trail", entry, root)


def append_explored(entry: dict[str, Any], root: str | os.PathLike | None = None):
    return append_json_entry("explored", entry, root)


def append_search_path(entry: dict[str, Any], root: str | os.PathLike | None = None):
    return append_json_entry("search_paths", entry, root)


def read_registry(root: str | os.PathLike | None = None) -> list[Any]:
    return read_json_list("registry", root)


def write_registry(entries: list[Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    return write_json("registry", entries, root)


def append_registry_entry(entry: dict[str, Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    """registry 锁内读-追加-写（并行 P5 压测补丁）：「read → append →
    write_registry」的读在锁外有丢更新窗口（3×8 压测丢一半）。追加一律走本助手。"""
    with state_write_lock(resolve_state_root(root)):
        entries = read_json_list("registry", root)
        entries.append(entry)
        path = _path("registry", root)
        _atomic_write_json(path, entries)
    return {"kind": "registry", "count": len(entries), "path": str(path)}


# ---------------------------------------------------------------- 源码外挂库

def factor_source_path(source_hash: str, root: str | os.PathLike | None = None) -> Path:
    """源码库路径：sources/<hash前2位>/<hash>.py（分片防单目录过大）。"""
    return resolve_state_root(root) / FACTOR_SOURCES_DIR / str(source_hash)[:2] / f"{source_hash}.py"


def store_factor_source(source: str, root: str | os.PathLike | None = None) -> dict[str, Any]:
    """源码入外挂库（内容寻址、幂等、原子、无锁）。

    键 = source_fingerprint(source)——与 trail 条目的 source_hash 同一函数，
    账本零改动即可回溯。同键必同内容：并发写竞争是幂等的（各写 tmp 后
    replace 同一字节序列），故不加写锁（评估高频路径不抢锁）。"""
    h = source_fingerprint(source)
    path = factor_source_path(h, root)
    if path.exists():
        return {"hash": h, "path": str(path), "dedup": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".py.tmp")
    # newline=""：禁用换行翻译——存档字节必须与产生 source_hash 的原文
    # 逐字节一致（CRLF 翻译会让按内容寻址的档案失真）
    tmp.write_text(source or "", encoding="utf-8", newline="")
    os.replace(str(tmp), str(path))
    return {"hash": h, "path": str(path), "dedup": False}


def factor_source_stats(root: str | os.PathLike | None = None) -> dict[str, Any]:
    """源码库规模（trail_summary 展示用；目录缺失 = 空库）。"""
    base = resolve_state_root(root) / FACTOR_SOURCES_DIR
    count, total_bytes = 0, 0
    if base.exists():
        for shard in base.iterdir():
            if shard.is_dir():
                for f in shard.iterdir():
                    if f.suffix == ".py":
                        count += 1
                        try:
                            total_bytes += f.stat().st_size
                        except OSError:
                            pass
    return {"count": count, "bytes": total_bytes,
            "dir": str(base)}


def read_mining_state(root: str | os.PathLike | None = None) -> dict[str, Any]:
    path = _path("mining_state", root)
    default = dict(MINING_CONFIG, round=0, global_fail_streak=0,
                   candidate_pool=[], finalized=False, lanes={})
    if not path.exists():
        return default
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    for k, v in default.items():
        st.setdefault(k, v)
    return st


def reset_mining_state(root: str | os.PathLike | None = None) -> dict[str, Any]:
    default = dict(MINING_CONFIG, round=0, global_fail_streak=0,
                   candidate_pool=[], finalized=False, lanes={})
    with state_write_lock(resolve_state_root(root)):
        _atomic_write_json(_path("mining_state", root), default)
    return default


def record_round(entry: dict[str, Any], root: str | os.PathLike | None = None) -> dict[str, Any]:
    with state_write_lock(resolve_state_root(root)):
        st = read_mining_state(root)
        st["round"] = int(entry.get("round", st["round"] + 1))
        if entry.get("accepted"):
            st["global_fail_streak"] = 0
            st["candidate_pool"].append({
                "signal": entry.get("signal", ""),
                "ic_ir_train": entry.get("ic_ir_train"),
                "accepted_at_round": st["round"],
                "attribution": entry.get("attribution", ""),
            })
        else:
            st["global_fail_streak"] = int(st.get("global_fail_streak", 0)) + 1
        path = _path("mining_state", root)
        _atomic_write_json(path, st)
    return check_termination(st)


def arc_rounds_bump(root: str | os.PathLike | None = None,
                    reset: bool = False, lane: str | None = None) -> int:
    """方向段（arc）轮次计数（2026-08-25 arc 化；2026-08-31 lane 化）。

    - reset=False：+1（agent 写一条叙事 trail = 一轮，bridge._paths_append）
    - reset=True ：归 0（家族链断裂 = 机械换向事件，
      bridge._append_engine_trail_batch 检测到新试验不接本线尾部链时调用
      ——与 cluster_trials 断链重置同一事件）
    lane 化（方案 A）：每条并行线各自的方向段预算——A 线换向不该解锁
    B 线的预算，A 线的轮次也不消耗 B 线的预算。计数存
    mining_state["lanes"][lane]["arc_rounds"]；顶层遗留 arc_rounds 键
    首次写入时迁移进 default 线（历史单线数据不丢）。
    旧 mining_state 无字段 → 读作 0（部署即解锁触顶会话）。
    遗留 st["round"]（record_round 维护）仅信息性，不参与停点。
    reset=True 同时清本线方向段配额计数（2026-09-01 灵感源规划书）：
    inspiration_used / random_used 与 arc 同一「方向段」生命周期——
    断链换向即新段，配额跟着归零（decl_round/explore_hashes 保留：
    前者是同轮去重哨兵，后者是 origin 判据，都不是段内配额）。"""
    with state_write_lock(resolve_state_root(root)):
        st = read_mining_state(root)
        lanes = _ensure_lane_buckets(st)
        bucket = lanes.setdefault(lane or DEFAULT_LANE, {})
        bucket["arc_rounds"] = 0 if reset else int(bucket.get("arc_rounds", 0)) + 1
        if reset:
            bucket["inspiration_used"] = 0
            bucket["random_used"] = 0
        _atomic_write_json(_path("mining_state", root), st)
    return int(bucket["arc_rounds"])


def _ensure_lane_buckets(st: dict[str, Any]) -> dict[str, Any]:
    """lanes 桶初始化 + 顶层遗留 arc_rounds 一次性迁移（default 线继承）。

    迁移后删除顶层键——双处存值会在回滚/混部时双计。"""
    lanes = st.get("lanes")
    if not isinstance(lanes, dict):
        lanes = {}
        st["lanes"] = lanes
    if "arc_rounds" in st:
        legacy = int(st.get("arc_rounds") or 0)
        d = lanes.setdefault(DEFAULT_LANE, {})
        if legacy > 0 and int(d.get("arc_rounds", 0) or 0) == 0:
            d["arc_rounds"] = legacy
        del st["arc_rounds"]
    return lanes


def lane_arc_rounds(st: dict[str, Any], lane: str | None = None) -> int:
    """读指定线的方向段轮次（写侧由 arc_rounds_bump 维护）。未迁移的
    旧 state（尚无 lanes 桶）对 default 线回退顶层遗留键——读正确性
    不依赖首次 bump 触发迁移。"""
    lane = lane or DEFAULT_LANE
    bucket = (st.get("lanes") or {}).get(lane) or {}
    arc = int(bucket.get("arc_rounds", 0) or 0)
    if arc == 0 and lane == DEFAULT_LANE and "arc_rounds" in st:
        arc = int(st.get("arc_rounds") or 0)
    return arc


# ------------------------------------------------- 灵感源切换台账（2026-09-01）

def _lane_bucket_mut(root, lane) -> tuple[dict[str, Any], dict[str, Any]]:
    """锁内读 mining_state + 定位本线桶（写侧通用入口）。"""
    st = read_mining_state(root)
    lanes = _ensure_lane_buckets(st)
    bucket = lanes.setdefault(lane or DEFAULT_LANE, {})
    return st, bucket


def inspiration_declare(root: str | os.PathLike | None = None,
                        lane: str | None = None, round_id: int = -1,
                        mode: str = "literature") -> dict[str, Any]:
    """灵感重置声明配额消费（规划书 WS-B/D1/D3；锁内读改写）。

    round_id 去重：_loop_directive 在一个叙事轮内被 status/evaluate/
    submit 多次调用，同一声明重复观察不得重复扣额——decl_round 记上次
    受理轮，同轮重放直接沿用判定。被拒的声明不记 decl_round（下轮重判
    同因，确定性一致）。mode=random 时叠加检查 random 配额（声明与 R1
    轮转共用）——受理不等于发射，random_used 在发射时刻计（见
    random_emit_count）。"""
    with state_write_lock(resolve_state_root(root)):
        st, bucket = _lane_bucket_mut(root, lane)
        quota = int(st.get("inspiration_quota",
                           MINING_CONFIG["inspiration_quota"]))
        rquota = int(st.get("random_strategy_quota",
                            MINING_CONFIG["random_strategy_quota"]))
        used = int(bucket.get("inspiration_used", 0) or 0)
        rnd = int(bucket.get("random_used", 0) or 0)
        if bucket.get("inspiration_decl_round") == round_id:
            return {"accepted": True, "inspiration_used": used,
                    "quota_left": max(0, quota - used), "random_used": rnd}
        if used >= quota:
            return {"accepted": False,
                    "reason": (f"灵感重置声明超限（本方向段 {used}/{quota} 已用；"
                               "断链换向后配额自动重置）——本条按停笔宣言处理，"
                               "给出可执行的新假设"),
                    "inspiration_used": used, "quota_left": 0,
                    "random_used": rnd}
        if mode == "random" and rnd >= rquota:
            return {"accepted": False,
                    "reason": (f"random 策略本方向段已用 {rnd}/{rquota}（声明与"
                               "引擎轮转共用配额）——改声明 [灵感重置:literature] "
                               "或先完成本次换向（断链后配额重置）"),
                    "inspiration_used": used,
                    "quota_left": max(0, quota - used), "random_used": rnd}
        bucket["inspiration_used"] = used + 1
        bucket["inspiration_decl_round"] = round_id
        _atomic_write_json(_path("mining_state", root), st)
        return {"accepted": True, "inspiration_used": used + 1,
                "quota_left": max(0, quota - used - 1), "random_used": rnd}


def random_emit_count(root: str | os.PathLike | None = None,
                      lane: str | None = None, round_id: int = -1) -> None:
    """random 策略发射计数（WS-A/D3）：声明强制与 R1 轮转共用，配额
    检查在决策侧（读 random_used），本函数只在策略真正挂出时记账。
    round_id 去重同 inspiration_declare——一轮内 loop 被多次计算只计
    一次。全局随机 st["random_runs"] 是纯遥测（观测 origin=random 的
    通路总量；R1 轮转判据用的是 papers.searches，不读本键）。"""
    with state_write_lock(resolve_state_root(root)):
        st, bucket = _lane_bucket_mut(root, lane)
        if bucket.get("random_emit_round") == round_id:
            return
        bucket["random_used"] = int(bucket.get("random_used", 0) or 0) + 1
        bucket["random_emit_round"] = round_id
        st["random_runs"] = int(st.get("random_runs", 0) or 0) + 1
        _atomic_write_json(_path("mining_state", root), st)


def lane_random_used(st: dict[str, Any], lane: str | None = None) -> int:
    """读本线 random 已发射次数（决策侧配额检查；快照读，锁外安全）。"""
    bucket = (st.get("lanes") or {}).get(lane or DEFAULT_LANE) or {}
    return int(bucket.get("random_used", 0) or 0)


def lane_drive_bk(st: dict[str, Any], lane: str | None = None) -> dict[str, Any]:
    """读本线推进治理记账（2026-09-19 总闸；快照读，锁外安全）。

    键：last_key/last_ts（上次发射的策略键与时戳）、growth_rounds/
    growth_trials/growth_ts（上次增长快照）、nudge/nudge_ts（卡住补推
    计数与时戳）、drain（drain 发射计数）、milestone（已消费的入册
    里程碑数——query 边沿触发的消费记录）。"""
    bucket = (st.get("lanes") or {}).get(lane or DEFAULT_LANE) or {}
    bk = bucket.get("drive_bk")
    return dict(bk) if isinstance(bk, dict) else {}


def lane_drive_bk_update(root: str | os.PathLike | None = None,
                          lane: str | None = None,
                          **fields: Any) -> dict[str, Any]:
    """更新本线推进治理记账（锁内合并写；返回更新后的完整记账快照）。

    与 random_emit_count 同一写模式（状态查询路径上的小原子写）。"""
    with state_write_lock(resolve_state_root(root)):
        st, bucket = _lane_bucket_mut(root, lane)
        bk = bucket.get("drive_bk")
        bk = dict(bk) if isinstance(bk, dict) else {}
        bk.update(fields)
        bucket["drive_bk"] = bk
        _atomic_write_json(_path("mining_state", root), st)
        return dict(bk)


def lane_explore_hashes_extend(root: str | os.PathLike | None = None,
                               lane: str | None = None,
                               hashes: "list[str] | tuple[str, ...]" = ()) -> None:
    """explore 幸存者哈希入 lane 台账（WS-C origin=random 判据）。

    并集追加（多批 explore 的幸存者都算），截尾 300 条防膨胀——哈希
    是 16 字符级字符串，300 条 ~KB 级。幂等（同哈希不重复）。"""
    with state_write_lock(resolve_state_root(root)):
        st, bucket = _lane_bucket_mut(root, lane)
        cur = list(bucket.get("explore_hashes") or [])
        cur.extend(h for h in hashes if h and h not in cur)
        bucket["explore_hashes"] = cur[-300:]
        _atomic_write_json(_path("mining_state", root), st)


def lane_decl_seen_update(root: str | os.PathLike | None = None,
                           lane: str | None = None,
                           seen: int = 0,
                           previous: int | None = None) -> None:
    """S2 宣言分型校验的轮次游标（2026-09-04 探索相似性防线）：
    本线引擎条目已核对到的下标——record_trail 宣言校验取 own[seen:]
    为「本轮」，核对后写回当前总量。previous 传入时 CAS 语义
    （并发 record_trail 不回退游标）。"""
    with state_write_lock(resolve_state_root(root)):
        st, bucket = _lane_bucket_mut(root, lane)
        cur = int(bucket.get("decl_seen_engine") or 0)
        if previous is not None and cur != previous:
            return
        bucket["decl_seen_engine"] = int(seen)
        _atomic_write_json(_path("mining_state", root), st)


def check_termination(state: dict[str, Any] | None = None, root: str | os.PathLike | None = None) -> dict[str, Any]:
    """合法停点只认机械判据（2026-08-24 用户决策；当晚二次修正：试验上限
    按「当前簇」计；2026-08-25 arc 化：轮次上限按「当前方向段」计——
    终身计数在 trail 只增不减的现实下 = 一次到顶永久停机）：

    - finalized（test 一次性锁的自然终态）→ kind=finalize
    - arc_rounds >= max_rounds（当前方向段叙事轮次上限）→ kind=direction_budget
    - cluster_trials >= max_cluster_trials（当前方向连续同族试验上限，
      state 需带 cluster_trials 键——由 bridge 侧用 family_streak 数学
      注入；换方向即断链重置）→ kind=direction_budget
    - fail >= max_fail_streak（跨方向全局收敛；当前无喂入方，恒 0 不触发）
      → kind=fail_streak

    kind 语义（注入器分流依据）：direction_budget = 方向预算耗尽，
    换向断链自动解除——引擎照挂 rotate/literature 策略、注入器照常推进；
    finalize / fail_streak = 真终态，注入器静默交还用户。
    候选池满不是停点（12/12 假穷尽实证）。终身 n_trials 不做停点——
    只用于 deflation 多重检验计价（统计上必须全量计数）。
    IC_IR 改善收敛是机械停点但在 bridge 侧计算（族内滑窗对滑窗，
    见 bridge._family_convergence；kind=direction_budget/must_rotate——
    2026-08-26 v8：convergence 静默终态退役，族收敛只做换向信号），
    不经本函数。"""
    st = state if state is not None else read_mining_state(root)
    arc = int(st.get("arc_rounds", 0))
    fail = int(st.get("global_fail_streak", 0))
    cluster = int(st.get("cluster_trials", 0))
    n_trials = int(st.get("n_trials", 0))
    n_cand = len(st.get("candidate_pool", []))
    max_rounds = int(st.get("max_rounds", MINING_CONFIG["max_rounds"]))
    max_fail = int(st.get("max_fail_streak", MINING_CONFIG["max_fail_streak"]))
    max_cluster = int(st.get("max_cluster_trials",
                             MINING_CONFIG["max_cluster_trials"]))

    if st.get("finalized"):
        return {"stop": True, "kind": "finalize",
                "reason": "已 finalize（test 已消费，这批结束）", "state": st}
    if arc >= max_rounds:
        return {"stop": True, "kind": "direction_budget",
                "reason": (f"方向段轮次上限（arc_rounds {arc} >= {max_rounds}，"
                           "换向断链自动重置）"), "state": st}
    if cluster >= max_cluster:
        return {"stop": True, "kind": "direction_budget",
                "reason": (f"簇试验上限（当前方向连续 {cluster} >= "
                           f"{max_cluster}，换方向即断链重置）"), "state": st}
    if fail >= max_fail:
        return {"stop": True, "kind": "fail_streak",
                "reason": f"全局连续失败（{fail} >= {max_fail}，跨方向收敛）", "state": st}
    return {"stop": False, "kind": None,
            "reason": (f"继续（方向段轮次 {arc}/{max_rounds}，簇试验 {cluster}/{max_cluster}"
                       f"（终身 {n_trials} 只计价不停），失败 {fail}/{max_fail}，"
                       f"候选 {n_cand} 不计停）"),
            "state": st}
