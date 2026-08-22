# coding=utf-8
"""自主性停走接线回归（2026-08-21）。

事故背景（trail.json round 8/9 实证）：agent 在里程碑时刻主动停笔
（「本次探索结束」「等待用户决定是否清 trail 重置后重新注册」），
或写了 next_hypothesis 不执行——用户被迫用固定话术手动推动。

修复：引擎响应注入 loop 指令（state=running/may_stop + obligation +
escalation 分级升级 + submit 拒绝菜单）。本文件锁定该行为。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _make_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
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


def _write_engine_trail(state_root: Path, sketches: list[list[float]]) -> None:
    """直写 trail_engine.json（家族 streak 测试用——sketch 同族即链长）。"""
    entries = []
    for i, s in enumerate(sketches):
        entries.append({"ts": f"2026-08-21T20:00:{i:02d}", "envId": "primary",
                        "source_hash": f"hash{i:03d}", "stage": "development",
                        "horizon": 20, "ic_ir": 0.1,
                        "ic_series_sketch": s})
    (state_root / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _family_sketches(n: int, seed: int = 7) -> list[list[float]]:
    """同族 sketch：共同基底 + 微噪声（|ρ| > 0.9）。"""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(64)
    return [list(0.95 * base + 0.05 * rng.standard_normal(64)) for _ in range(n)]


def _indep_sketches(n: int, seed: int = 9) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    return [list(rng.standard_normal(64)) for _ in range(n)]


# ---- 1. loop 字段存在性与冷启动 running ----

def test_loop_on_status_and_evaluate(tmp_path):
    b = _make_bridge(tmp_path)
    st = b.dispatch("status", {})
    assert "loop" in st, "status 缺 loop 指令"
    assert st["loop"]["state"] == "running"
    assert st["loop"]["obligation"] is not None  # 冷启动：继续内循环

    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": BASELINE_SOURCE,
                       "stage": "development"})
    assert "loop" in diag, "evaluate 响应缺 loop 指令"
    assert diag["loop"]["state"] in ("running", "may_stop")
    assert diag["loop"]["n_trials"] >= 1  # 本次评估已入 trail


# ---- 2. pending obligation：上一轮 next_hypothesis 原样回显 ----

def test_loop_pending_obligation(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 1, "signal": "momentum",
        "attribution": "IC_IR 弱",
        "next_hypothesis": "换高阶矩维度（skew/kurtosis）",
        "new_information": "无"}})
    loop = b._loop_directive()
    assert loop["state"] == "running"
    assert loop["pending_hypothesis"] == "换高阶矩维度（skew/kurtosis）"
    assert "换高阶矩维度" in loop["obligation"], loop["obligation"]
    assert "不得静默放弃" in loop["obligation"]


# ---- 3. 家族饱和三级升级（trail.json round 3→6 事故的引擎化） ----

def test_family_streak_escalation_levels(tmp_path):
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    # 3 条同族 → 一级（换构造思路）
    _write_engine_trail(state, _family_sketches(3))
    loop = b._loop_directive()
    assert loop["family_streak"] == 3
    assert loop["escalation"] is not None and "构造思路" in loop["escalation"]
    # 6 条 → 二级（换信息源维度）
    _write_engine_trail(state, _family_sketches(6))
    loop = b._loop_directive()
    assert loop["family_streak"] == 6
    assert "信息源" in loop["escalation"]
    # 9 条 → 三级（必须 arxiv）
    _write_engine_trail(state, _family_sketches(9))
    loop = b._loop_directive()
    assert loop["family_streak"] == 9
    assert "arxiv" in loop["escalation"]
    # 独立试验 → 无升级（换方向后链断，新方向预算重新起算）
    _write_engine_trail(state, _indep_sketches(9))
    loop = b._loop_directive()
    assert loop["escalation"] is None


# ---- 4. may_stop：accepted 入册后允许收尾 ----

def test_loop_may_stop_after_accepted(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "strong_a", "signal": "momentum",
        "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is True, sub
    assert sub["loop"]["state"] == "may_stop"
    assert "accepted" in sub["loop"]["stop_reason"]


# ---- 5. submit 拒绝路径：战略菜单 + running（不停下来问用户） ----

def test_submit_rejection_menu(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    # 弱因子：deflated p 必然拒绝
    sub = b.dispatch("registry.submit", {
        "name": "weak_probe", "signal": "weak",
        "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is False
    assert "loop" in sub and sub["loop"]["state"] == "running"
    nm = sub.get("next_moves")
    assert nm is not None, "拒绝响应缺 next_moves 菜单"
    assert any("结构性新假设" in o for o in nm["options"]), nm
    assert any("stateRoot" in o for o in nm["options"]), nm
    assert any("假门" in f for f in nm["forbidden"]), nm
    assert any("停下来问用户" in f for f in nm["forbidden"]), nm


# ---- 6. batch / trail_summary 响应带 loop ----

def test_loop_on_batch_and_summary(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    res = b.dispatch("factor.evaluate_batch", {
        "envId": "primary", "horizon": 20,
        "sources": {
            "v1": BASELINE_SOURCE,
            "v2": BASELINE_SOURCE.replace("shift(20)", "shift(21)")}})
    assert "loop" in res, "batch 响应缺 loop 指令"
    # 近邻窗口变体（shift 20 vs 21）：IC 序列高相关 = 真实同族 → streak ≥ 2
    assert res["loop"]["family_streak"] >= 2, res["loop"]

    summary = b.dispatch("state.trail_summary", {})
    assert "loop" in summary, "trail_summary 缺 loop 指令"
    assert summary["loop"]["state"] == "running"
