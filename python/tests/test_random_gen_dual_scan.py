# coding=utf-8
"""explore 双轨扫描回归（2026-08-27 规划书 WS-C / D3）。

锁定行为：
- light_ic_scan 追加 spread_ir（与 null 校准 spread 段同口径）与
  spread_pct（landscape 在场时的分位查表）；函数名与旧返回键兼容
- explore 返回双列表：top（IC 线，现行）+ top_tail（spread_ir 排序）；
  重叠如实报告；同树两指标一致；无 landscape 时 spread_pct 缺省不炸
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.random_gen import (  # noqa: E402
    light_ic_scan, spread_percentile)
from dsh_factor_mining.factor.env import FactorEnv  # noqa: E402


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


# ---- 1. spread_percentile 纯函数 ----

_KNOTS = {"p10": -0.05, "p25": 0.0, "p50": 0.05, "p75": 0.10,
          "p90": 0.16, "p95": 0.22}


def test_spread_percentile_interpolation():
    # 低于最低 knot → 该 knot 位
    assert spread_percentile(-0.2, _KNOTS) == 10.0
    # p50 恰好 → 50
    assert spread_percentile(0.05, _KNOTS) == 50.0
    # p50-p75 中点 → 62.5
    assert spread_percentile(0.075, _KNOTS) == 62.5
    # 高于 p95 → 封顶 100（不冒充尾部外推）
    assert spread_percentile(0.9, _KNOTS) == 100.0
    # 值缺省 / 段缺省 / knots 不足
    assert spread_percentile(None, _KNOTS) is None
    assert spread_percentile(0.05, None) is None
    assert spread_percentile(0.05, {"p50": 0.05}) is None


# ---- 2. light_ic_scan 双指标 ----

def test_light_ic_scan_spread_keys():
    """返回结构：旧键不变 + spread_ir/spread_pct 新键（无参照时
    spread_pct=None）。"""
    env = _synthetic_env()
    c = pd.DataFrame(env.c)
    F = (c / c.shift(20) - 1.0).values
    r = light_ic_scan(F, env)
    assert r["n"] > 0 and r["ic_ir"] is not None
    assert "spread_ir" in r and "spread_pct" in r
    assert isinstance(r["spread_ir"], (int, float, type(None)))
    assert r["spread_pct"] is None  # 无 spread_ref
    r2 = light_ic_scan(F, env, spread_ref=_KNOTS)
    if r2["spread_ir"] is not None:
        assert 0 <= r2["spread_pct"] <= 100, r2


def test_light_ic_scan_spread_matches_null_calibration_scale():
    """同口径对照：light_ic_scan 的 spread_ir 与 null 校准 spread 段
    量级一致（同一面板同一 train 边界；null 分布以 0 为中心，单因子
    值散布其周边——只验数值有限与键存在，不验符号）。"""
    import tempfile
    from dsh_factor_mining.factor.random_gen import run_null_calibration
    env = _synthetic_env()
    c = pd.DataFrame(env.c)
    F = (c / c.shift(20) - 1.0).values
    r = light_ic_scan(F, env)
    with tempfile.TemporaryDirectory() as d:
        land = run_null_calibration(env, d, n=8, seed=42)
        main = str(env.calibration.horizon)
        seg = land["spread"][main]
        assert seg["p50"] is not None
        ref = {k: seg.get(k) for k in
               ("p10", "p25", "p50", "p75", "p90", "p95")}
        r2 = light_ic_scan(F, env, spread_ref=ref)
        if r2["spread_ir"] is not None:
            assert 0 <= r2["spread_pct"] <= 100, r2


# ---- 3. explore 双列表（经 Bridge） ----

def _make_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 500, 30
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        cl = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(cl[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


def test_explore_dual_lists(tmp_path):
    """双列表存在且各自排序正确；同树两指标一致；重叠如实报告。"""
    b = _make_bridge(tmp_path)
    out = b.dispatch("factor.random_generate", {
        "envId": "primary", "mode": "explore", "n": 12, "top_k": 3,
        "seed": 7})
    assert out["mode"] == "explore"
    assert "top" in out and "top_tail" in out, out.keys()
    assert "overlap_indexes" in out
    # IC 列表按 |ic_ir| 降序
    ics = [abs(r["light_ic"]["ic_ir"] or 0.0) for r in out["top"]]
    assert ics == sorted(ics, reverse=True), ics
    # 尾部列表按 spread_ir 降序，且无 spread 的树不进列表
    sps = [r["light_ic"]["spread_ir"] for r in out["top_tail"]]
    assert all(s is not None for s in sps), sps
    assert sps == sorted(sps, reverse=True), sps
    # 同树两指标一致
    ic_by_idx = {r["index"]: r for r in out["top"]}
    for r in out["top_tail"]:
        if r["index"] in ic_by_idx:
            assert ic_by_idx[r["index"]]["light_ic"] == r["light_ic"]
            assert ic_by_idx[r["index"]]["source"] == r["source"]
    # 重叠集合与列表一致
    expected_overlap = sorted(set(ic_by_idx)
                              & {r["index"] for r in out["top_tail"]})
    assert out["overlap_indexes"] == expected_overlap, out["overlap_indexes"]
    # 每树轻量诊断带双指标键
    for r in out["top"] + out["top_tail"]:
        assert "spread_ir" in r["light_ic"]
        assert "spread_pct" in r["light_ic"]


def test_explore_no_landscape_spread_pct_absent_no_crash(tmp_path):
    """无 landscape：spread_pct=None（不炸），双列表照常。"""
    b = _make_bridge(tmp_path)
    out = b.dispatch("factor.random_generate", {
        "envId": "primary", "mode": "explore", "n": 8, "top_k": 2,
        "seed": 11})
    for r in out["top"] + out["top_tail"]:
        assert r["light_ic"]["spread_pct"] is None, r["light_ic"]


def test_explore_with_landscape_spread_pct_present(tmp_path):
    """landscape 在场且指纹匹配 → spread_pct 可用（0-100）。"""
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration",
                                          "n": 6, "seed": 42})
    out = b.dispatch("factor.random_generate", {
        "envId": "primary", "mode": "explore", "n": 10, "top_k": 3,
        "seed": 99})  # seed != 42：避免与 null 校准前缀重放被拒
    scored = [r for r in out["top"] + out["top_tail"]
              if r["light_ic"]["spread_ir"] is not None]
    assert scored, out
    for r in scored:
        pct = r["light_ic"]["spread_pct"]
        assert pct is None or (0 <= pct <= 100), r["light_ic"]
    assert any(r["light_ic"]["spread_pct"] is not None for r in scored), \
        "指纹匹配的 landscape 在场时应有分位得分"


if __name__ == "__main__":
    import tempfile
    for fn in [test_spread_percentile_interpolation,
               test_light_ic_scan_spread_keys,
               test_light_ic_scan_spread_matches_null_calibration_scale]:
        fn()
        print(f"PASS {fn.__name__}")
    for fn in [test_explore_dual_lists,
               test_explore_no_landscape_spread_pct_absent_no_crash,
               test_explore_with_landscape_spread_pct_present]:
        with tempfile.TemporaryDirectory() as d:
            fn(Path(d))
        print(f"PASS {fn.__name__}")
    print("RANDOM_GEN_DUAL_SCAN_TESTS PASS")
