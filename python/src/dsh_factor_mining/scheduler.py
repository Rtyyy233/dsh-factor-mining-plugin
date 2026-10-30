# coding=utf-8
"""研究预算调度器（边际价值调度器 v1.1，PLAN-scheduler-task-brief-2026-09-29）。

把「进行中研究」统一成带族谱的 frontier，每张卡片带「下一发值不值」的
分数 A(s)，算力按分数投放。分期落地（依赖链 1a→1b→(2a/2b/2c∥κ)→3a→
3b→排序先行→4a→4b→5），本模块承载 3a（打分+影子 tick）/3b（周对账）/
4a（exec_share 灰度+消费记账）/4b（收编入口）/5（重放 harness）。

数学基线（任务书 §四 v1.1 复审后口径，推导见技术设计 §13 F1-F11）：
- 墙高 Θ_l = √(1−ρ̄_l)·b_{N_l}，b_N = Φ⁻¹(1−1/N)——不用 sqrt(2 ln N)
  （小 N 高估 20-30%）；Kish N_eff 移植作废（低估墙 3 倍）；全局尾概率
  用单因子 Monte Carlo
- Q = Φ⁻¹(1−p_def)（复用近失现价重算路径）
- U = w1·U_regime + w2·U_legs + w3·U_gap（+w4·U_resid，默认 0）；
  U_regime 必须过分裂样本复验门（不过则归零）
- D = 分位校正后的最大秩相关距离（防参考集变大机械趋 1）
- E = Σ p̂_valid·r̂；r̂ Beta-Binomial 收缩，n 数族批次、k 数该批 ≥1
  成功；成功判据 = 墙单位增益 g=(t_child−t_parent)/b_{N_t} > δ，对决策
  时点冻结 null 计价
- C = λ_c·算力 + λ_s·边际有效假设数 + κ·族流入；边际有效假设数 =
  (1−ρ̄_f)/(1+(n_f−1)ρ̄_f)²（族拥挤是折扣不是附加费——F1；κ 默认 0
  待 1c 回归裁决——不显著则整项删除）
- A = MV̂ − λC 加性式（对偶阈值形式）；z 标准化仅 tick 内排序，阈值
  与停止判断读原始项；原始分项全量入 score 事件（权重离线反事实搜索
  的基础；fn_version 与权重绑定，禁止在线调权——红线 4）
- 分配 = Thompson 后验采样 + ε 硬底（D7；ε 承保多样性保险不参与
  Thompson 分工）；停止 = P(视界内≥1成功) ≈ 1−Π(1−p̂_i) < p_stop（F9，
  E 项机制的直接推论）

红线遵守：
- 注入引擎零改动：选点行只走点名通道（loop 内容层 + ENGINE_ORDERS 由
  runner 消费 loop 自动带出），drive.ts 投放层零接触
- 活动计数隔离：tick/影子打分/日志写入零调用 lane_activity_tick，
  不碰试验数——打分不是挖掘活动（红线 5）
- 逐比特回退：exec_share=0（默认）时系统行为与现状一致——picks 不
  挂出、drain 判定原样；影子开关独立于执行开关（红线 8）
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from . import derivation
from .factor.gates import dsr_p_from_stats, dsr_t_from_stats, norm_ppf
from .filelock import state_write_lock

SCHEDULER_STATE_FILE = "scheduler_state.json"

#: 配置默认（MINING_CONFIG["scheduler"] 的缺省体；mining_state 同名键覆盖）
SCHEDULER_DEFAULTS: dict[str, Any] = {
    "enabled": False,           # 影子开关（独立于执行开关）
    "exec_share": 0.0,          # 执行灰度；0 = 逐比特回退（默认现状）
    "epsilon": 0.20,            # ε 硬配额：随机农场+新方向的多样性保险
    "fn_version": "v1.1",
    "weights": {"beta": 1.0, "gamma": 1.0, "eta": 1.0, "lam": 1.0},
    "u_weights": {"regime": 1.0, "legs": 1.0, "gap": 1.0, "resid": 0.0},
    "p_stop": 0.05,
    "horizon_days": 7,          # D8：视界 H，对账与 r̂ 标定窗口
    "delta": 0.10,              # D9：墙单位增益阈值
    "tick_min_interval_s": 600, # tick 节流（与 drain 间隔同量级）
    "telemetry_eval_budget": 4, # 每 tick 遥测评估上限（2a 纪律：防饿死生产）
    "top_n_scores": 10,
    "rank_by_a": False,         # 排序先行实验：深化队列 p 升序 → 按 A
    "kappa": 0.0,               # κ 族流入项（1c 回归裁决；默认 0 = 删除态）
    "rho_family_default": 0.3,  # ρ̄_f v1 诚实缺省（trail 成对相关接入前）
    "w_tok": 0.0,               # token 成本权重（事件日志积累 token 前为 0）
    "w_slot": 1.0,
    "lam_c": 0.01,              # λ_c：单槽位 z 当量价（0.01–0.05 区间起步）
    "lam_s": 1.0,
}

#: op 目录（legal(s) 的 v1 形态：多腿因子给 drop_leg/reweight，通用给
#: 结构轴；p̂_valid v1 恒 1.0——AST 可算性的历史命中率数据未积累，不虚标）
OPS_GENERIC = ("add_leg", "winsorize", "regime_condition", "compose_with",
               "lag_decay", "deepen_variant")
OPS_MULTILEG = ("drop_leg", "reweight")

#: Beta 先验（r̂ 经验贝叶斯收缩；均值 0.1——历史入册/试验比的量级）
R_ALPHA0, R_BETA0 = 1.0, 9.0


# ---------------------------------------------------------------- 配置

def scheduler_config(mining_state: dict | None = None) -> dict:
    """合并配置：SCHEDULER_DEFAULTS ← mining_state["scheduler"] 覆盖。"""
    cfg = json.loads(json.dumps(SCHEDULER_DEFAULTS))  # 深拷贝防污染
    over = (mining_state or {}).get("scheduler")
    if isinstance(over, dict):
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------- 墙数学

def b_wall(n: float) -> float:
    """b_N = Φ⁻¹(1−1/N)：N 发独立噪声的最优值的较准近似（F2）。

    N=100 → 2.33 / N=1000 → 3.09（sqrt(2 ln N) 分别高估 30%/20%）。"""
    if n <= 1:
        return 0.0
    return norm_ppf(1.0 - 1.0 / n)


def wall_height(n: float, rho_bar: float = 0.0) -> float:
    """层墙 Θ_l = √(1−ρ̄)·b_N（单因子表示，均衡相关下精确——F2）。"""
    rho_bar = min(max(float(rho_bar), 0.0), 0.99)
    return math.sqrt(1.0 - rho_bar) * b_wall(n)


def wall_prime(n: float) -> float:
    """Θ'(N) = d b_N/dN = 1/(N²·φ(z_N))（精确导数，b_N 口径）。

    N=1000 → ≈2.9×10⁻⁴ z/发（F3 数量级验算一致：逐发统计价是小量，
    硬约束在准入与 test，不进调度器）。"""
    if n <= 1:
        return 0.0
    z = b_wall(n)
    pdf = math.exp(-z * z / 2.0) / math.sqrt(2.0 * math.pi)
    if pdf <= 0:
        return 0.0
    return 1.0 / (n * n * pdf)


def single_factor_tail_prob(z: float, n: int, rho_bar: float = 0.0,
                            draws: int = 20000, seed: int = 20260929) -> float:
    """全局尾概率的单因子 Monte Carlo：Z_j = √ρ̄·V + √(1−ρ̄)·ε_j，
    P(max_j Z_j ≥ z)。均衡相关下精确、便宜（F2）。确定性 seed。"""
    if n <= 0:
        return 0.0
    import numpy as np
    rng = np.random.default_rng(seed)
    rho_bar = min(max(float(rho_bar), 0.0), 0.99)
    common = rng.standard_normal(draws)
    max_z = np.full(draws, -np.inf)
    for _ in range(n):
        eps = rng.standard_normal(draws)
        max_z = np.maximum(max_z, math.sqrt(rho_bar) * common
                           + math.sqrt(1.0 - rho_bar) * eps)
    return float((max_z >= z).mean())


# ---------------------------------------------------------------- 打分五项

def q_from_p(p_def: float | None) -> float | None:
    """Q = Φ⁻¹(1−p_def)——已有量的换标度，不是新计算。"""
    if p_def is None or not (0.0 < p_def < 1.0):
        return None
    return norm_ppf(1.0 - p_def)


def d_novelty(max_abs_corr: float | None, ref_size: int,
              overlap_days: int = 250) -> float | None:
    """D：分位校正后的最大秩相关距离（F5 诚实性控制）。

    原始 1−max|ρ| 随参考集变大机械趋 0（max 是次序统计量）——失区分度。
    校正：独立参考下 E[max|ρ|] ≈ σ_ρ·b_M（σ_ρ = 零假设相关 std 的
    Fisher 近似 1/√(T−3)，M=参考集大小），D = clip(E_ref − obs, 0, 1)。
    观测相关若只到随机撞出来的期望水平 → D=0（不比随机新）；低于期望
    多少就是真新颖度——参考集变大不再机械塌缩。"""
    if max_abs_corr is None or ref_size <= 0:
        return None
    if overlap_days <= 4:
        return None
    sigma_rho = 1.0 / math.sqrt(overlap_days - 3.0)
    e_ref = sigma_rho * b_wall(ref_size)
    return min(max(e_ref - float(max_abs_corr), 0.0), 1.0)


def marginal_effective_hyp(n_f: int, rho_bar: float) -> float:
    """边际有效假设数 = ∂N_eff/∂n = (1−ρ̄_f)/(1+(n_f−1)ρ̄_f)²（F1 精确）。

    族拥挤是折扣不是附加费：n_f=30、ρ̄=0.3 → 0.007（族内第 31 发只
    消耗全新方向 0.7% 的统计资本）。"""
    rho_bar = min(max(float(rho_bar), 0.0), 0.99)
    return (1.0 - rho_bar) / (1.0 + max(n_f - 1, 0) * rho_bar) ** 2


def u_gap(t_def: float | None, bar_sigma: float, se: float) -> float | None:
    """U_gap = (Θ̄ − t_def)/SE——离墙的距离（近失带内为正）。"""
    if t_def is None or se <= 0:
        return None
    return (bar_sigma - t_def) / se


# ---------------------------------------------------------------- r̂ / E 项

def success_gain(t_child: float | None, t_parent: float | None,
                 n_at_decision: int) -> float | None:
    """F7 墙单位增益 g = (t_child − t_parent)/b_{N_t}（纪元归一：
    晚时代 op 面对的墙更高，原始 Δt 系统性不可比）。"""
    if t_child is None or t_parent is None or n_at_decision <= 1:
        return None
    bw = b_wall(n_at_decision)
    if bw <= 0:
        return None
    return (t_child - t_parent) / bw


def r_hat_from_events(events: list[dict], op: str, delta: float = 0.10,
                      horizon_days: int = 7) -> dict:
    """按 F7 三修正从事件流累积 (k, n)：

    - n 数**族批次**不数试验（每 family+op 一批——族内试验不独立）
    - k 数该批 ≥1 成功（成功 = 批内任一 expand 的 g > δ）
    - g 对决策时点冻结 null 计价（用 expand 快照的 n_trials/池参数）
    返回 {"k", "n", "r_hat"}；r_hat = (α0+k)/(β0+n−k)（Beta-Binomial
    后验均值，收缩：小样本格子塌向族先验）。"""
    batches: dict[str, dict] = {}
    for ev in events:
        if ev.get("ev") != "expand" or ev.get("op") != op:
            continue
        fam = str(ev.get("family") or ev.get("hash") or ev.get("child") or "?")
        key = fam
        b = batches.setdefault(key, {"gains": []})
        b["gains"].append(ev.get("gain"))
    k = n = 0
    for b in batches.values():
        gains = [g for g in b["gains"] if isinstance(g, (int, float))]
        if not gains:
            continue
        n += 1
        if any(float(g) > delta for g in gains):
            k += 1
    # Beta-Binomial 后验均值 = (α0+k)/(α0+β0+n)（后验 Beta(α0+k, β0+n−k)
    # 的期望——设计文档 §2.4 的 (α0+k)/(β0+n−k) 系笔误，按正确式实现）
    r_hat = (R_ALPHA0 + k) / (R_ALPHA0 + R_BETA0 + n)
    return {"k": k, "n": n, "r_hat": r_hat}


def thompson_sample_r(k: int, n: int, seed_key: str) -> float:
    """Thompson 后验采样 r ~ Beta(α0+k, β0+n−k)（F8）。

    确定性 seed（fn_version+op+tick_id 派生）——打分确定性（I4）要求
    同输入同输出；不确定性大的格子偶尔被抽高，探索由概率匹配内生。"""
    import random
    rng = random.Random(seed_key)
    return rng.betavariate(R_ALPHA0 + k, R_BETA0 + max(n - k, 0))


def legal_ops(source: str | None, is_multileg: bool | None = None) -> list[str]:
    """legal(s)：多腿（可拆）给 drop_leg/reweight + 通用轴；否则通用轴。

    is_multileg 由调用方注入（attribution.extract_legs 的结果）；
    source 给出时现场判定。"""
    if is_multileg is None and source:
        from .factor.attribution import extract_legs
        is_multileg = extract_legs(source) is not None
    ops = list(OPS_GENERIC)
    if is_multileg:
        ops = list(OPS_MULTILEG) + ops
    return ops


def e_expandability(events: list[dict], ops: list[str], tick_key: str,
                    delta: float = 0.10, thompson: bool = True) -> dict:
    """E = Σ p̂_valid·r̂（v1 p̂_valid=1——历史命中率未积累，不虚标）。

    thompson=True 用后验采样（F8）；False 用后验均值（保守备选）。"""
    total = 0.0
    per_op = {}
    for op in ops:
        stats = r_hat_from_events(events, op, delta=delta)
        r = (thompson_sample_r(stats["k"], stats["n"], f"{tick_key}:{op}")
             if thompson else stats["r_hat"])
        per_op[op] = {"r": round(r, 4), "k": stats["k"], "n": stats["n"]}
        total += r  # p̂_valid = 1.0
    return {"e": total, "per_op": per_op, "p_valid_basis": "v1:1.0"}


# ---------------------------------------------------------------- 停止规则

def stop_posterior(ops_probs: list[float]) -> float:
    """F9：P(视界内 ≥1 成功) ≈ 1 − Π(1−p̂_i)——E 项机制的直接推论，
    非独立部件。"""
    p = 1.0
    for x in ops_probs:
        p *= (1.0 - min(max(float(x), 0.0), 1.0))
    return 1.0 - p


# ---------------------------------------------------------------- 状态文件

def read_scheduler_state(root) -> dict:
    path = Path(root) / SCHEDULER_STATE_FILE
    if not path.exists():
        return {}
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _write_scheduler_state(root, st: dict) -> None:
    path = Path(root) / SCHEDULER_STATE_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    import os
    os.replace(str(tmp), str(path))


def _mut_scheduler_state(root, **fields) -> dict:
    with state_write_lock(root):
        st = read_scheduler_state(root)
        st.update(fields)
        _write_scheduler_state(root, st)
        return dict(st)


# ---------------------------------------------------------------- 打分

def score_node(node: dict, ctx: dict, events: list[dict],
               cfg: dict, tick_key: str) -> dict:
    """单节点五项打分。ctx 需含（调用方注入，测试可 fake）：
    p_def / dsr（sr,skew,kurt,n）/ bar_sigma / pool_std / n_trials /
    max_abs_corr / ref_size / u_regime / u_legs / u_gap / family_n /
    rho_family / inflow_rate / is_multileg。缺项诚实置 None（不编数）。

    返回原始分项（阈值/停止判断读原始项——z 标准化只在 tick 内排序）。"""
    w = cfg["weights"]
    uw = cfg["u_weights"]
    p_def = ctx.get("p_def")
    Q = q_from_p(p_def)
    # U 合成：缺项跳过（权重归一在可用项上）
    u_parts, u_w = {}, {}
    for key in ("regime", "legs", "gap", "resid"):
        v = ctx.get(f"u_{key}")
        if v is not None:
            u_parts[key] = float(v)
            u_w[key] = float(uw.get(key, 0.0))
    wsum = sum(u_w.values())
    U = (sum(u_parts[k] * u_w[k] for k in u_parts) / wsum) if wsum > 0 else None
    D = d_novelty(ctx.get("max_abs_corr"), int(ctx.get("ref_size") or 0),
                  int(ctx.get("overlap_days") or 250))
    ops = legal_ops(None, ctx.get("is_multileg"))
    E = e_expandability(events, ops, tick_key,
                        delta=float(cfg["delta"]), thompson=True)
    # C 三段（F1/F3：族拥挤在价值侧已收，C 侧只有折扣化的边际假设价）
    slot_cost = (cfg["w_slot"] * (1.0 if ctx.get("new_candidate", True)
                                  else 0.1))
    tok_cost = cfg["w_tok"] * float(ctx.get("tokens_est") or 0.0)
    lam_s = float(cfg["lam_s"]) * float(ctx.get("nearmiss_active") or 0) * \
        wall_prime(float(ctx.get("n_trials") or 100))
    marg = marginal_effective_hyp(int(ctx.get("family_n") or 1),
                                  float(ctx.get("rho_family")
                                        or cfg["rho_family_default"]))
    inflow = float(cfg["kappa"]) * float(ctx.get("inflow_rate") or 0.0)
    C = float(cfg["lam_c"]) * (slot_cost + tok_cost) + lam_s * marg + inflow
    A = None
    if Q is not None:
        A = Q
        if U is not None:
            A += w["beta"] * U
        if D is not None:
            A += w["gamma"] * D
        A += w["eta"] * E["e"]
        A -= w["lam"] * C
    return {
        "Q": None if Q is None else round(Q, 4),
        "U": None if U is None else round(U, 4),
        "D": None if D is None else round(D, 4),
        "E": round(E["e"], 4), "E_per_op": E["per_op"],
        "C": round(C, 4),
        "C_parts": {"slot": round(slot_cost, 4), "lam_s": round(lam_s * marg, 6),
                    "marginal_hyp": round(marg, 6), "inflow": round(inflow, 6)},
        "A": None if A is None else round(A, 4),
        "U_inputs": u_parts, "p_valid_basis": E["p_valid_basis"],
    }


def z_standardize(values: list[float | None]) -> list[float | None]:
    """tick 内排序用的 z 标准化（None 传染为 None；阈值判断不读这个）。"""
    xs = [v for v in values if isinstance(v, (int, float))]
    if len(xs) < 2:
        return list(values)
    mu = sum(xs) / len(xs)
    sd = (sum((x - mu) ** 2 for x in xs) / len(xs)) ** 0.5 or 1e-12
    return [None if v is None else (v - mu) / sd for v in values]


# ---------------------------------------------------------------- frontier / tick

def frontier_view(root, deepen_hashes: list[str] | None = None) -> list[dict]:
    """frontier = 图上 status=frontier 节点 + 近失带准入（不在图即入图，
    审阅钟/近失通道的统一准入口——4b「四账一币」的图侧投影）。"""
    nodes = derivation.load_nodes(root)
    out = [dict(n, hash=h) for h, n in nodes.items()
           if n.get("status") in (None, "frontier")]
    if deepen_hashes:
        known = {n["hash"] for n in out}
        for h in deepen_hashes:
            if h in known:
                continue
            try:
                derivation.note_node(root, h, None, None, "root_unlinked",
                                     status="frontier")
            except Exception:
                pass
            out.append({"hash": h, "parent": None, "op": None,
                        "edge_source": "root_unlinked", "status": "frontier"})
    return out


def maybe_tick(bridge, engine_trail: list | None = None,
               deepen_tel: dict | None = None) -> dict | None:
    """调度 tick 主入口（挂 _loop_directive，deepen 遥测之后）。

    节流 + enabled 检查 + 打分 + score 事件 +（exec_share>0 时）选点。
    任何失败吃掉返回 None——挂点零影响评估主路径（I5 关闭等价）。
    bridge 需提供：state_root / _read_engine_trail / _n_eff_from_entries /
    _resolve_pool_std / envs（现价重算 Q）；测试可注入同名 fake。"""
    try:
        return _tick_impl(bridge, engine_trail, deepen_tel)
    except Exception as e:
        try:
            import sys
            sys.stderr.write(f"[scheduler] tick 失败(不阻断): {e!r}\n")
        except Exception:
            pass
        return None


def _tick_impl(bridge, engine_trail, deepen_tel) -> dict | None:
    from .state import read_mining_state
    root = bridge.state_root
    cfg = scheduler_config(read_mining_state(root))
    if not cfg.get("enabled"):
        return None
    st = read_scheduler_state(root)
    now = time.time()
    if now - float(st.get("last_tick_ts") or 0) < float(
            cfg["tick_min_interval_s"]):
        return None
    tick_id = int(st.get("tick_id") or 0) + 1
    events = derivation.load_events(root)
    deepen_hashes = [r.get("hash") for r in (deepen_tel or {}).get("queue")
                     or [] if r.get("hash")]
    # 近失带 hash 桥接：deepen queue 只带 prefix——用 trail 全 hash 补全
    full_hashes = []
    trail = engine_trail if engine_trail is not None else []
    for pref in deepen_hashes:
        m = next((e.get("source_hash") for e in trail
                  if isinstance(e, dict)
                  and str(e.get("source_hash", "")).startswith(str(pref))),
                 None)
        if m:
            full_hashes.append(m)
    frontier = frontier_view(root, full_hashes)
    scored = []
    for i, node in enumerate(frontier[:cfg["top_n_scores"]]):
        ctx = _node_ctx(bridge, node, trail, events, cfg)
        terms = score_node(node, ctx, events, cfg,
                           f"{cfg['fn_version']}:{tick_id}:{node['hash']}")
        terms["hash"] = node["hash"]
        terms["status"] = node.get("status") or "frontier"
        # 停止规则（F9）：P(视界内≥1成功) < p_stop → 退场孵化（可重返）
        p_is = [op["r"] for op in terms["E_per_op"].values()]
        terms["P_success"] = round(stop_posterior(p_is), 4)
        if terms["P_success"] < float(cfg["p_stop"]) and len(frontier) > 1:
            try:
                derivation.update_status(root, node["hash"], "incubating")
            except Exception:
                pass
            terms["status"] = "incubating"
        scored.append(terms)
    ranked = [s for s in scored if s.get("A") is not None]
    ranked.sort(key=lambda s: -float(s["A"]))
    for rank, s in enumerate(ranked):
        s["rank"] = rank
    for s in scored:
        derivation.note_score(
            root, s["hash"], s, rank=s.get("rank"),
            fn_version=cfg["fn_version"], policy="shadow", executed=False)
    out = {
        "policy": "shadow", "fn_version": cfg["fn_version"],
        "tick": tick_id, "frontier_n": len(frontier),
        "top": [{"hash": s["hash"][:12], "A": s["A"],
                 "Q": s["Q"], "U": s["U"], "D": s["D"], "E": s["E"],
                 "C": s["C"], "P_success": s["P_success"]}
                for s in ranked[:5]],
        "note": "调度器影子打分（不执行）——exec_share=0 逐比特回退态",
    }
    # ---- 4a 执行灰度：exec_share>0 才挂选点（Thompson+ε 硬底）----
    if float(cfg.get("exec_share") or 0) > 0:
        picks = _emit_picks(root, cfg, ranked, st, tick_id)
        out["policy"] = "exec"
        out["picks"] = picks
        out["note"] = (f"调度器执行灰度 exec_share={cfg['exec_share']}"
                       f"——选点行只含事实（hash/操作/预算/键）")
        _mut_scheduler_state(root, last_tick_ts=now, tick_id=tick_id,
                             pending_picks=picks)
        return out
    _mut_scheduler_state(root, last_tick_ts=now, tick_id=tick_id)
    return out


def _node_ctx(bridge, node, trail, events, cfg) -> dict:
    """节点打分上下文：现价重算 Q 的池参数 + 遥测项（预算内不触发评估
    ——遥测评估（regime/legs）由 4b 收编后另行供给；v1 影子期读事件流
    最近快照，纯只读）。"""
    h = node["hash"]
    ctx: dict[str, Any] = {"hash": h}
    snap = _latest_snapshot(events, h)
    if snap is not None:
        ctx.update({
            "p_def": snap.get("p_def"), "new_candidate": snap.get(
                "new_candidate", True),
            "n_trials": snap.get("n_trials"),
            "u_gap": _gap_from_snapshot(snap),
        })
    # 图族计数（C 项 n_f / 流入率）
    fam = _family_of(events, h)
    if fam:
        fam_events = [e for e in events if e.get("ev") == "expand"
                      and (e.get("family") == fam)]
        ctx["family_n"] = len({e.get("child") for e in fam_events
                               if e.get("child")})
        ctx["family"] = fam
    return ctx


def _latest_snapshot(events, hash: str) -> dict | None:
    """该 hash 最新 expand 快照（含 dsr 四元组+池参数——r̂/对账/重放的
    自足数据源）。"""
    hit = None
    for e in events:
        if e.get("ev") == "expand" and e.get("child") == hash:
            hit = e
    return hit


def _family_of(events, hash: str) -> str | None:
    for e in reversed(events):
        if e.get("ev") == "expand" and e.get("child") == hash:
            return e.get("family") or e.get("hash")
    return None


def _gap_from_snapshot(snap: dict) -> float | None:
    """U_gap 从快照重算（(Θ̄−t)/SE——同池态纯函数）。"""
    dsr = snap.get("dsr") or {}
    t = dsr_t_from_stats(dsr.get("sr"), dsr.get("skew"), dsr.get("kurt"),
                         dsr.get("n"), snap.get("bar_sigma"),
                         snap.get("pool_std"))
    se = 1.0 / math.sqrt(max(float(dsr.get("n") or 1), 1.0))
    return u_gap(t, float(snap.get("bar_sigma") or 0.0), se)


def _emit_picks(root, cfg, ranked, st, tick_id) -> list[dict]:
    """选点（4a 纪律）：Thompson 排序 + ε 硬底 + 消费记账键。

    - 选点数 = max(1, round(预算份额·(1−ε)))；ε 部分算力留给随机农场
      与新方向（多样性保险，不参与 Thompson 分工——红线 7）
    - 每行带键 pick:{hash}:{tick_id}——消费于该状态的试验派发；
      pending 未消费的同 hash 不重挂（compose_fired 定律：电平触发+
      会变键=重注机）
    - 行只含事实（hash/操作类型/预算），不含方向论证（红线 2）"""
    budget_share = min(max(float(cfg["exec_share"]), 0.0), 1.0)
    eps = min(max(float(cfg["epsilon"]), 0.0), 1.0)
    n_picks = max(1, round(budget_share * (1.0 - eps) * 3))
    pending = {p.get("hash") for p in (st.get("pending_picks") or [])
               if isinstance(p, dict)}
    picks = []
    for s in ranked:
        if len(picks) >= n_picks:
            break
        if s["hash"] in pending:
            continue
        picks.append({
            "hash_prefix": s["hash"][:12], "hash": s["hash"],
            "op": "deepen_variant", "budget": 1,
            "key": f"pick:{s['hash']}:{tick_id}",
            "A": s.get("A"),
        })
    return picks


def consume_pick(root, child_hash: str, parent_hash: str | None) -> dict | None:
    """pick 消费记账：试验派发（expand 入账）时调用——同状态不重复挂出
    的机械基础。返回被消费的 pick（或 None）。"""
    st = read_scheduler_state(root)
    for p in (st.get("pending_picks") or []):
        if not isinstance(p, dict):
            continue
        target = p.get("hash")
        if target in (child_hash, parent_hash):
            consumed = dict(p, consumed_ts=time.strftime(
                "%Y-%m-%dT%H:%M:%S"), consumed_by=child_hash)
            rest = [q for q in (st.get("pending_picks") or [])
                    if isinstance(q, dict) and q.get("key") != p.get("key")]
            with state_write_lock(root):
                st2 = read_scheduler_state(root)
                st2["pending_picks"] = rest
                st2.setdefault("consumed_picks", []).append(consumed)
                st2["consumed_picks"] = st2["consumed_picks"][-200:]
                _write_scheduler_state(root, st2)
            return consumed
    return None


def has_active_frontier(root) -> bool:
    """drain 收编判据（4a）：exec 模式下 frontier 非空（含 pending picks）
    → 不算 drained（旧 drain 阶梯让位；exec_share=0 时桥侧不读本函数）。"""
    st = read_scheduler_state(root)
    if st.get("pending_picks"):
        return True
    return bool(frontier_view(root))


# ---------------------------------------------------------------- 排序先行实验

def rank_by_a_enabled(root) -> bool:
    """排序开关（零机制改动的纯内容实验——默认 False=现状 p 升序）。"""
    from .state import read_mining_state
    return bool(scheduler_config(read_mining_state(root)).get("rank_by_a"))


def latest_a_by_hash(root) -> dict[str, float]:
    """最新 score 事件的 A 值索引 {hash: A}（resort 消费）。"""
    out: dict[str, float] = {}
    for e in derivation.load_events(root):
        if e.get("ev") == "score" and isinstance(
                (e.get("terms") or {}).get("A"), (int, float)):
            out[e.get("hash")] = float(e["terms"]["A"])
    return out


def resort_by_a(root, rows: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """深化队列排序换 A（排序先行实验改点——bridge.py rows.sort 的 A 版）。

    有 score 的按 A 降序在前，无 score 的保持 p 升序在后——打分器没
    覆盖的节点不换序（诚实：不虚挂）。"""
    a_map = latest_a_by_hash(root)
    with_a = [(a_map.get(r[1].get("source_hash"), None), i, r)
              for i, r in enumerate(rows)]
    with_a.sort(key=lambda t: (t[0] is None, -(t[0] or 0), t[1]))
    return [r for _, _, r in with_a]


def follow_rate(root, window_days: int = 7, follow_horizon_h: int = 48
                ) -> dict:
    """跟随率统计（排序先行实验验收）：挂出首位（score rank=0）后
    follow_horizon_h 小时内，该 hash 或其子代是否被试验派发（expand）。"""
    events = derivation.load_events(root)
    expands = [e for e in events if e.get("ev") == "expand"]
    heads = [e for e in events if e.get("ev") == "score" and e.get("rank") == 0]
    now = time.time()
    followed = total = 0
    for h in heads:
        ts = _parse_ts(h.get("ts"))
        if ts is None or now - ts > window_days * 86400:
            continue
        total += 1
        target = h.get("hash")
        deadline = ts + follow_horizon_h * 3600
        hit = any(e.get("child") == target or e.get("hash") == target
                  for e in expands
                  if (_parse_ts(e.get("ts")) or 0) <= deadline
                  and (_parse_ts(e.get("ts")) or 0) >= ts)
        followed += 1 if hit else 0
    return {"followed": followed, "total": total,
            "rate": (followed / total) if total else None,
            "window_days": window_days,
            "note": ("跟随率=排序首位被试验派发消费的比例；排序先行实验"
                     "的用户拍板材料——不跟则调度器退回遥测供给")}


def _parse_ts(s) -> float | None:
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


# ---------------------------------------------------------------- 周对账（3b）

def _spearman(a: list[float], b: list[float]) -> float | None:
    if len(a) != len(b) or len(a) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = (sum((x - ma) ** 2 for x in ra)) ** 0.5
    db = (sum((y - mb) ** 2 for y in rb)) ** 0.5
    if da < 1e-12 or db < 1e-12:
        return None
    return num / (da * db)


def reconcile(root, window_days: int = 7, now: float | None = None) -> dict:
    """周对账（F6 配对定义）：

    配对 = 决策时刻父态 A × 子−父**同池态** deflated 增益——父态在
    子代评估时刻的池态下重算（现价重算是纯函数），公共池漂移在差分中
    消掉。分臂：pick 消费的 expand（via_pick）与自由 expand 分开统计；
    ε 硬配额臂（无 pick 的自由派发）不进同口径功效判据。附分位校准
    （预测分位 × 实现均值增益）——校准比相关更贴决策质量。"""
    now = now or time.time()
    events = derivation.load_events(root)
    scores: dict[str, tuple[float, float]] = {}  # hash -> (ts, A)
    for e in events:
        if e.get("ev") == "score":
            t = _parse_ts(e.get("ts"))
            a = (e.get("terms") or {}).get("A")
            if t is not None and isinstance(a, (int, float)):
                if e.get("hash") not in scores or scores[e.get("hash")][0] <= t:
                    scores[e.get("hash")] = (t, float(a))
    pairs_pick, pairs_free = [], []
    for e in events:
        if e.get("ev") != "expand" or not e.get("new_candidate"):
            continue
        t = _parse_ts(e.get("ts"))
        if t is None or now - t > window_days * 86400:
            continue
        parent = e.get("hash")
        child = e.get("child")
        pa = scores.get(parent)
        if pa is None or pa[0] > t:
            continue  # 无决策时刻的父态 A（打分器未覆盖）——跳过不编造
        gain = e.get("gain")
        if not isinstance(gain, (int, float)):
            continue
        rec = (pa[1], float(gain))
        (pairs_pick if e.get("via_pick") else pairs_free).append(rec)

    def _arm(recs):
        rho = _spearman([r[0] for r in recs], [r[1] for r in recs]) \
            if len(recs) >= 3 else None
        # 分位校准：按 A 五分位桶的均值增益
        buckets = {}
        if recs:
            srt = sorted(recs)
            for i, (a, g) in enumerate(srt):
                key = min(4, (i * 5) // len(srt))
                buckets.setdefault(key, []).append(g)
            buckets = {f"q{k}": round(sum(v) / len(v), 4)
                       for k, v in sorted(buckets.items())}
        return {"n": len(recs), "spearman": None if rho is None else round(rho, 4),
                "calibration": buckets}

    terms = [e for e in events if e.get("ev") == "terminal"]
    admitted_total = sum(1 for e in terms
                         if e.get("verdict") == "admitted")
    return {
        "window_days": window_days,
        "pick_arm": _arm(pairs_pick),
        "free_arm_excluded_from_power": _arm(pairs_free),
        "note_free_arm": ("自由派发臂（ε 硬配额语义）不进同口径功效判据"
                          "——故意次优，只对账不判效"),
        "terminal_admitted_total": admitted_total,
        "calibration_note": "校准=预测分位×实现均值增益；K 周连续 rho<=0 触发"
                            "估计器复审标记（不自动改权重——红线 4）",
    }


# ---------------------------------------------------------------- 重放（5）

REPLAY_STRATEGIES = ("uniform", "score_max", "novelty", "near_miss",
                     "marginal_value")


def replay(root, budgets: tuple[float, ...] = (0.5, 1.0),
           strategies: tuple[str, ...] = REPLAY_STRATEGIES) -> dict:
    """重放 harness（F10 反事实池态）：事件流时间序重放，各策略按自身
    反事实累计试验数重算 deflation——现价重算是纯函数，但墙用单因子
    解析式 b_N（行为策略的 CRN 采样器不可移植——诚实声明，方向一致性
    足够，绝对值近似）。主终点 = 每单位算力 deflated t 增益。

    生成侧不可重放（红线排除）：策略只能选「事件流里真实 expand 过的
    派发」，未发生过的子代无结局数据——本实验测的是固定生成器下的
    分配质量（任务书 §8.3 范围声明）。"""
    events = derivation.load_events(root)
    expands = [e for e in events if e.get("ev") == "expand"]
    total = len(expands)
    if total == 0:
        return {"error": "事件流无 expand——重放需 1b 日志积累",
                "note": "影子期 4 周后跑首份报告（任务书 §五 票 5）"}
    # 每 expand 事件的池快照（行为策略口径）+ 增益
    scored_expands = []
    for i, e in enumerate(expands):
        if not isinstance(e.get("gain"), (int, float)):
            continue
        scored_expands.append({
            "i": i, "gain": float(e["gain"]), "a_parent": e.get("a_parent"),
            "p_def": e.get("p_def"), "new_candidate": e.get("new_candidate"),
            "near_miss": bool(e.get("near_miss_flag")),
            "dsr": e.get("dsr") or {}, "pool_std": e.get("pool_std"),
        })
    out: dict[str, dict] = {}
    for strat in strategies:
        out[strat] = {}
        for frac in budgets:
            n_avail = max(1, int(round(total * float(frac))))
            # 策略选择：在可用的前 n_avail 个 expand 里按策略偏好排序消费
            pool = [x for x in scored_expands if x["i"] < n_avail]
            if strat == "uniform":
                chosen = pool
            elif strat == "score_max":
                chosen = sorted(pool, key=lambda x: -(x["a_parent"] or -1e9))
            elif strat == "novelty":
                chosen = sorted(pool, key=lambda x: -(x.get("d_novelty") or 0))
            elif strat == "near_miss":
                chosen = sorted(pool, key=lambda x: (0 if x["near_miss"] else 1))
            else:  # marginal_value：按父态 A（行为侧边际价值打分的代理）
                chosen = sorted(pool, key=lambda x: -(x["a_parent"] or -1e9))
            # 反事实池态：该策略自己消费了 j 发 → 墙 = b_N(j)（单因子近似）
            gains, slots = [], 0
            for j, x in enumerate(chosen):
                t_pi = dsr_t_from_stats(x["dsr"].get("sr"),
                                        x["dsr"].get("skew"),
                                        x["dsr"].get("kurt"),
                                        x["dsr"].get("n"),
                                        b_wall(j + 2), x.get("pool_std"))
                if t_pi is None:
                    continue
                gains.append(t_pi)
                slots += 1 if x["new_candidate"] else 0.1
            total_gain = sum(g - gains[0] for g in gains[1:]) if gains else 0.0
            out[strat][f"budget_{frac}"] = {
                "n": len(chosen),
                "gain_per_slot": (round(total_gain / slots, 4)
                                  if slots else None),
                "n_slots": round(slots, 1),
            }
    return {
        "strategies": out,
        "counterfactual_note": ("各策略墙=b_N(自身累计试验数) 单因子解析式"
                                "——不继承行为策略池态（F10）"),
        "power_note": ("功效回填：影子 4 周实测 σ_diff 代入 required_blocks"
                       "——不预编数字（任务书 §8.4）"),
    }


def required_blocks(sigma_diff: float, delta: float, alpha: float = 0.05,
                    power: float = 0.80) -> float:
    """配对周块设计所需块数：n ≈ ((z_{1−α/2}+z_{power})·σ_diff/δ)²。

    σ_diff 用影子期实测周块内策略差标准差回填（D4 纪律：不预编数字）。"""
    z_a = norm_ppf(1.0 - alpha / 2.0)
    z_p = norm_ppf(power)
    if delta <= 0 or sigma_diff < 0:
        return float("inf")
    return ((z_a + z_p) * sigma_diff / delta) ** 2
