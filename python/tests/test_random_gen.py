# coding=utf-8
"""随机因子生成器测试 — 结构约束、可复现、null 地形、源码可执行。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.factor.random_gen import (
    effective_operator_set, generate_tree, light_ic_scan,
    read_null_landscape, render_factor_source, run_null_calibration,
    write_operator_override,
)
from dsh_factor_mining.factor.ops import OPERATOR_REGISTRY, LEAVES
from dsh_factor_mining.factor.env import FactorEnv


def _synthetic_env(T=400, N=40, seed=4):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=T)
    c = 10 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, (T, N)), axis=0))
    o = c * (1 + rng.normal(0, 0.002, (T, N)))
    h = np.maximum(o, c) * 1.001
    l = np.minimum(o, c) * 0.999
    v = rng.lognormal(12, 0.4, (T, N))
    amount = v * c
    return FactorEnv(o, h, l, c, v, dates, [f"S{i:02d}" for i in range(N)],
                     listed=np.ones((T, N), bool), amount=amount)


def _tree_ops(node, acc):
    if node.category != "leaf":
        acc.add(node.op)
    for ch in node.children:
        _tree_ops(ch, acc)
    return acc


def test_structure_constraints():
    """至少 1 ts + 1 cs 算子；二元子树不全是叶子。"""
    opset = effective_operator_set()
    rng = np.random.default_rng(7)
    for _ in range(50):
        tree = generate_tree(rng, opset)
        ops = _tree_ops(tree, set())
        cats = set()
        for cat, entries in OPERATOR_REGISTRY.items():
            if ops & entries.keys():
                cats.add(cat)
        assert "ts" in cats, f"缺时序算子: {tree.to_expression()}"
        assert "cs" in cats, f"缺截面算子: {tree.to_expression()}"

    def _check_binary(node):
        if node.category == "binary":
            assert not all(ch.category == "leaf" for ch in node.children), \
                f"二元子树全叶: {node.to_expression()}"
        for ch in node.children:
            _check_binary(ch)
    rng = np.random.default_rng(8)
    for _ in range(30):
        _check_binary(generate_tree(rng, opset))


def test_reproducible():
    """同 seed 同树序列。"""
    opset = effective_operator_set()
    a = [generate_tree(np.random.default_rng(42), opset).to_expression() for _ in range(10)]
    b = [generate_tree(np.random.default_rng(42), opset).to_expression() for _ in range(10)]
    assert a == b


def test_render_source_executes():
    """渲染出的 factor 源码可被编译执行，输出 (T,N)。

    2026-08-18 契约修正：函数名固定 factor（下游 causality/evaluate/batch 契约），
    tree id 移入注释保留可读性。
    """
    env = _synthetic_env()
    opset = effective_operator_set()
    tree = generate_tree(np.random.default_rng(3), opset)
    src, _imports = render_factor_source(tree, "random_factor_test")
    assert "def factor(env):" in src
    assert "# tree: random_factor_test" in src
    ns = {}
    exec(compile(src, "<random_factor>", "exec"), ns)
    F = ns["factor"](env)
    assert F.shape == (env.T, env.N)
    assert np.isfinite(F).any()


def test_operator_override():
    """用户覆盖：禁用算子 + 覆盖窗口。"""
    with tempfile.TemporaryDirectory() as d:
        eff = write_operator_override({"disable": ["ts_kurt", "sigmoid"], "windows": [10, 20]}, d)
        assert "ts_kurt" not in eff["ops"]["ts"]
        assert "sigmoid" not in eff["ops"]["unary"]
        assert eff["windows"] == [10, 20]
        # 生成的树不再使用被禁算子
        rng = np.random.default_rng(1)
        for _ in range(20):
            ops = _tree_ops(generate_tree(rng, eff), set())
            assert "ts_kurt" not in ops and "sigmoid" not in ops


def test_null_calibration_persists():
    """null 地形：分位数结构完整 + 持久化 + 可读回。"""
    env = _synthetic_env()
    with tempfile.TemporaryDirectory() as d:
        r = run_null_calibration(env, d, n=10, seed=42)
        assert r["n_generated"] == 10
        assert r["n_valid"] > 0
        for k in ("p50", "p95"):
            assert r["ic_ir"][k] is not None
        back = read_null_landscape(d)
        assert back is not None and back["ic_ir"]["p95"] == r["ic_ir"]["p95"]


def test_light_ic_scan_shape():
    """轻量 IC 返回结构。"""
    env = _synthetic_env()
    c = pd.DataFrame(env.c)
    F = (c / c.shift(20) - 1.0).values
    r = light_ic_scan(F, env)
    assert r["n"] > 0
    assert r["ic_ir"] is not None


if __name__ == "__main__":
    failures = []
    for fn in [test_structure_constraints, test_reproducible,
               test_render_source_executes, test_operator_override,
               test_null_calibration_persists, test_light_ic_scan_shape]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e!r}")
    if failures:
        raise SystemExit(1)
    print("RANDOM_GEN_TESTS PASS")
