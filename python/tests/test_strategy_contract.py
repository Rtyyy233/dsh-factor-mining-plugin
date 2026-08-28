# coding=utf-8
"""P1 契约测试：signal-only 形状校验、种子注入、放置锁（依赖方向单向）。"""
import numpy as np
import pytest

from dsh_strategy_lab.contract import (
    StrategyError,
    check_namespace,
    compile_strategy,
    inject_seed,
    run_apply,
    validate_weight_path,
)
from _strategy_fixtures import make_env


GOOD = """
import numpy as np

def fit(env):
    return {"mean": np.nanmean(env.c, axis=0)}

def apply(state, env):
    base = float(np.nanmean(state["mean"]))
    out = []
    for t in range(env.T):
        w = {}
        for j, s in enumerate(env.symbols):
            if env.c[t, j] > base * 1.5:
                w[s] = 0.25
        out.append(w)
    return out
"""


def test_compile_and_namespace():
    ns = compile_strategy(GOOD)
    check_namespace(ns)
    with pytest.raises(StrategyError, match="apply"):
        compile_strategy("def fit(env):\n    return 1\n")
    with pytest.raises(StrategyError, match="策略源为空"):
        compile_strategy("   ")
    ns2 = {"apply": lambda s, e: [], "fit": "not-callable"}
    with pytest.raises(StrategyError, match="fit"):
        check_namespace(ns2)


def test_validate_weight_path_shapes():
    env = make_env(T=10, N=3)
    ok = validate_weight_path([{} for _ in range(10)], env)
    assert ok == [dict() for _ in range(10)]
    with pytest.raises(StrategyError, match="list"):
        validate_weight_path({"E0": 0.5}, env)          # 单截面 dict = 违规
    with pytest.raises(StrategyError, match="9 != env.T"):
        validate_weight_path([{}] * 9, env)
    with pytest.raises(StrategyError, match="未知资产"):
        validate_weight_path([{"NOPE": 0.5}] + [{}] * 9, env)
    with pytest.raises(StrategyError, match="非有限"):
        validate_weight_path([{"E0": float("nan")}] + [{}] * 9, env)
    with pytest.raises(StrategyError, match="权重类型"):
        validate_weight_path([{"E0": "0.5"}] + [{}] * 9, env)
    with pytest.raises(StrategyError, match="权重类型"):
        validate_weight_path([{"E0": True}] + [{}] * 9, env)
    # int 权重规范化为 float
    out = validate_weight_path([{"E0": 1}] + [{}] * 9, env)
    assert out[0] == {"E0": 1.0} and isinstance(out[0]["E0"], float)


def test_run_apply_seed_injection_determinism():
    env = make_env(T=12, N=3, seed=11)
    src = """
import numpy as np

def apply(state, env):
    w = np.random.rand(env.T, len(env.symbols))
    w = w / (w.sum(axis=1, keepdims=True) * 2.0)
    return [{s: float(w[t, j]) for j, s in enumerate(env.symbols)}
            for t in range(env.T)]
"""
    ns = compile_strategy(src)
    a = run_apply(ns, None, env, seed=42)
    b = run_apply(ns, None, env, seed=42)
    c = run_apply(ns, None, env, seed=43)
    assert a == b                       # 同 seed 双跑逐位一致（注入生效）
    assert a != c                       # 异 seed 不同（确实走了全局 RNG）


def test_placement_lock_factor_package_never_imports_strategy():
    """依赖方向物理单向（规划 §1）：因子包源码树不得 import dsh_strategy_lab
    （docstring 提及不算——只锁 import 语句）。"""
    import re
    from pathlib import Path
    import dsh_factor_mining
    root = Path(dsh_factor_mining.__file__).parent
    pat = re.compile(r"^\s*(import\s+dsh_strategy_lab|from\s+dsh_strategy_lab)",
                     re.MULTILINE)
    violations = [str(p) for p in root.rglob("*.py")
                  if "__pycache__" not in str(p)
                  and pat.search(p.read_text(encoding="utf-8"))]
    assert not violations, f"反向依赖出现：{violations}"


def test_inject_seed_covers_both_rngs():
    inject_seed(99)
    a1, a2 = np.random.rand(), np.random.rand()
    import random
    b1 = random.random()
    inject_seed(99)
    assert np.random.rand() == a1 and np.random.rand() == a2
    assert random.random() == b1
