# coding=utf-8
"""2026-08-18 第三轮生产审计修复回归。

四项（21:17 实测取证）：
  A. evaluate_batch 从未工作过 — 入口无条件编译空 source 必崩（两种执行模式）
  B. 生成器渲染 def random_factor_N(env) 违反 factor(env) 契约 — 每轮 5 次 ERR
  C. 同名重复入册未拦截 — registry 被同一因子灌 3 条
  D. 编译错误信息无格式指引 — agent 只能盲试
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402
from dsh_factor_mining.factor import random_gen  # noqa: E402

GOOD = "def factor(env):\n    import pandas as pd\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
GOOD2 = "def factor(env):\n    import pandas as pd\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(60) - 1.0).values\n"


def _panel(path: Path, T=900, N=40, seed=4):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}", "open": p * 1.001,
                         "high": p * 1.01, "low": p * 0.99, "close": p,
                         "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(path)


def _setup(root: Path, mode="in_process"):
    data = root / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(root / "state"), execution_mode=mode)
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "etf": {"source": {"type": "parquet", "path": str(data)}, "layout": "long",
                "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                            "low": "low", "close": "close", "volume": "volume",
                            "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "etf"})
    return b


# ---- A: batch 双模式可用 ----

def test_evaluate_batch_works_in_process(tmp_path):
    b = _setup(tmp_path, "in_process")
    r = b.dispatch("factor.evaluate_batch", {"envId": "etf", "sources": {"f1": GOOD, "f2": GOOD2}})
    facs = r.get("factors") or {}
    assert set(facs) == {"f1", "f2"}, r.keys()
    assert "deflated_p" in str(facs["f1"]) or facs["f1"].get("deflated_train"), "batch 应带批次校正"


def test_evaluate_batch_works_worker(tmp_path):
    b = _setup(tmp_path, "worker")
    r = b.dispatch("factor.evaluate_batch", {"envId": "etf", "sources": {"f1": GOOD, "f2": GOOD2}})
    assert set((r.get("factors") or {})) == {"f1", "f2"}


# ---- B: 生成器输出符合 factor(env) 契约 ----

def test_rendered_source_defines_factor(tmp_path):
    b = _setup(tmp_path)
    r = b.dispatch("factor.random_generate", {"envId": "etf", "mode": "explore",
                                              "n": 6, "top_k": 2})
    assert r["top"], "explore 应返回 top 源码"
    for item in r["top"]:
        src = item["source"]
        assert "def factor(env):" in src, src[:80]
        # 渲染产物直接可编译（causality 契约）
        Bridge._compile_factor(src)
    # tree id 保留在注释
    assert "# tree:" in r["top"][0]["source"]


# ---- C: 同名重复入册拦截 ----

def test_same_name_resubmit_rejected(tmp_path):
    b = _setup(tmp_path)
    diag = b.dispatch("factor.evaluate", {"envId": "etf", "source": GOOD, "stage": "development"})
    b.dispatch("registry.submit", {"name": "f1", "source": GOOD, "signal": "d1", "diagnosis": diag})
    # 同名同 hash（想改描述）
    try:
        b.dispatch("registry.submit", {"name": "f1", "source": GOOD, "signal": "d2", "diagnosis": diag})
        raise AssertionError("同名同因子重复提交应被拒")
    except BridgeError as e:
        assert "registry_update" in str(e) or "重复" in str(e)
    # 同名不同 hash（换汤不换药）
    try:
        b.dispatch("registry.submit", {"name": "f1", "source": GOOD2, "signal": "d3", "diagnosis": diag})
        raise AssertionError("同名不同源码应被拒")
    except BridgeError as e:
        assert "名字" in str(e) or "换" in str(e)
    # registry 只有一条
    reg = b.dispatch("registry.get", {})["registry"]
    assert len([e for e in reg if e["name"] == "f1"]) == 1
    # 正确通道：update 改描述
    upd = b.dispatch("registry.update", {"name": "f1", "signal": "fixed desc"})
    assert upd["ok"]


# ---- D: 编译错误信息带格式指引 ----

def test_compile_error_message_actionable():
    try:
        Bridge._compile_factor("def random_factor_45(env):\n    return None")
        raise AssertionError("应抛错")
    except BridgeError as e:
        msg = str(e)
        assert "factor(env)" in msg and ("random_generate" in msg or "命名" in msg), msg
