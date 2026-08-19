# coding=utf-8
"""Causality perturbation tests.

`check_causality` uses the noise-perturbation method of the reference harness:
factor F[t] depends only on data[<=t] iff perturbing data[t0:] never changes
F[:t0].  `check_causality_nan` is the independent NaN-truncation method used
by the audit module.
"""
from __future__ import annotations

import time

import numpy as np

from .env import FactorEnv


def check_causality(factor_fn, env: FactorEnv, t0_samples: int = 6, seed: int = 42, atol: float = 1e-9):
    """Return {"verdict": "causal" | "FUTURE_LEAK", "leak_t0": int|None, "note": str}.

    附带执行计时（perf，效率防御层 2：向量化因子在大面板单次 <2s；>10s 疑似循环实现）
    与非确定性检测（nondeterministic：同 env 双跑不一致——随机无种子/外部状态，
    其高 IC 可能是碰巧，警示不是否决）。
    """
    rng = np.random.default_rng(seed)
    t_start = time.perf_counter()
    F_full = np.asarray(factor_fn(env), dtype=np.float64)
    exec_ms_first = (time.perf_counter() - t_start) * 1000.0
    # H4 非确定性检测：同 env 再跑一次。二跑前重播种全局 legacy RNG——
    # 因子若用 np.random.seed(42) 固定种子，不重播种则两次必然相同、检测恒失效；
    # 重播种后 legacy API 的因子两次不同 → 正确触发警示。
    # （default_rng/Generator 自带独立种子的仍无法拦截，见 note。）
    np.random.seed(seed + 1)
    F_rerun = np.asarray(factor_fn(env), dtype=np.float64)
    exec_ms_avg = exec_ms_first
    nondet = False
    if F_rerun.shape == F_full.shape:
        m = np.isfinite(F_full) & np.isfinite(F_rerun)
        if m.any() and not np.allclose(F_full[m], F_rerun[m], atol=atol, rtol=1e-6):
            nondet = True
    else:
        nondet = True
    perf = {"exec_ms": float(exec_ms_avg), "nondeterministic": nondet}
    if nondet:
        perf["note"] = ("因子非确定性（同环境两次执行结果不同）：若含随机性请固定种子；"
                        "碰巧撞出的高 IC 不可信")
    elif F_rerun.shape == F_full.shape:
        perf["note"] = ("确定性通过（legacy 全局 RNG 已重播种验证）；"
                        "np.random.Generator 自带独立种子的无法用重播种扰动，此检测不覆盖")

    valid_t0 = np.arange(env.T // 4, env.T - 1)
    if len(valid_t0) == 0:
        return {"verdict": "causal", "leak_t0": None,
                "note": "样本太短，未抽样", "perf": perf}
    idx = np.linspace(0, len(valid_t0) - 1, min(t0_samples, len(valid_t0))).astype(int)
    t0_list = valid_t0[idx]

    for t0 in t0_list:
        def _noise(a):
            b = a.copy()
            scale = np.nanstd(a[:t0]) or 1.0
            b[t0:] = rng.normal(0, scale, size=b[t0:].shape)
            return b

        env_p = FactorEnv(
            _noise(env.o), _noise(env.h), _noise(env.l), _noise(env.c), _noise(env.v),
            env.dates, env.symbols, listed=env.listed,
            amount=(_noise(env.amount) if env.amount is not None else None),
            calibration=env.calibration,
        )
        F_p = np.asarray(factor_fn(env_p), dtype=np.float64)

        past_full = F_full[:t0]
        past_p = F_p[:t0]
        if past_full.shape != past_p.shape:
            return {"verdict": "FUTURE_LEAK", "leak_t0": int(t0), "note": "输出形状随扰动改变", "perf": perf}
        mask = np.isfinite(past_full) & np.isfinite(past_p)
        if mask.any() and not np.allclose(past_full[mask], past_p[mask], atol=atol, rtol=1e-6):
            return {
                "verdict": "FUTURE_LEAK",
                "leak_t0": int(t0),
                "note": f"扰动 data[{t0}:] 后 F[:{t0}] 改变 → factor 用了未来数据",
                "perf": perf,
            }
    return {"verdict": "causal", "leak_t0": None,
            "note": f"抽样 {len(t0_list)} 个 t0，全部通过", "perf": perf}


def check_causality_nan(factor_fn, env: FactorEnv, t0_samples: int = 6, seed: int = 42, atol: float = 1e-9):
    """Independent NaN-truncation causality test used by audit."""
    F_full = np.asarray(factor_fn(env), dtype=np.float64)
    T = env.T
    valid_t0 = np.arange(T // 4, T - 1)
    if len(valid_t0) == 0:
        return {"verdict": "causal", "leak_t0": None, "note": "样本太短，未抽样"}
    idx = np.linspace(0, len(valid_t0) - 1, min(t0_samples, len(valid_t0))).astype(int)
    t0_list = valid_t0[idx]

    for t0 in t0_list:
        def _nan_cut(a):
            b = a.copy().astype(np.float64)
            b[t0:] = np.nan
            return b

        def _nan_cut_bool(m):
            # listed 是未来已知的可交易状态吗？截断后 [t0:] 视为不可交易，
            # 若因子偷读未来的 listed，F[:t0] 会因 mask 变化而改变 → 被抓到
            b = m.copy()
            b[t0:] = False
            return b

        env_nan = FactorEnv(
            _nan_cut(env.o), _nan_cut(env.h), _nan_cut(env.l),
            _nan_cut(env.c), _nan_cut(env.v),
            env.dates, env.symbols,
            listed=(_nan_cut_bool(env.listed) if env.listed is not None else None),
            amount=(_nan_cut(env.amount) if env.amount is not None else None),
            calibration=env.calibration,
        )
        F_nan = np.asarray(factor_fn(env_nan), dtype=np.float64)
        past_full = F_full[:t0]
        past_nan = F_nan[:t0]
        if past_full.shape != past_nan.shape:
            return {"verdict": "FUTURE_LEAK", "leak_t0": int(t0), "note": "输出形状随截断改变"}

        direct_leak = np.isfinite(past_full) & ~np.isfinite(past_nan)
        if direct_leak.any():
            return {
                "verdict": "FUTURE_LEAK",
                "leak_t0": int(t0),
                "note": f"NaN截断 data[{t0}:] 后 F[:{t0}] 出现 NaN → 直接索引未来(shift负数)",
            }
        mask = np.isfinite(past_full) & np.isfinite(past_nan)
        if mask.any() and not np.allclose(past_full[mask], past_nan[mask], atol=atol, rtol=1e-6):
            return {
                "verdict": "FUTURE_LEAK",
                "leak_t0": int(t0),
                "note": f"NaN截断 data[{t0}:] 后 F[:{t0}] 改变 → 间接未来泄漏",
            }
    return {"verdict": "causal", "leak_t0": None, "note": f"NaN截断法抽样 {len(t0_list)} 个 t0 全通过"}
