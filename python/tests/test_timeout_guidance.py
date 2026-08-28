# coding=utf-8
"""worker 超时的可行动引导回归（2026-08-26 规划书 W1-W3）。

事故背景（生产实证）：agent 手写 rank autocorrelation 因子，per-ETF
Python 循环 → 单次 factor(env) > 300s → worker 被杀 → 只收到一句
「worker 超时（>300s），已终止」→ agent 误判 bridge 崩了要等恢复，
随即放弃研究方向换「更简单的方法」。正确行为：向量化实现，方向不变。

本文件锁定三块修复：
- W1 超时错误一条消息含四要素（存活/零写入/根因修法/纪律）；
- W2 evaluate 单次计时 + submit 噪声门可行性预警（blocked/warn/ok）；
- W3 -32005 台账（tool_failures.json，有界 100）→ loop.infra_failures。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""

# 慢因子：per-symbol Python 循环的替身（sleep 等比压缩了生产 >300s 的场景）
# 2026-08-28 修正：固定 25M 迭代在慢机上翻倍（标定机 0.4s → 本机 0.81s，
# 把 est 推出断言窗并翻转 warn/blocked 分支）——改按 process_time 定向
# 燃烧 0.45s，perf 断言与机器速度解耦（全部断言过需 cpu ∈ (0.35, 0.6]）
SLOW_SOURCE = """
import pandas as pd
import time as _t

def factor(env):
    _end = _t.process_time() + 0.45
    _x = 0
    while _t.process_time() < _end:
        for _ in range(1_000_000):
            _x += 1
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _make_bridge(root: Path, execution_mode: str = "in_process") -> Bridge:
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
    b = Bridge(state_root=str(root / "state"), execution_mode=execution_mode)
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


# ---- 1. W1：超时错误四要素 + W3 端到端台账 ----

def test_timeout_error_actionable(tmp_path, monkeypatch):
    """monkeypatch Popen.communicate 抛 TimeoutExpired → evaluate 收 -32005，
    消息含方法名、「立即可重试」「零写入」「向量化」「修实现」；
    随后 W3 台账落盘、status 携带 loop.infra_failures 引导。
    （P1 起 _run_worker 走 Popen+communicate——超时时刻读子进程 CPU 做
    二维归因；假进程 pid 读不到 CPU → 保守按 cpu_dense，四要素不变。）"""
    b = _make_bridge(tmp_path, execution_mode="worker")
    # 先让因果检查真实跑过并进缓存（真实时序：check_causality 先于 evaluate；
    # 缓存命中后 monkeypatch 的 Popen 只会拦到 factor.evaluate 本身）
    causal = b.dispatch("factor.check_causality",
                        {"envId": "primary", "source": BASELINE_SOURCE})
    assert causal["verdict"] == "causal", causal

    class _FakeProc:
        pid = 424242
        returncode = None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["worker"], timeout=300)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.evaluate",
                   {"envId": "primary", "source": BASELINE_SOURCE,
                    "stage": "development"})
    err = ei.value
    assert err.code == -32005, err.code
    msg = err.message
    assert "factor.evaluate" in msg, msg           # 方法名
    for kw in ("可立即重试", "零写入", "向量化", "修实现"):
        assert kw in msg, f"超时消息缺要素「{kw}」: {msg}"
    # 行为纪律：超时不是对研究方向的判定
    assert "不换假设" in msg, msg

    # W3 端到端：失败已入台账 → 下一个成功响应携带纠偏引导
    ledger = tmp_path / "state" / "tool_failures.json"
    assert ledger.exists()
    entries = json.loads(ledger.read_text(encoding="utf-8"))
    assert entries[-1]["method"] == "factor.evaluate"
    st = b.dispatch("status", {})
    assert "infra_failures" in st["loop"], st["loop"]
    inf = st["loop"]["infra_failures"]
    assert inf["last_method"] == "factor.evaluate"
    assert inf["recent_count"] >= 1
    assert "勿因超时更换研究方向" in inf["note"], inf["note"]


# ---- 2. W2：evaluate 单次计时 + submit 可行性预警 ----

def test_evaluate_perf_verdicts(tmp_path, monkeypatch):
    """慢因子源（CPU 燃烧 ~0.4s）+ monkeypatch 阈值常量 → diagnosis.perf 的
    verdict 与估算秒数；快因子默认阈值 → ok。P4 起 perf 用 CPU 秒
    （sleep 不烧 CPU，墙钟口径在并行下失真——故用计算密集源）。"""
    from dsh_factor_mining.factor import noise

    b = _make_bridge(tmp_path)  # in_process：perf 与 worker 同口径
    # 快因子 × 默认阈值（预算 240s）→ ok
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": BASELINE_SOURCE,
                       "stage": "development"})
    assert diag["perf"]["verdict"] == "ok", diag["perf"]

    # 慢因子（单次 ~0.4s）× 预算 2.0s：est = 10×0.4 ≈ 4s > 2.0 → blocked
    monkeypatch.setattr(noise, "NOISE_BUDGET_SECS", 2.0)
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": SLOW_SOURCE,
                       "stage": "development"})
    perf = diag["perf"]
    assert perf["verdict"] == "blocked", perf
    assert perf["factor_runtime_s"] >= 0.35, perf
    assert 3.5 < perf["submit_noise_gate_estimate_s"] < 8.0, perf
    assert "向量化" in perf["note"], perf["note"]
    assert "事务中止" in perf["note"], perf["note"]

    # 同一慢因子 × 预算 6.0s：est ≈ 4s ∈ (3.0, 6.0] → warn
    monkeypatch.setattr(noise, "NOISE_BUDGET_SECS", 6.0)
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": SLOW_SOURCE,
                       "stage": "development"})
    perf = diag["perf"]
    assert perf["verdict"] == "warn", perf
    assert 3.5 < perf["submit_noise_gate_estimate_s"] < 8.0, perf


# ---- 3. W3：台账 → loop.infra_failures 注入 ----

def test_loop_infra_failures_guidance(tmp_path):
    """直写 tool_failures.json → status 的 loop.infra_failures 存在且
    note 含「勿因超时更换研究方向」；30 分钟窗口外的条目不计；
    无失败文件时 loop 无该键。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    (state / "tool_failures.json").write_text(json.dumps([
        {"ts": "2020-01-01T00:00:00", "method": "factor.walk_forward",
         "code": -32005},                       # 窗口外：不计
        {"ts": now, "method": "factor.evaluate", "code": -32005},
    ], ensure_ascii=False), encoding="utf-8")
    st = b.dispatch("status", {})
    inf = st["loop"]["infra_failures"]
    assert inf is not None, st["loop"]
    assert inf["recent_count"] == 1, inf
    assert inf["last_method"] == "factor.evaluate"
    assert "勿因超时更换研究方向" in inf["note"], inf["note"]
    assert "可立即重试" in inf["note"], inf["note"]
    # 无失败文件 → loop 无该键（纯附加，不污染常规响应）
    (state / "tool_failures.json").unlink()
    st2 = b.dispatch("status", {})
    assert "infra_failures" not in st2["loop"], st2["loop"]


# ---- 4. W3：台账有界性 ----

def test_tool_failures_bounded(tmp_path):
    """>100 条直写后再记录一条 → 截断为最近 100 条，最新在尾部。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    entries = [{"ts": "2026-08-26T10:00:00", "method": f"m{i}", "code": -32005}
               for i in range(105)]
    (state / "tool_failures.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    b._record_infra_failure("factor.evaluate")
    data = json.loads((state / "tool_failures.json").read_text(encoding="utf-8"))
    assert len(data) == 100, len(data)
    assert data[-1]["method"] == "factor.evaluate"
    assert data[0]["method"] == "m6"           # 最旧的 6 条被挤出（106 → 100）
