# coding=utf-8
"""独立审计（AUDIT-VERIFICATION-2026-08-18）三项发现的核实与修复回归。

核实结论：
  F-TP-01（指控 P0）: 误报——完整绕过链（reset all → 重配 → load → 再消费；
    换口径再消费）全部仍被 -32003 拒。审计者把「reset 后立即 evaluate 报
    -32001 数据缺失」误判为绕过。修复仅 UX：lock 检查前置（本文件锁定）。
  F-D11（P2）: 属实——深度 6 存在（自测 200 树统计 19.5%），定性为 docstring
    契约与实现不一致（边界行为有界且有意），已对齐 docstring。
  F-A1-SEM（P3）: 属实——receipt_verified 出口三态（None=无 receipt 手构），
    已 bool 化（None→False）。
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


F = "def factor(env):\n    import pandas as pd\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"


def _env(data):
    return {"source": {"type": "parquet", "path": str(data)}, "layout": "long",
            "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                        "low": "low", "close": "close", "volume": "volume",
                        "amount": "amount"},
            "calibration": {"dev_end": "2020-06-01", "sel_end": "2021-06-01"}}


def test_full_bypass_chain_blocked(tmp_path):
    """F-TP-01 核实：reset all 后完整绕过链（重配+load；换口径）全部被 -32003 拒。"""
    data = tmp_path / "panel.parquet"
    _panel(data)
    state = tmp_path / "state"
    b = Bridge(state_root=str(state), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": _env(data)}}})
    b.dispatch("data.load", {"envId": "etf"})

    r1 = b.dispatch("factor.evaluate", {"envId": "etf", "source": F, "stage": "test"})
    assert "error" not in r1, r1  # 首次消费成功
    assert (state / "test_lock.json").exists()

    b.dispatch("state.reset", {"scope": "all"})
    assert (state / "test_lock.json").exists()  # 锁存活

    # 重配 + load 后再消费 → 必须 -32003（审计者未走完的半条链）
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": _env(data)}}})
    b.dispatch("data.load", {"envId": "etf"})
    try:
        b.dispatch("factor.evaluate", {"envId": "etf", "source": F, "stage": "test"})
        raise AssertionError("重配后 test 消费未被拒——绕过成立")
    except BridgeError as e:
        assert e.code == -32003, e.code

    # 换口径（审计指控的核心场景）再消费 → 同样必须拒
    env2 = _env(data)
    env2["calibration"] = {"dev_end": "2019-12-01", "sel_end": "2020-12-01"}
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": env2}}})
    b.dispatch("data.load", {"envId": "etf"})
    try:
        b.dispatch("factor.evaluate", {"envId": "etf", "source": F, "stage": "test"})
        raise AssertionError("换口径后 test 消费未被拒——绕过成立")
    except BridgeError as e:
        assert e.code == -32003, e.code


def test_lock_error_precedes_missing_config(tmp_path):
    """F-TP-01 UX 修复：reset all 后（无 config 无 env）立即 evaluate(test)
    必须报 -32003（纪律锁）而非 -32001（数据缺失）——消除误读空间。"""
    data = tmp_path / "panel.parquet"
    _panel(data)
    state = tmp_path / "state"
    b = Bridge(state_root=str(state), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": _env(data)}}})
    b.dispatch("data.load", {"envId": "etf"})
    b.dispatch("factor.evaluate", {"envId": "etf", "source": F, "stage": "test"})
    b.dispatch("state.reset", {"scope": "all"})
    try:
        b.dispatch("factor.evaluate", {"envId": "etf", "source": F, "stage": "test"})
        raise AssertionError("应被纪律锁拒")
    except BridgeError as e:
        assert e.code == -32003, f"应报纪律锁(-32003)而非数据缺失: {e.code}"


def test_receipt_verified_is_bool(tmp_path):
    """F-A1 修复：手构（无 receipt）diagnosis 提交 → receipt_verified 必须是 False（bool），
    不得是 None——下游 `is False` 严格检查不漏手构场景。"""
    data = tmp_path / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": _env(data)}}})
    b.dispatch("data.load", {"envId": "etf"})
    fake = {"ic_ir_train": 0.9, "ic_n_train": 100, "column_perm_train": {"z": 9.0},
            "beta_exposure": 0.0,
            # 无 _receipt 全编造；deflated_train 带充分统计量（A3 提交时刻
            # 重算要求——重算本身不依赖 receipt，verified 仍须 False）
            "deflated_train": {"p": 0.001, "n_trials": 1,
                               "sr_hat": 0.9, "skew": 0.0, "kurt": 3.0,
                               "n_obs": 100}}
    sub = b.dispatch("registry.submit", {"envId": "etf", "name": "fake",
                                         "source": F, "signal": "x", "diagnosis": fake})
    assert sub["receipt_verified"] is False, sub["receipt_verified"]


def test_submit_incomplete_stats_rejected_with_clear_error(tmp_path):
    """2026-08-21 tsi_ad 事故回归：agent 手工构造诊断只抄了 sr_hat/n_obs，
    丢了 skew/kurt → 提交时刻重算 float(None) 静默 None → passes_acceptance
    误报"缺池分布基线"（基线明明在），把 agent 引去无效的 null 重校准。

    修复：sr_hat 在场但 (skew, kurt, n_obs) 不齐 = 删改痕迹 → -32602
    明确拒绝并指出缺失字段，不得进入重算。"""
    data = tmp_path / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": _env(data)}}})
    b.dispatch("data.load", {"envId": "etf"})
    b.dispatch("factor.random_generate", {"envId": "etf", "mode": "null-calibration", "n": 5})
    # 事故现场复刻：sr_hat/n_obs 在场，skew/kurt 缺（agent 摘要式构造）
    hand_built = {"ic_ir_train": 0.406, "ic_n_train": 58,
                  "column_perm_train": {"z": 104.0, "p": 0.0001},
                  "beta_exposure": 0.05,
                  "deflated_train": {"p": 0.041, "n_trials": 89,
                                     "sr_hat": 0.406, "n_obs": 58}}
    try:
        b.dispatch("registry.submit", {"envId": "etf", "name": "tsi_ad_incident",
                                       "source": F, "signal": "x",
                                       "diagnosis": hand_built})
        raise AssertionError("缺 skew/kurt 的诊断未被拒——静默 p=None 事故复发")
    except BridgeError as e:
        assert e.code == -32602, e.code
        msg = str(e)
        # 必须点名缺失字段（可操作），且不得误报为缺基线（误导重校准）
        assert "skew" in msg and "kurt" in msg, msg
        assert "基线" not in msg, f"错误消息仍在误导（缺基线）：{msg}"
    # 完整统计量（对照）：同结构 + skew/kurt → 进入重算（p 有值或按门拒绝，不再是 -32602 缺字段）
    complete = {**hand_built,
                "deflated_train": {**hand_built["deflated_train"],
                                   "skew": 0.1, "kurt": 3.2}}
    sub = b.dispatch("registry.submit", {"envId": "etf", "name": "tsi_ad_complete",
                                         "source": F, "signal": "x",
                                         "diagnosis": complete})
    dp = (sub.get("entry") or {}).get("diagnosis", {}).get("deflated_train", {})
    assert dp.get("p") is not None, f"完整统计量仍 p=None（重算链路另有问题）：{dp}"
