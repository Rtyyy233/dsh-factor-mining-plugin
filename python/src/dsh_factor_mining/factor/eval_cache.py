# coding=utf-8
"""C1 evaluate 结果磁盘缓存（修正案 A，PLAN-worker-timeout-guidance §6）。

个股层面板（5347×2574）单次 evaluate 达分钟级，而 evaluate_composite
= (K+1) 次全量评估、成分结果 trail 里本就有——重复评估是纯浪费。
本模块只服务纯读的 development 评估；selection（预算消耗品）与
test（一次性锁）一概不缓存，调用方责任。

键 = (source_hash, env 指纹, train_end, horizon, n_trials, pool_std,
bar_sigma)——影响 evaluate 输出的全部自由度。**门参数（bar_sigma/
pool_std/n_trials）必须进键**：deflated 口径是统计门输入，回放旧门
参数 = 用过期的多重检验校正放行因子。代价是桥侧注入的 bar_sigma
（E[max|X|]）随评估后的入池动作一次性漂移——同一 source 的重复
evaluate 因此有"首两次 miss、第三次起稳定命中"的预热形态（实测
2026-08-29）；composite 成分走固定默认口径，无此形态、直接命中。
env 指纹优先用 bridge 注入的 env_fp（_env_full_fingerprint：数据
文件+口径+引擎版本三元组，placebo 缓存同源）；worker 直接跑
request.json 时退化为采样数值指纹（两套指纹不会串键：指纹不同最多
产生重复条目，绝不误命中）。

落盘照抄 placebo_cache 模式：entries+order、FIFO 有界（128——
evaluate 结果比 placebo 抽样胖，取更紧的界）、tmp+os.replace 原子
写、写失败不阻断评估（缓存是加速器不是门输入）。写前递归归一
numpy 标量：json 的 default=str 会把 np.int64 变字符串，缓存读回
就与新鲜结果不同型（np.float64 是 float 子类不受影响，np.int64/
np.bool_ 不是）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

from .evaluate import evaluate

MAX_ENTRIES = 128
CACHE_NAME = "eval_cache.json"


def env_value_fingerprint(env) -> str:
    """worker 侧退化指纹：o/h/l/c/v 采样网格 + 形状的 sha256（桥注入
    env_fp 时不用于键——见模块 docstring 的不串键论证）。"""
    def _grid(a):
        a = np.asarray(a, dtype=np.float64)
        T, N = a.shape
        return np.ascontiguousarray(a[::max(1, T // 37), ::max(1, N // 53)]).tobytes()

    h = hashlib.sha256()
    for arr in (env.o, env.h, env.l, env.c, env.v):
        h.update(_grid(arr))
    dates = getattr(env, "dates", None)
    span = f"{getattr(env, 'T', '?')}|{getattr(env, 'N', '?')}"
    if dates is not None and len(dates):
        span += f"|{dates[0]}|{dates[-1]}"
    h.update(span.encode())
    return h.hexdigest()[:16]


def _jsonify(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return [_jsonify(v) for v in obj.tolist()]
    return obj


def eval_cache_key(*, source_hash, env_fp, train_end, horizon, n_trials,
                   pool_std, bar_sigma) -> str:
    payload = json.dumps({
        "source_hash": source_hash, "env_fp": env_fp,
        "train_end": str(train_end), "horizon": horizon,
        "n_trials": n_trials, "pool_std": pool_std, "bar_sigma": bar_sigma,
    }, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def eval_key_from_parts(source_hash, env_fp, env, *, horizon=None,
                        n_trials=1.0, pool_std=None, bar_sigma=None,
                        train_end=None) -> str:
    """桥/worker 共用的键归一入口——两侧必须走同一归一，否则同一笔
    评估在两层算出不同键（假未命中）。horizon：int 或 None；
    n_trials/pool_std/bar_sigma：float 或 None；train_end：显式值或
    calibration.dev_end（与 evaluate 内部解析同源）。"""
    cal = getattr(env, "calibration", None)
    eff_train = train_end or getattr(cal, "dev_end", None)

    def _f(v, default=None):
        return float(v) if isinstance(v, (int, float)) else default

    try:
        h = int(horizon) if horizon not in (None, "") else None
    except (TypeError, ValueError):
        h = None
    return eval_cache_key(
        source_hash=source_hash or "", env_fp=env_fp,
        train_end=eff_train, horizon=h,
        n_trials=_f(n_trials, 1.0), pool_std=_f(pool_std),
        bar_sigma=_f(bar_sigma))


def cache_read(state_root, key: str):
    if not state_root or not key:
        return None
    try:
        p = Path(state_root) / CACHE_NAME
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        entry = (data.get("entries") or {}).get(key) if isinstance(data, dict) else None
        v = entry.get("value") if isinstance(entry, dict) else None
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def cache_write(state_root, key: str, result: dict) -> None:
    if not state_root or not key or not isinstance(result, dict):
        return
    try:
        p = Path(state_root) / CACHE_NAME
        data = {"entries": {}, "order": []}
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(old, dict) and isinstance(old.get("entries"), dict):
                    data = old
            except Exception:
                pass
        data["entries"][key] = {"at": round(time.time(), 3), "value": _jsonify(result)}
        order = [k for k in data.get("order", []) if k in data["entries"]]
        if key not in order:
            order.append(key)
        while len(order) > MAX_ENTRIES:
            data["entries"].pop(order.pop(0), None)
        data["order"] = order
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception as e:
        try:
            sys.stderr.write(f"[eval_cache] 写入失败（缓存不可用，评估照常）: {e}\n")
        except Exception:
            pass


def cached_evaluate(F, env, *, state_root, source_hash, env_fp=None,
                    train_end=None, horizon=None, n_trials=1.0,
                    pool_std=None, bar_sigma=None, _evaluate=None):
    """read-through evaluate：命中回缓存值（附 eval_cache="hit" 标记），
    未命中算一次并写回。仅 development 口径可用（调用方责任）。

    F 仍由调用方预先计算（worker 的 perf 遥测需要诚实的 fn(env) 计时），
    缓存只免 evaluate() 本体的分钟级成本。"""
    ev = _evaluate or evaluate
    if not state_root or not source_hash:
        return ev(F, env, train_end=train_end, horizon=horizon,
                  n_trials=n_trials, pool_std=pool_std, bar_sigma=bar_sigma)
    fp = env_fp or env_value_fingerprint(env)
    key = eval_key_from_parts(source_hash, fp, env, horizon=horizon,
                              n_trials=n_trials, pool_std=pool_std,
                              bar_sigma=bar_sigma, train_end=train_end)
    hit = cache_read(state_root, key)
    if hit is not None:
        return {**hit, "eval_cache": "hit"}
    out = ev(F, env, train_end=train_end, horizon=horizon, n_trials=n_trials,
             pool_std=pool_std, bar_sigma=bar_sigma)
    if isinstance(out, dict) and not out.get("error"):
        cache_write(state_root, key, out)
    return out
