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
        F = fn(env)
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
            return evaluate(F, env, n_trials=n_trials, pool_std=pool_std,
                            horizon=horizon, bar_sigma=bar_sigma)
        if stage == "selection":
            return evaluate_selection(F, env)
        if stage == "test":
            # test 是消耗品：锁写在用户 state_root，worker 子进程写同一文件，跨调用可见
            return evaluate_test(F, env, state_root=state_root,
                                 source_hash=params.get("source_hash"))
        raise ValueError(f"worker 不支持的 stage: {stage}")
    if method == "factor.evaluate_composite":
        parts = {}
        for name, src in (params.get("ingredients") or {}).items():
            parts[name] = _compile(src)(env)
        return evaluate_composite(fn(env), parts, env)
    if method == "factor.evaluate_batch":
        F_dict = {}
        for name, src in (params.get("sources") or {}).items():
            F_dict[name] = _compile(src)(env)
        horizon = params.get("horizon")
        try:
            horizon = int(horizon) if horizon not in (None, "") else None
        except (TypeError, ValueError):
            horizon = None
        pool_std = params.get("pool_std")
        pool_std = float(pool_std) if isinstance(pool_std, (int, float)) else None
        return evaluate_batch(F_dict, env, horizon=horizon, pool_std=pool_std)
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
