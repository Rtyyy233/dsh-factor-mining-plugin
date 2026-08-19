# coding=utf-8
"""2026-08-18 晚 生产轨迹审计修复回归。

五个修复点（session.jsonl 实测取证）：
  FIX-1 walk_forward test 泄漏   — fold4/fold5 把 test 区 IC 暴露给入册决策
  FIX-2 explore seed 重放        — seed=42 与 null 校准相同 → 整轮重放、信息量为零
  FIX-3 n_trials 恒 1            — 旧计数用 mining_state.round（agent 从不触发）→ 校正从未生效
  FIX-4 DSR 尺度                 — √(2lnN) 与 IC_IR 不同尺度，需池分布缩放（B-LP 式 4）
  FIX-6 registry_update          — 想改描述只能换名重登（被铁律拦）→ 缺正规通道
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

GOOD = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""

VARIANT = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(60) - 1.0).values
"""

ANOTHER = """
import pandas as pd

def factor(env):
    v = pd.DataFrame(env.v)
    return -v.rolling(10).mean().values
"""


def _panel(path: Path, T=800, N=40, seed=4, start="2019-01-02"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=T)
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


def _setup(root: Path):
    data = root / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)}, "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    load = b.dispatch("data.load", {"envId": "primary"})
    assert load["ok"], load
    return b, load["calibration"]["sel_end"]


# ---- FIX-1: walk_forward 不得暴露 test 区 ----

def test_walk_forward_limited_to_selection_region(tmp_path):
    b, sel_end = _setup(tmp_path)
    wf = b.dispatch("factor.walk_forward", {"envId": "primary", "source": GOOD})
    assert "region_note" in wf and "selection" in wf["region_note"]
    for fold in wf.get("per_fold", []):
        assert str(fold["t1"]) <= str(sel_end), (
            f"fold t1={fold['t1']} 越过 sel_end={sel_end}——test 区泄漏")
    # 显式越界 → 拒绝
    try:
        b.dispatch("factor.walk_forward", {"envId": "primary", "source": GOOD,
                                           "t1_date": "2099-01-01"})
        raise AssertionError("t1 越过 sel_end 应被拒绝")
    except BridgeError as e:
        assert "sel_end" in str(e) or "test" in str(e)


# ---- FIX-2: explore seed 冲突拒绝 + 自动派生 ----

def test_explore_seed_conflict_rejected_and_auto_derived(tmp_path):
    b, _ = _setup(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    # 与 null 校准同 seed → 拒绝
    try:
        b.dispatch("factor.random_generate", {"envId": "primary", "mode": "explore",
                                              "n": 5, "seed": 42})
        raise AssertionError("seed=42 与 null 校准冲突应被拒绝")
    except BridgeError as e:
        assert "重放" in str(e) or "seed" in str(e)
    # 不冲突的显式 seed → 正常
    r = b.dispatch("factor.random_generate", {"envId": "primary", "mode": "explore",
                                              "n": 5, "top_k": 2, "seed": 43})
    assert r["mode"] == "explore" and r["seed"] == 43
    # 无 seed → 自动派生 + seed_note 可复现
    r2 = b.dispatch("factor.random_generate", {"envId": "primary", "mode": "explore",
                                               "n": 5, "top_k": 2})
    assert "seed_note" in r2 and "自动派生" in r2["seed_note"]
    assert r2["seed"] != 42


# ---- FIX-3: n_trials 引擎侧自动统计 ----

def test_evaluate_auto_n_trials_from_trail_engine(tmp_path):
    b, _ = _setup(tmp_path)
    r1 = b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD,
                                        "stage": "development"})
    assert r1["deflated_train"]["n_trials"] == 1
    r2 = b.dispatch("factor.evaluate", {"envId": "primary", "source": VARIANT,
                                        "stage": "development"})
    assert r2["deflated_train"]["n_trials"] == 2, "第二个假设应计 2 次试验"
    r3 = b.dispatch("factor.evaluate", {"envId": "primary", "source": ANOTHER,
                                        "stage": "development"})
    assert r3["deflated_train"]["n_trials"] == 3
    # 重复评估不重复计数
    r2b = b.dispatch("factor.evaluate", {"envId": "primary", "source": VARIANT,
                                         "stage": "development"})
    assert r2b["deflated_train"]["n_trials"] == 3
    # N>1 且池样本 <10 且未校准 null → 拒绝给 p（不给不可信数字）
    assert r2b["deflated_train"].get("p") is None
    assert "pool_std" in r2b["deflated_train"].get("note", "")


def test_evaluate_n_trials_uses_null_landscape_fallback(tmp_path):
    """trail_engine <10 条时用 null 地形分位数估 pool_std → p 正常给出。"""
    b, _ = _setup(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD,
                                   "stage": "development"})
    r2 = b.dispatch("factor.evaluate", {"envId": "primary", "source": VARIANT,
                                        "stage": "development"})
    d = r2["deflated_train"]
    assert d["n_trials"] == 2 and d.get("p") is not None and d.get("pool_std"), d


# ---- FIX-6: registry_update 只开描述通道 ----

def test_registry_update_descriptive_only(tmp_path):
    b, _ = _setup(tmp_path)
    diag = b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD,
                                          "stage": "development"})
    b.dispatch("registry.submit", {"name": "f1", "source": GOOD,
                                   "signal": "旧描述", "diagnosis": diag})
    # 改 signal 成功
    r = b.dispatch("registry.update", {"name": "f1", "signal": "新描述"})
    assert r["ok"] and "signal" in r["changed"]
    reg = b.dispatch("registry.get", {})["registry"]
    assert reg[-1]["signal"] == "新描述"
    # note 追加（历史保留）
    r2 = b.dispatch("registry.update", {"name": "f1", "note": "事后复核备注"})
    assert "note(append)" in r2["changed"]
    assert b.dispatch("registry.get", {})["registry"][-1]["notes"][-1]["note"] == "事后复核备注"
    # 禁改 source（铁律域）
    try:
        b.dispatch("registry.update", {"name": "f1", "source": GOOD})
        raise AssertionError("改 source 应被拒绝")
    except BridgeError as e:
        assert "铁律" in str(e)
    # 禁改数字
    try:
        b.dispatch("registry.update", {"name": "f1", "ic_ir_train": 0.99})
        raise AssertionError("改数字应被拒绝")
    except BridgeError:
        pass
    # 不存在的条目
    try:
        b.dispatch("registry.update", {"name": "ghost", "signal": "x"})
        raise AssertionError("不存在的条目应报错")
    except BridgeError:
        pass
