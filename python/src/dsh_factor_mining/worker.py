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
from .factor.eval_cache import cached_evaluate
from .factor.evaluate import (
    evaluate,
    evaluate_batch,
    evaluate_composite,
    evaluate_selection,
    evaluate_test,
    evaluate_walk_forward,
)
from .factor.noise import factor_perf
from .discipline import source_fingerprint
from .panel_mmap import load_bundle_mmap
from .smoke import (
    EQUIV_ATOL,
    EQUIV_RTOL,
    SMOKE_INTERP_CAP_S,
    SMOKE_SMALL_COLS,
    SMOKE_SMALL_ROWS,
    env_subsample,
    env_view,
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


# ---- P3 并行批次：子进程池（initializer 每子进程加载一次 env npz）----

_BATCH_CHILD_ENV: dict = {}


def load_env_any(npz_path, pbin_path=None) -> FactorEnv:
    """优先 mmap bundle（多进程共享物理页，2026-09-08），失败回退 npz。"""
    if pbin_path:
        try:
            return load_bundle_mmap(pbin_path)
        except Exception:  # noqa: BLE001 — 半写/损坏/版本差 → 回退压缩路径
            pass
    return load_env_npz(npz_path)


def _batch_child_init(npz_path: str, pbin_path=None) -> None:
    _BATCH_CHILD_ENV["env"] = load_env_any(npz_path, pbin_path)


def _batch_child_eval(name: str, src: str, train_end, horizon, pool_std):
    """子进程任务：算 F + 单成员诊断（与顺序路径同参同口径）。"""
    env = _BATCH_CHILD_ENV["env"]
    fn = _compile(src)
    F = fn(env)
    diag = evaluate(F, env, train_end=train_end, horizon=horizon)
    return name, diag, F


def _batch_parallel(sources: dict, npz_path: str, env, horizon, pool_std,
                    pbin_path=None) -> dict:
    """进程池并行批次（层 1）：env 一份共享（子进程各自加载只读；有
    pbin 走 mmap 共享物理页，否则各自解压 npz），
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
                             initargs=(npz_path, pbin_path)) as ex:
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


def _min_two(call):
    """双跑取 min（亚毫秒计时噪声缓解——spike F1c 实证 alpha 会漂）。"""
    t0 = time.perf_counter()
    call()
    t1 = time.perf_counter() - t0
    t0 = time.perf_counter()
    call()
    t2 = time.perf_counter() - t0
    return round(min(t1, t2), 4)


def _smoke_request(req: dict, fn) -> dict:
    """factor.smoke（内部方法，不进 _method 注册表——tail_placebo 先例）：
    小子面板实测 + njit 探针 +（verify 模式）与原实现严格等价比对。
    外推/判定逻辑全在 bridge 侧（smoke.classify_gate）——worker 只产实测
    数字，单点可测。verify 模式下重写代码（LLM 产出）在本子进程内执行，
    不经安全扫描+等价验证的代码绝不进主执行路径。"""
    p = req.get("params") or {}
    env = load_env_npz(req["npzPath"])   # 512x128 烟测面板（bridge 已切好）
    small = env_subsample(env, int(p.get("small_rows", SMOKE_SMALL_ROWS)),
                          int(p.get("small_cols", SMOKE_SMALL_COLS)))
    out: dict = {"mode": p.get("mode", "gate"),
                 "smoke_cells": int(env.T * env.N),
                 "small_cells": int(small.T * small.N),
                 "full_cells": int(p.get("full_cells") or 0)}
    if out["mode"] == "verify":
        fn_o = _compile(p["orig_source"])
        try:
            a = fn_o(small)
        except Exception as e:  # noqa: BLE001 — 原实现在小面板失败=验证失败
            out["verify_error"] = f"orig 运行失败: {type(e).__name__}: {e}"[:200]
            return out
        try:
            b = fn(small)
        except Exception as e:  # noqa: BLE001 — 重写版可运行性即验证一部分
            out["run_error"] = f"{type(e).__name__}: {e}"[:200]
            return out
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)
        if a.shape != b.shape:
            out["equiv"] = False
            out["shape"] = f"{a.shape}->{b.shape}"
        else:
            out["equiv"] = bool(np.allclose(a, b, rtol=EQUIV_RTOL,
                                            atol=EQUIV_ATOL, equal_nan=True))
        out["t_small_s"] = _min_two(lambda: fn(small))
        out["t_smoke_s"] = _min_two(lambda: fn(env))
        return out
    # gate 模式
    try:
        fn(env_subsample(env, 8, 8))            # 预热（首次 numpy 触发等）
    except Exception as e:  # noqa: BLE001 — 小面板跑不起来（窗口长度等）
        out["interp_error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    out["t_small_s"] = _min_two(lambda: fn(small))
    t0 = time.perf_counter()
    fn(env)
    t1 = time.perf_counter() - t0
    if t1 > SMOKE_INTERP_CAP_S:
        # smoke 尺寸已超 CAP：必超预算，不再计时（省烟测预算给 njit 探针）
        out["t_smoke_s"] = round(t1, 4)
        out["too_slow"] = True
        return out
    t0 = time.perf_counter()
    fn(env)
    t2 = time.perf_counter() - t0
    out["t_smoke_s"] = round(min(t1, t2), 4)
    # njit 探针（惰性 import；未安装=available False，主路径照常解释执行）。
    # 编译对象 = 剥白名单 import 后的预处理源码（平台因子惯例是函数内
    # import numpy，numba 不支持 import 语句——与主路径 _njit_accelerate
    # 共用 _compile_for_njit，等价性验证的对象即实际加速的对象）
    nj: dict = {"available": False}
    try:
        import numba  # noqa: F401
        nj["available"] = True
        t0 = time.perf_counter()
        fnj = numba.njit(cache=False)(_compile_for_njit(req["source"]))
        fnj(env_view(small))                    # 首调触发编译
        nj["compile_s"] = round(time.perf_counter() - t0, 2)
        nj["compiled"] = True
        a = np.asarray(fn(small), dtype=float)
        b = np.asarray(fnj(env_view(small)), dtype=float)
        nj["equiv"] = bool(a.shape == b.shape and np.allclose(
            a, b, rtol=EQUIV_RTOL, atol=EQUIV_ATOL, equal_nan=True))
        if nj["equiv"]:
            nj["t_small_s"] = _min_two(lambda: fnj(env_view(small)))
            nj["t_smoke_s"] = _min_two(lambda: fnj(env_view(env)))
    except Exception as e:  # noqa: BLE001 — 编译失败是正常分支（pandas/nan 系）
        nj["compiled"] = False
        nj["error"] = f"{type(e).__name__}: {e}"[:160]
    out["njit"] = nj
    return out


_NUMBA_MEMO: dict = {}

# njit 预处理编译的命名空间：覆盖 `import numpy`（裸名）与
# `import numpy as np` 两种绑定（smoke.njit_prepare 已剥离这些 import 行）
_NJIT_NS_BASE = {"np": np, "pd": pd, "math": __import__("math"),
                 "numpy": np, "pandas": pd}


def _compile_for_njit(source: str):
    """剥白名单 import 后在注入 ns 的命名空间编译——与烟测探针共用同一
    预处理，保证等价性验证的对象就是主路径实际加速的对象。"""
    import math as _math  # noqa: F401 — ns 绑定用
    from .smoke import njit_prepare
    prepared = njit_prepare(source)
    ns = {**_NJIT_NS_BASE, "__name__": "dsh_factor_mining_worker_factor"}
    exec(compile(prepared, "<factor_njit>", "exec"), ns)
    if "factor" not in ns or not callable(ns["factor"]):
        raise ValueError("njit 预处理后缺少可调用 factor")
    return ns["factor"]


def _njit_accelerate(fn, source: str):
    """主执行路径的 njit 包装——仅在烟测门已验证（编译过+严格等价+投影
    进预算）后被 params["smoke_engine"]=="njit" 启用。进程内按源码指纹
    memo；编译/首调任何异常永久回退解释版：numba 层绝不破坏本可运行的
    因子（spike 实测编译失败 0.01-0.4s 无害快速失败）。"""
    try:
        key = source_fingerprint(source) if source else str(id(fn))
    except Exception:
        key = str(id(fn))
    st = _NUMBA_MEMO.get(key)
    if st is None:
        try:
            import numba
            fn_fast = _compile_for_njit(source)
            st = {"mode": "njit", "fn": numba.njit(cache=False)(fn_fast)}
        except Exception as e:  # noqa: BLE001 — numba 缺失/装坏/预处理失败
            st = {"mode": "interp", "why": f"{type(e).__name__}: {e}"[:120]}
        _NUMBA_MEMO[key] = st
    if st["mode"] != "njit":
        return fn

    def _accel(env):
        try:
            return st["fn"](env_view(env))
        except Exception:  # noqa: BLE001 — 编译期/首调异常：回退并永久降级
            st["mode"] = "interp"
            return fn(env)

    return _accel


def _engine_telemetry(params: dict) -> dict:
    """效率编译层遥测并入 perf 块（烟测画像 / njit / 重写溯源）。"""
    t: dict = {}
    sm = params.get("smoke_telemetry")
    if isinstance(sm, dict):
        t["smoke"] = sm
    rw = params.get("engine_rewrite")
    if isinstance(rw, dict):
        t["engine"] = "rewrite:llm-v1"
        t["rewrite_provenance"] = rw
    elif params.get("smoke_engine") == "njit":
        t["engine"] = "njit"
    return t


def run_request(req: dict) -> dict:
    env = load_env_any(req["npzPath"], req.get("pbinPath"))
    method = req["method"]
    # evaluate_batch 是多 source 方法（sources dict），无单个 source——
    # 入口编译跳过，由其分支自行编译每个 source（2026-08-18 修复：
    # 此前无条件编译空串导致 batch 从未成功过）
    fn = _compile(req["source"]) if (req.get("source") and method != "factor.evaluate_batch") else None
    params = req.get("params") or {}
    state_root = params.get("state_root")
    if method == "factor.smoke":
        return _smoke_request(req, fn)
    # 效率编译层（2026-09-01）：烟测门判 njit 可救时主执行也走编译版
    if fn is not None and params.get("smoke_engine") == "njit":
        fn = _njit_accelerate(fn, req.get("source") or "")
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
            # C1（修正案 A 2026-08-28）：development 是纯读——走磁盘缓存。
            # F 仍先算（perf 遥测要诚实的 fn(env) 计时），缓存只免
            # evaluate() 本体的分钟级成本。source_hash/env_fp 由 bridge
            # 注入 params（与桥侧预检同一键空间）；selection/test 是
            # 消耗品语义，一概不缓存。
            out = cached_evaluate(
                F, env, state_root=state_root,
                source_hash=(params.get("source_hash")
                             or source_fingerprint(req["source"])),
                env_fp=params.get("env_fp"),
                horizon=horizon, n_trials=n_trials,
                pool_std=pool_std, bar_sigma=bar_sigma)
        elif stage == "selection":
            out = evaluate_selection(F, env)
        elif stage == "test":
            # test 是消耗品：锁写在用户 state_root，worker 子进程写同一文件，跨调用可见
            out = evaluate_test(F, env, state_root=state_root,
                                source_hash=params.get("source_hash"))
        else:
            raise ValueError(f"worker 不支持的 stage: {stage}")
        if isinstance(out, dict):
            _perf.update(_engine_telemetry(params))
            out["perf"] = _perf
        return out
    if method == "factor.evaluate_composite":
        parts_srcs = params.get("ingredients") or {}
        parts = {}
        for name, src in parts_srcs.items():
            parts[name] = _compile(src)(env)
        # C1：合成本体与成分都过缓存——成分是 trail 里已评估过的源，
        # 命中后 (K+1)×evaluate 降到 1×（个股面板的关键修正）。口径
        # 参数与 factor.evaluate 分支同型归一，键才能对上。
        eval_fn = None
        if state_root:
            horizon = params.get("horizon")
            try:
                horizon = int(horizon) if horizon not in (None, "") else None
            except (TypeError, ValueError):
                horizon = None

            def _num(v):
                return float(v) if isinstance(v, (int, float)) else None

            hashes = {"__composite__": (params.get("source_hash")
                                        or source_fingerprint(req["source"]))}
            for name, src in parts_srcs.items():
                hashes[name] = source_fingerprint(src)

            def eval_fn(tag, G, env_, train_end=None):
                return cached_evaluate(
                    G, env_, state_root=state_root, source_hash=hashes[tag],
                    env_fp=params.get("env_fp"), train_end=train_end,
                    horizon=horizon, n_trials=_num(params.get("n_trials")) or 1.0,
                    pool_std=_num(params.get("pool_std")),
                    bar_sigma=_num(params.get("bar_sigma")))
        return evaluate_composite(fn(env), parts, env, eval_fn=eval_fn)
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
                                   horizon=horizon, pool_std=pool_std,
                                   pbin_path=req.get("pbinPath"))
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
            # R30（2026-08-31 隔离复审）：G2 统计量绑环境口径 tail_k（与
            # bridge in_process 分发同款——此前两处都漏绑恒用默认 0.2）；
            # G2 恒毛口径（dc6a42a），net 定价由 G1/G3 承载
            import functools as _ft
            stat = _ft.partial(spread_ir_statistic,
                               k_frac=float(env.calibration.tail_k))
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
        # W2 延伸（2026-08-29 生产事故）：audit 超时时 agent 无法分清
        # 「因子实现慢」还是「审计机器重」（audit = fn×(1+因果探针) +
        # IC/top-N + 200 次列置换 + 内嵌一次全量 evaluate——当日实测
        # 同面板 evaluate 6.5s 而 audit 315s CPU，agent 被旧超时消息
        # 误导重写了两遍因子源码）。计时闭包只测不重算：fn 每次被调
        # （含因果探针的多次调用）都累计 CPU。
        _tm = {"calls": 0, "cpu": 0.0}

        def _timed(env_):
            _p = time.process_time()
            out = fn(env_)
            _tm["cpu"] += time.process_time() - _p
            _tm["calls"] += 1
            return out

        out = audit(_timed, env)
        if isinstance(out, dict):
            out["perf"] = {
                "factor_calls": _tm["calls"],
                "factor_cpu_s": round(_tm["cpu"], 1),
                "machinery_note": ("audit 机器成本 = 因果探针 + IC/top-N + "
                                   "200 次列置换 + 内嵌全量 evaluate；"
                                   "factor_cpu_s 只是其中因子本体——超时对照"
                                   "它判定该向量化因子还是拆小审计"),
            }
        return out
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
