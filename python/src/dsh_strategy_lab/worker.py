# coding=utf-8
"""strategy worker（锁 2：策略代码只在 harness 沙箱执行）。

CLI 主进程 spawn 本模块子进程，跑 evaluate / walk_forward / submit 的
全部测量（audit、模拟、placebo、门链）；worker 只写自己的结果文件，
**从不写账本/registry/test 锁**——超时被主进程杀 = 零写入不烧指纹；
事务写全部在主进程（worker 成功返回之后）。

环境经 npz 分发（复用因子层 worker 的 write/load_env_npz）：面板
加载器私有（锁 1 数据禁运），workflow 中不出现原始面板文件路径。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from dsh_factor_mining.worker import load_env_npz

from .audit import audit_full, run_apply, run_fit, run_pipeline
from .contract import compile_strategy
from .env_adapter import fold_envs, region_span, walk_forward_schedule
from .gates import g1_placebo, g2_artifact, g3_deflation, trade_minhash
from .increment import (
    combine_factor_matrices,
    g4_increment,
    increment_report,
    passive_topk_path,
)
from .placebo import (
    PLACEBO_MIN_DRAWS_FULL,
    PLACEBO_MIN_DRAWS_LIGHT,
    matched_turnover_placebo,
)
from .robustness import startup_perturbation
from .simulator import CostModel, simulate


def _cost_model(params: dict) -> CostModel:
    cm = params.get("cost_model") or {}
    known = CostModel.__dataclass_fields__
    return CostModel(**{k: v for k, v in cm.items() if k in known})


def _factor_matrices(params: dict, env):
    """编译因子源（CLI 已按 factor_refs 从因子 registry 解出 source）。"""
    srcs = params.get("factor_sources") or {}
    out = []
    for h, src in srcs.items():
        from dsh_factor_mining.worker import _compile as _compile_factor

        out.append(_compile_factor(src)(env))
    return out


def _baseline_path(params: dict, env):
    Fs = _factor_matrices(params, env)
    if not Fs:
        return None
    F = combine_factor_matrices(Fs, env)
    return passive_topk_path(
        F, env, top_k=int(params.get("top_k", 10)),
        rebalance_every=int(params.get("rebalance_every", 5)))


def _evaluate(ns, env, params: dict) -> dict:
    """development 评估：审计链（五道锁的锁 3/4 全量）+ 基线模拟 +
    轻 placebo（m≥30 标 degraded）+ 增量报告（给了因子源才算）。"""
    seed = int(params.get("seed", 42))
    cm = _cost_model(params)
    aud = audit_full(ns, env, seed,
                     day_perm_m=int(params.get("day_perm_m", 20)),
                     rw_m=int(params.get("rw_m", 10)), cost_model=cm)
    base_path = aud.pop("base_path", None)
    sim = simulate(env, base_path, cm)
    out = {"stage": "development", "audit": aud, "sim": sim,
           "cost_model_version": cm.version}
    if aud["g0_pass"]:
        placebo = matched_turnover_placebo(
            env, base_path, real_sharpe=sim["metrics"]["sharpe"],
            m=int(params.get("placebo_m", PLACEBO_MIN_DRAWS_LIGHT)),
            seed=seed, cost_model=cm)
        placebo["degraded"] = bool(
            placebo.get("m", 0) < PLACEBO_MIN_DRAWS_FULL)
        out["placebo"] = placebo
        bp = _baseline_path(params, env)
        if bp is not None:
            out["increment"] = increment_report(env, base_path, bp, cm)
        else:
            out["increment"] = None
            out["increment_note"] = "未提供因子源——增量门只在 submit 判"
    return out


def _walk_forward(ns, env, params: dict) -> dict:
    """walk-forward（策略层必经 stage）：折内 fit 物理截断、apply 折末
    截断，权重段拼接成全路径后单次模拟（账目连续）。"""
    seed = int(params.get("seed", 42))
    cm = _cost_model(params)
    wf = params.get("wf_config") or {}
    t0_date = params.get("t0_date")
    t1_date = params.get("t1_date")
    folds = walk_forward_schedule(
        env, n_folds=int(wf.get("n_folds", 5)),
        embargo_bars=int(wf.get("embargo_bars", 5)),
        t0_date=t0_date, t1_date=t1_date)
    span_t0, span_t1 = folds[0]["apply_t0_idx"], folds[-1]["apply_t1_idx"]
    segments: list[dict] = [dict() for _ in range(span_t0)]
    for f in folds:
        fit_env, apply_env = fold_envs(env, f)
        state = run_fit(ns, fit_env, seed) if fit_env is not None else None
        seg = run_apply(ns, state, apply_env, seed)
        segments.extend(seg[f["apply_t0_idx"]:f["apply_t1_idx"]])
    sim_env = env if span_t1 >= env.T else _trunc_env(env, span_t1)
    sim = simulate(sim_env, segments[:sim_env.T], cm)
    eq = sim["equity"]
    per_fold = []
    for f in folds:
        a, b = f["apply_t0_idx"], min(f["apply_t1_idx"], len(eq) - 1)
        if b > a and eq[a] > 0:
            seg_ret = eq[b] / eq[a] - 1.0
            per_fold.append({"fold": f["fold"],
                             "t0": f["apply_t0_date"], "t1": f["apply_t1_date"],
                             "ret": round(float(seg_ret), 6)})
        else:
            per_fold.append({"fold": f["fold"], "ret": None})
    return {"stage": "walk_forward", "n_folds": len(folds),
            "folds": folds, "per_fold": per_fold, "sim": sim,
            "cost_model_version": cm.version}


def _trunc_env(env, t1_inclusive):
    from .env_adapter import truncate_env

    return truncate_env(env, t1_inclusive - 1)


def _test_region(ns, env, params: dict) -> dict:
    """test 区一次性消费的评估本体（锁的写入在主进程，成功返回后）。

    t0 守卫（2026-08-28 修复配套）：显式 t0_date 早于 sel_end → 拒绝——
    test 起点侵入 dev 区 = 把 overlay 迭代过的数据当样本外（只会更晚，
    不会更早；省略即默认 sel_end 起）。"""
    import pandas as _pd

    seed = int(params.get("seed", 42))
    cm = _cost_model(params)
    sel_end = env.calibration.sel_end if env.calibration else None
    t0_date = params.get("t0_date") or sel_end
    if t0_date is not None and sel_end:
        try:
            early = _pd.Timestamp(str(t0_date)) < _pd.Timestamp(str(sel_end))
        except Exception:
            early = True   # 解析失败按越界处理（保守拒绝）
        if early:
            raise ValueError(
                f"test 的 t0_date={t0_date} 早于 sel_end={sel_end}——"
                "test 区起点不得侵入 dev 区（overlay 在此迭代过）；省略 "
                "t0_date 即默认 sel_end 起，只允许更晚。")
    t0, _ = region_span(env, t0_date=t0_date)
    sub_env = _trunc_env(env, env.T) if t0 == 0 else _offset(env, t0)
    path = run_pipeline(ns, sub_env, seed)
    sim = simulate(sub_env, path, cm)
    return {"stage": "test", "t0_date": str(t0_date), "sim": sim,
            "cost_model_version": cm.version}


def _offset(env, skip):
    from .env_adapter import offset_env

    return offset_env(env, skip)


def _submit(ns, env, params: dict) -> dict:
    """submit 权威测量：audit + 权威 placebo（m≥60）+ G1′/G2′/G3′/G4′ +
    robustness。事务写与 N_eff 计价在主进程（需要账本；worker 无状态）。"""
    seed = int(params.get("seed", 42))
    cm = _cost_model(params)
    t0 = time.monotonic()
    aud = audit_full(ns, env, seed,
                     day_perm_m=int(params.get("day_perm_m", 20)),
                     rw_m=int(params.get("rw_m", 10)), cost_model=cm)
    base_path = aud.pop("base_path", None)
    out = {"stage": "submit", "audit": aud}
    if not aud["g0_pass"]:
        out["gate_verdicts"] = {"g0": (False, aud.get(
            "note", "G0 审计链未过（程序性拒收）"))}
        return out
    sim = simulate(env, base_path, cm)
    out["sim"] = sim
    # G1′ 权威 placebo（m≥60；预算自适应在 placebo 内部）
    placebo = matched_turnover_placebo(
        env, base_path, real_sharpe=sim["metrics"]["sharpe"],
        m=int(params.get("placebo_m", PLACEBO_MIN_DRAWS_FULL)),
        seed=seed, cost_model=cm)
    placebo["degraded"] = bool(placebo.get("m", 0) < PLACEBO_MIN_DRAWS_FULL)
    out["placebo"] = placebo
    # G2′ 随机游走（audit.random_walk 已在 aud 里）
    g2_ok, g2_msg = g2_artifact(aud.get("random_walk") or {})
    # G3′ deflation：N_eff = 账本（含本次 trade_minhash）
    trade_mh = trade_minhash(sim.get("trades") or [])
    ledger = params.get("ledger") or []
    ledger_all = ledger + [{"trade_mh": trade_mh}]
    from .gates import trade_n_eff
    n_eff, n_total = trade_n_eff(ledger_all)
    g1_ok, g1_msg = g1_placebo(placebo)
    g3_ok, g3_msg, bar = g3_deflation(placebo.get("z"), n_eff)
    # G4′ 增量（因子源必给——CLI 已校验 refs）
    bp = _baseline_path(params, env)
    if bp is None:
        out["gate_verdicts"] = {"g0": (True, ""),
                                "g4": (False, "G4′ 无法判定：未提供因子源")}
        return out
    inc = increment_report(env, base_path, bp, cm)
    g4_ok, g4_msg = g4_increment(inc)
    out["increment"] = inc
    out["robustness"] = startup_perturbation(
        ns, env, seed=seed,
        n_offsets=int((params.get("robustness") or {}).get("n_offsets", 8)),
        cost_model=cm)
    out["trade_mh"] = trade_mh
    out["n_eff"] = n_eff
    out["n_total"] = n_total
    out["deflation_bar"] = bar
    out["gate_verdicts"] = {
        "g0": (True, "审计链通过（确定性/截断不变/延迟退化）"),
        "g1": (g1_ok, g1_msg), "g2": (g2_ok, g2_msg),
        "g3": (g3_ok, g3_msg), "g4": (g4_ok, g4_msg),
    }
    out["accepted"] = all(ok for ok, _ in out["gate_verdicts"].values())
    out["elapsed_s"] = round(time.monotonic() - t0, 3)
    return out


def _dev_env(env, params: dict, stage: str):
    """dev 区域硬切（2026-08-28 修复：test 窗窥视）——因子层 2026-08-18
    生产审计同型修复（bridge._factor_walk_forward）的移植。

    修前：development/walk_forward/submit 拿全面板——WF 末折即 test 窗、
    dev 试验与 submit 门（G1′ placebo / G4′ 增量）的数字全含
    [sel_end, T)，test_lock 形同虚设（窗早被看穿）。现 dev 侧 env 一律
    物理截断到 sel_end（截断不可关，test 窗对开发过程不可见）：
      - walk_forward 显式 t1_date 越过 sel_end → 拒绝（不是静默放行）
      - calibration 无 sel_end（直调合成 env）→ 不切 + region_degraded
        标注（此配置下 test 一次性语义不成立，生产禁止）
    返回 (env, region_info)。"""
    import pandas as _pd

    sel_end = env.calibration.sel_end if env.calibration else None
    if not sel_end:
        return env, {"t1_date": None, "region_degraded": True,
                     "n_bars": int(env.T),
                     "note": "calibration 无 sel_end——dev 不切（全面板），"
                             "test 一次性语义不成立；生产面板必须配三区"}
    if stage == "walk_forward" and params.get("t1_date") is not None:
        try:
            over = _pd.Timestamp(str(params["t1_date"])) \
                > _pd.Timestamp(str(sel_end))
        except Exception:
            over = True   # 解析失败按越界处理（保守拒绝，同因子层）
        if over:
            raise ValueError(
                f"walk_forward 的 t1_date={params['t1_date']} 越过 "
                f"sel_end={sel_end}——test 区只经 stage='test' 的一次性"
                "流程消费，不得经 walk_forward 窥视。省略 t1_date 即默认"
                "限制在 dev 区。")
    end_idx = region_span(env, t1_date=sel_end)[1]
    if end_idx <= 1:
        raise ValueError(
            f"sel_end={sel_end} 早于面板起点——dev 区为空，无可评估数据")
    return (_trunc_env(env, end_idx) if end_idx < env.T else env), {
        "t1_date": str(sel_end), "region_degraded": False,
        "n_bars": int(min(end_idx, env.T)),
        "note": "dev 区硬切到 sel_end（test 窗对开发过程物理不可见；"
                "test 经 stage='test' 一次性消费）"}


def run_request(req: dict) -> dict:
    env = load_env_npz(req["npzPath"])
    ns = compile_strategy(req["source"])
    params = req.get("params") or {}
    method = req["method"]
    if method == "strategy.evaluate":
        stage = params.get("stage", "development")
        if stage in ("development", "walk_forward"):
            env, region = _dev_env(env, params, stage)
            out = _walk_forward(ns, env, params) if stage == "walk_forward" \
                else _evaluate(ns, env, params)
            out["dev_region"] = region
            return out
        if stage == "test":
            return _test_region(ns, env, params)
        raise ValueError(f"未知 stage: {stage}")
    if method == "strategy.submit":
        env, region = _dev_env(env, params, "submit")
        out = _submit(ns, env, params)
        out["dev_region"] = region
        return out
    raise ValueError(f"worker 不支持的方法: {method}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--result")
    parser.add_argument("--ping", action="store_true",
                        help="启动自检：import 全链通过即打印 ok 退出")
    args = parser.parse_args(argv)
    if args.ping:
        print(json.dumps({"ok": True, "worker": "strategy-lab-ready",
                          "python": sys.version.split()[0]}))
        return 0
    if not args.request:
        parser.error("需要 --request（或 --ping 做启动自检）")
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    try:
        out = run_request(req)
        result = {"ok": True, "result": out}
    except Exception as e:  # noqa: BLE001 — 结构化错误，CLI 映射可行动信息
        result = {"ok": False, "error": {"message": str(e),
                                         "type": type(e).__name__}}
    result_path = Path(args.result) if args.result \
        else Path(args.request + ".out.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
