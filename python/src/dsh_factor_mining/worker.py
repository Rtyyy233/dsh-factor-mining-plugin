# coding=utf-8
"""Isolated worker for user factor code.

The bridge is the protocol host and never executes user Python factor code
when executionMode=worker.  Instead it asks this module, in a fresh
subprocess, to run exactly one check/evaluate request against a serialized
environment.  User data remains read-only; the worker only writes its own
result file.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .factor.audit import audit
from .factor.causality import check_causality
from .factor.env import FactorEnv
from .factor.evaluate import (
    evaluate,
    evaluate_batch,
    evaluate_composite,
    evaluate_selection,
    evaluate_test,
    evaluate_walk_forward,
)
from .factor.noise import factor_perf


def write_env_npz(path, env: FactorEnv) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    from dataclasses import asdict

    meta = {"dates": [str(d) for d in env.dates], "symbols": list(env.symbols),
            "calibration": asdict(env.calibration)}
    arrays = {"o": env.o, "h": env.h, "l": env.l, "c": env.c, "v": env.v}
    if env.amount is not None:
        arrays["amount"] = env.amount
    if env.listed is not None:
        arrays["listed"] = env.listed.astype(bool)
    np.savez_compressed(p, **arrays)
    p.with_suffix(p.suffix + ".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def load_env_npz(path) -> FactorEnv:
    p = Path(path)
    meta_path = p.with_suffix(p.suffix + ".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    with np.load(p, allow_pickle=False) as data:
        o = data["o"]; h = data["h"]; l = data["l"]; c = data["c"]; v = data["v"]
        amount = data["amount"] if "amount" in data else None
        listed = data["listed"] if "listed" in data else None
    dates = pd.to_datetime(meta["dates"])
    calibration = None
    if meta.get("calibration"):
        from .factor.env import Calibration

        known = {f for f in Calibration.__dataclass_fields__}
        calibration = Calibration(**{k: v for k, v in meta["calibration"].items()
                                     if k in known})
    return FactorEnv(o, h, l, c, v, dates, meta["symbols"], listed=listed,
                     amount=amount, calibration=calibration)


def _compile(source: str):
    ns = {"__name__": "dsh_factor_mining_worker_factor"}
    exec(compile(source, "<factor_source>", "exec"), ns)
    if "factor" not in ns or not callable(ns["factor"]):
        raise ValueError(
            "source 中缺少可调用的 factor(env)——函数名必须是 factor"
            "（random_generate 返回的 source 原样可用；手写因子请命名 factor）")
    return ns["factor"]


# ---- P3 并行批次：子进程池（initializer 每子进程加载一次 env npz）----

_BATCH_CHILD_ENV: dict = {}


def _batch_child_init(npz_path: str) -> None:
    _BATCH_CHILD_ENV["env"] = load_env_npz(npz_path)


def _batch_child_eval(name: str, src: str, train_end, horizon, pool_std):
    """子进程任务：算 F + 单成员诊断（与顺序路径同参同口径）。"""
    env = _BATCH_CHILD_ENV["env"]
    fn = _compile(src)
    F = fn(env)
    diag = evaluate(F, env, train_end=train_end, horizon=horizon)
    return name, diag, F


def _batch_parallel(sources: dict, npz_path: str, env, horizon, pool_std) -> dict:
    """进程池并行批次（层 1）：env npz 一份共享（子进程各自加载只读），
    逐成员提交任务，部分失败只标记该成员（{"error": ...}，bridge 侧
    本就跳过 error 成员记账）。家族统计回到本进程 evaluate_batch。"""
    from concurrent.futures import ProcessPoolExecutor

    from .procinfo import resolve_jobs

    train_end = env.calibration.dev_end
    tasks = list(sources.items())
    jobs = min(resolve_jobs(), len(tasks))
    F_dict: dict = {}
    factors: dict = {}
    with ProcessPoolExecutor(max_workers=jobs,
                             initializer=_batch_child_init,
                             initargs=(npz_path,)) as ex:
        futs = {ex.submit(_batch_child_eval, name, src, train_end, horizon,
                          pool_std): name for name, src in tasks}
        for fut, name in futs.items():
            try:
                _, diag, F = fut.result()
                F_dict[name] = F
                factors[name] = diag
            except Exception as e:  # noqa: BLE001 — 部分失败隔离
                factors[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
                F_dict[name] = np.zeros((env.T, env.N))  # 占位（家族统计只走 ok）
    return evaluate_batch(F_dict, env, horizon=horizon, pool_std=pool_std,
                          factors=factors)


def run_request(req: dict) -> dict:
    env = load_env_npz(req["npzPath"])
    method = req["method"]
    # evaluate_batch 是多 source 方法（sources dict），无单个 source——
    # 入口编译跳过，由其分支自行编译每个 source（2026-08-18 修复：
    # 此前无条件编译空串导致 batch 从未成功过）
    fn = _compile(req["source"]) if (req.get("source") and method != "factor.evaluate_batch") else None
    params = req.get("params") or {}
    state_root = params.get("state_root")
    if method == "factor.check_causality":
        return check_causality(fn, env)
    if method == "factor.evaluate":
        stage = params.get("stage", "development")
        # W2（2026-08-26 规划书）：单次 factor(env) 计时——慢实现的结构
        # 天花板（submit 噪声门 ≥10 世界 × 单次 > 预算 240s → 必然事务中止）
        # 在 evaluate 即暴露，不等 submit 烧几分钟。P1：CPU 秒与墙钟并记
        # （并行会话下墙钟被挤占失真，CPU 是实现的诚实成本）；只做 agent
        # 手写源路径，batch/walk_forward（引擎自生成源，快）不加计时
        _t0 = time.monotonic()
        _p0 = time.process_time()
        F = fn(env)
        _wall = time.monotonic() - _t0
        _cpu = time.process_time() - _p0
        _perf = {**factor_perf(_cpu), "wall_s": round(_wall, 3),
                 "cpu_s": round(_cpu, 3)}
        # v3：bar_sigma 为门参数（E[max|X|]，σ 单位）；n_trials 纯遥测
        bar_sigma = params.get("bar_sigma")
        bar_sigma = float(bar_sigma) if isinstance(bar_sigma, (int, float)) else None
        n_trials = params.get("n_trials", 1)
        n_trials = float(n_trials) if isinstance(n_trials, (int, float)) else 1.0
        pool_std = params.get("pool_std")
        pool_std = float(pool_std) if isinstance(pool_std, (int, float)) else None
        # v2 申报制：per-call horizon（bridge 已校验 ∈ 菜单并归一为 int）
        horizon = params.get("horizon")
        try:
            horizon = int(horizon) if horizon not in (None, "") else None
        except (TypeError, ValueError):
            horizon = None
        if stage == "development":
            out = evaluate(F, env, n_trials=n_trials, pool_std=pool_std,
                           horizon=horizon, bar_sigma=bar_sigma)
        elif stage == "selection":
            out = evaluate_selection(F, env)
        elif stage == "test":
            # test 是消耗品：锁写在用户 state_root，worker 子进程写同一文件，跨调用可见
            out = evaluate_test(F, env, state_root=state_root,
                                source_hash=params.get("source_hash"))
        else:
            raise ValueError(f"worker 不支持的 stage: {stage}")
        if isinstance(out, dict):
            out["perf"] = _perf
        return out
    if method == "factor.evaluate_composite":
        parts = {}
        for name, src in (params.get("ingredients") or {}).items():
            parts[name] = _compile(src)(env)
        return evaluate_composite(fn(env), parts, env)
    if method == "factor.evaluate_batch":
        sources = params.get("sources") or {}
        horizon = params.get("horizon")
        try:
            horizon = int(horizon) if horizon not in (None, "") else None
        except (TypeError, ValueError):
            horizon = None
        pool_std = params.get("pool_std")
        pool_std = float(pool_std) if isinstance(pool_std, (int, float)) else None
        # P3 并行批次：进程池逐成员并行（jobs>=2 且成员>=2 才启用；
        # 否则退化为原顺序路径）。子进程只算（F + 单成员诊断），家族
        # 统计（ρ̄/bar_sigma/deflated）由本进程收齐后统一算——语义与
        # 顺序路径完全一致（evaluate_batch 注入 factors）。
        from .procinfo import resolve_jobs
        jobs = resolve_jobs()
        if len(sources) >= 2 and jobs >= 2:
            return _batch_parallel(sources, req["npzPath"], env,
                                   horizon=horizon, pool_std=pool_std)
        # 顺序路径（P3 起同样做部分失败隔离：坏成员标 error 不烧整批）
        F_dict, factors = {}, {}
        for name, src in sources.items():
            try:
                F_dict[name] = _compile(src)(env)
            except Exception as e:  # noqa: BLE001 — 单成员失败标记，其余照评
                factors[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
                F_dict[name] = np.zeros((env.T, env.N))  # 占位（家族统计只走 ok 成员）
        return evaluate_batch(F_dict, env, horizon=horizon, pool_std=pool_std,
                              factors=factors)
    if method == "factor.walk_forward":
        return evaluate_walk_forward(fn(env), env, n_folds=int(params.get("n_folds", 5)),
                                     t0_date=params.get("t0_date"), t1_date=params.get("t1_date"))
    if method == "factor.noise_test":
        # 噪声硬门（2026-08-24 用户决策）：M 个噪声世界在单次 worker
        # 调用内循环生成+求值（避免逐世界 spawn 子进程）
        from .factor.noise import noise_test
        m = int(params.get("m", 100) or 100)
        base_seed = int(params.get("base_seed", 0) or 0)
        stat = None
        if params.get("statistic") == "spread":
            from .factor.tail import spread_ir_statistic
            stat = spread_ir_statistic
            # net 基（2026-08-28 换手率定价 WS-T2）：合成世界的组差同样
            # 按 2·cost·turn 净掉——G2 与 G3 判定口径一致，一个成本模型
            if params.get("net_cost") is not None:
                import functools as _ft
                stat = _ft.partial(spread_ir_statistic,
                                   cost=float(params["net_cost"]))
        return noise_test(fn, env, m, base_seed, statistic=stat)
    if method == "factor.tail_placebo":
        # G1 权威 placebo（WS2 2026-08-25）：submit 侧重跑——样本加厚
        # m≥60 + 预算自适应截断。horizon 视图与 tail_metrics 同口径
        # （fwd/pit/sample_step/topn 都从视图读，F 在基础 env 上算——
        # 与 evaluate 的 F = fn(env)、视图只改口径一致）
        from .factor.evaluate import _env_horizon_view, _forward_returns, _pit_mask
        from .factor.tail import topn_placebo
        horizon = params.get("horizon")
        try:
            horizon = int(horizon) if horizon not in (None, "") else None
        except (TypeError, ValueError):
            horizon = None
        F = fn(env)
        v = _env_horizon_view(env, horizon) if horizon is not None else env
        dev_end = v.calibration.dev_end
        t_end = (int(np.searchsorted(v.dates, pd.Timestamp(dev_end)))
                 if dev_end is not None else int(v.T))
        budget = params.get("budget_secs")
        return topn_placebo(
            F, _forward_returns(v), _pit_mask(v), v, t_end,
            int(params.get("draws", 120) or 120),
            int(params.get("seed", 0) or 0),
            budget_secs=(float(budget) if budget not in (None, "") else None))
    if method == "factor.day_perm_test":
        # 日期置换 null（2026-08-25）：真实边缘 + 随机配对，worker 内循环
        from .factor.permute import day_permutation_test
        m = int(params.get("m", 200) or 200)
        base_seed = int(params.get("base_seed", 0) or 0)
        return day_permutation_test(fn, env, m, base_seed)
    if method == "factor.flatness_test":
        # 参数平坦性（2026-08-25）：变体逐个编译评估，worker 单次调用
        from .factor.flatness import flatness_test
        return flatness_test(req["source"], env, params.get("flatness") or [],
                             compile_fn=_compile,
                             budget_secs=float(params.get("budget_secs", 90.0)
                                               or 90.0))
    if method == "factor.audit":
        return audit(fn, env)
    raise ValueError(f"worker 不支持的方法: {method}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--result")
    parser.add_argument("--ping", action="store_true",
                        help="启动自检：import 全链（pathlib/numpy/pandas/全部 evaluate 模块）"
                             "通过即打印 ok 退出——bridge 用它在冷启动暴露环境问题")
    args = parser.parse_args(argv)
    if args.ping:
        print(json.dumps({"ok": True, "worker": "ready",
                          "python": sys.version.split()[0]}))
        return 0
    if not args.request:
        parser.error("需要 --request（或 --ping 做启动自检）")
    req = json.loads(Path(args.request).read_text(encoding="utf-8"))
    try:
        out = run_request(req)
        result = {"ok": True, "result": out}
    except Exception as e:  # noqa: BLE001 — 转成结构化错误，让 bridge 映射为 JSON-RPC 错误
        result = {"ok": False, "error": {"message": str(e), "type": type(e).__name__}}
    result_path = Path(args.result) if args.result else Path(args.request + ".out.json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
