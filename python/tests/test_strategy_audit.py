# coding=utf-8
"""P2 审计测试（规划 §8.1 立身测试 + 扰动族结构）。

- 截断不变性：确定性正例通过；**故意前视负例必须被抓**。
- 确定性：自带独立种子的实现被抓。
- 延迟退化：均值回复真信号温和退化（pass）；不退化 = 红旗。
- 日置换 / 随机游走：真策略崩向 null（|z| < 3）。
"""
import numpy as np
import pytest

from dsh_strategy_lab.audit import (
    audit_full,
    day_permutation,
    delay_verdict,
    determinism_check,
    permute_env,
    random_walk_panel,
    run_pipeline,
    truncation_invariance,
)
from dsh_strategy_lab.contract import compile_strategy
from dsh_strategy_lab.simulator import simulate
from _strategy_fixtures import make_env

# 真因果策略：买昨日的最大输家（在 ar<0 均值回复面板上有真信号）
CAUSAL_MR = """
import numpy as np

def apply(state, env):
    out = []
    for t in range(env.T):
        w = {}
        if t >= 1:
            rets = env.c[t] / env.c[t - 1] - 1.0
            ok = np.isfinite(rets) & (env.c[t - 1] > 0)
            if ok.any():
                j = int(np.nanargmin(np.where(ok, rets, np.inf)))
                w[env.symbols[j]] = 1.0
        out.append(w)
    return out
"""

# 故意前视：每个 bar 都用**最后一行**收盘价选股
LOOKAHEAD = """
import numpy as np

def apply(state, env):
    last = env.c[-1]
    out = []
    for t in range(env.T):
        ratio = last / env.c[t]
        j = int(np.nanargmax(np.where(np.isfinite(ratio), ratio, -np.inf)))
        out.append({env.symbols[j]: 1.0})
    return out
"""

# 非确定：自带无种子独立 RNG（harness 的全局重播种覆盖不到）
NONDET = """
import random

def apply(state, env):
    r = random.Random()
    return [{env.symbols[r.randrange(env.N)]: 1.0} for _ in range(env.T)]
"""

AR_KW = dict(T=80, N=6, seed=21, ar=-0.55)


def test_truncation_invariance_positive_and_negative():
    env = make_env(**AR_KW)
    ns = compile_strategy(CAUSAL_MR)
    res = truncation_invariance(ns, env, seed=42)
    assert res["verdict"] == "causal"
    # 立身测试：前视负例必须被抓（fail-closed，带可行动定位）
    ns_bad = compile_strategy(LOOKAHEAD)
    res_bad = truncation_invariance(ns_bad, env, seed=42)
    assert res_bad["verdict"] == "FUTURE_LEAK"
    assert res_bad["leak_t"] is not None and "未来" in str(res_bad["note"])


def test_determinism_check():
    env = make_env(T=30, N=3, seed=5)
    assert determinism_check(compile_strategy(CAUSAL_MR), env, 42)["verdict"] \
        == "deterministic"
    assert determinism_check(compile_strategy(NONDET), env, 42)["verdict"] \
        == "NONDETERMINISTIC"


def test_delay_verdict_rules():
    assert delay_verdict(1.5, 1.2)["verdict"] == "degraded"
    assert delay_verdict(1.5, 1.5)["verdict"] == "NO_DEGRADATION"
    assert delay_verdict(1.0, 1.3)["verdict"] == "NO_DEGRADATION"
    assert delay_verdict(None, 1.0)["verdict"] == "undecidable"
    assert delay_verdict(1.0, None)["verdict"] == "undecidable"


def test_delay_degrades_mean_reversion_edge():
    """均值回复信号 +1 bar 延迟应温和退化（真信号随延迟衰减）。"""
    env = make_env(**AR_KW)
    ns = compile_strategy(CAUSAL_MR)
    path = run_pipeline(ns, env, seed=42)
    base = simulate(env, path)
    assert base["metrics"]["sharpe"] > 1.0        # 面板上确有真信号（前置）
    from dsh_strategy_lab.audit import delay_check
    out = delay_check(env, path)
    assert out["verdict"] == "degraded", out
    assert out["delayed_sharpe"] < out["base_sharpe"]


def test_day_permutation_null_collapses_edge():
    env = make_env(**AR_KW)
    ns = compile_strategy(CAUSAL_MR)
    out = day_permutation(ns, env, seed=42, m=20)
    assert out["n_valid"] >= 2 and out["z"] is not None
    # 时间对齐破坏后信号崩向 null：均值不再显著为正
    assert out["z"] < 3.0
    assert abs(out["mean"]) < 1.0


def test_random_walk_panel_null():
    env = make_env(**AR_KW)
    ns = compile_strategy(CAUSAL_MR)
    out = random_walk_panel(ns, env, seed=42, m=10)
    assert out["n_valid"] >= 2 and out["z"] is not None
    assert out["z"] < 3.0          # G2′：假价格上无显著正收益


def test_permute_env_structure():
    env = make_env(T=40, N=3, seed=9)
    rng = np.random.default_rng(0)
    penv = permute_env(env, rng)
    # 截面结构保留：每个 bar 的收盘向量是原面板某行的原样搬运
    orig_rows = {tuple(env.c[t]) for t in range(env.T)}
    perm_rows = {tuple(penv.c[t]) for t in range(penv.T)}
    assert orig_rows == perm_rows
    # dates 随行走（行序置换，时间对齐破坏）
    assert sorted(str(d) for d in penv.dates) == sorted(str(d) for d in env.dates)
    assert list(penv.dates) != list(env.dates)


def test_audit_full_orchestration():
    env = make_env(**AR_KW)
    good = audit_full(compile_strategy(CAUSAL_MR), env, seed=42,
                      day_perm_m=8, rw_m=5)
    assert good["g0_pass"] is True
    assert good["base_path"] is not None
    bad = audit_full(compile_strategy(LOOKAHEAD), env, seed=42,
                     day_perm_m=8, rw_m=5)
    assert bad["g0_pass"] is False
    assert bad["truncation"]["verdict"] == "FUTURE_LEAK"
    assert "day_perm" not in bad          # 程序性拒收不烧扰动预算
