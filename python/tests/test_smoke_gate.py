# coding=utf-8
"""烟测门（效率编译层 2026-09-01）单元+集成测试。

设计依据 tmp/numba-spike/spike-report.md（生产面板实测）：
- classify_gate 纯函数三档（pass/njit/reject）与 α 数学（线性外推放行
  T^2*N 排序怪的 spike 教训）
- 门集成：慢因子 -32003 零烧名拒绝、缓存命中、fail-open、小面板豁免、
  死循环（烟测超时）分类
- 机器速度解耦：burner 用 process_time 定向燃烧（每 40k 格 1 CPU 秒，
  t∝cells → α≈1）；预算经 monkeypatch effective_wall_timeout 恒等固定
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import bridge as bridge_mod
from dsh_factor_mining import smoke as smoke_mod
from dsh_factor_mining.bridge import BridgeError
from dsh_factor_mining.smoke import classify_gate, extrapolate, fit_alpha

# 确定性 CPU 燃烧因子：t ∝ cells（α≈1），函数内 import 使 njit 不可编译
# → 纯拒绝路径（不混入 njit 分支）
CPU_BURNER = (
    "def factor(env):\n"
    "    import time as _t\n"
    "    c = env.c\n"
    "    n = c.shape[0] * c.shape[1]\n"
    "    end = _t.process_time() + n / 40000.0\n"
    "    while _t.process_time() < end:\n"
    "        pass\n"
    "    return c * 0.0\n")

FAST_SRC = "def factor(env):\n    return env.c / (env.v + 1.0)\n"


# ---- 纯函数：α 数学与三档判定 ----

RAW = {
    "t_small_s": 0.1, "small_cells": 10240,
    "t_smoke_s": 0.4, "smoke_cells": 40960,
    "full_cells": 40960,
    "njit": {"available": False, "compiled": False},
}


def test_fit_alpha_clamps():
    assert fit_alpha(0.1, 0.4, 10240, 40960) == pytest.approx(1.0)
    assert fit_alpha(0.05, 0.8, 10240, 40960) == pytest.approx(2.0)
    assert fit_alpha(0.1, 0.01, 10240, 40960) == 0.8      # 钳下限
    assert fit_alpha(0.01, 100.0, 10240, 40960) == 2.5    # 钳上限


def test_extrapolate_scaling():
    assert extrapolate(0.4, 40960, 40960 * 10, 1.0) == pytest.approx(4.0)
    assert extrapolate(0.4, 40960, 40960 * 10, 2.0) == pytest.approx(40.0)


def test_classify_pass_then_reject():
    v = classify_gate(RAW, "factor.evaluate", {}, 2.0)      # eff=1.0
    assert v["verdict"] == "pass" and not v.get("warn")
    v = classify_gate(dict(RAW, t_smoke_s=0.6), "factor.evaluate", {}, 2.0)
    assert v["verdict"] == "pass" and v.get("warn") is True
    # 双尺寸同比放大（α=1）超预算 → 常数慢类
    v = classify_gate(dict(RAW, t_small_s=0.375, t_smoke_s=1.5),
                      "factor.evaluate", {}, 2.0)
    assert v["verdict"] == "reject" and v["reason"] == "const_slow"


def test_classify_causality_multiplier():
    v = classify_gate(dict(RAW, t_small_s=0.075, t_smoke_s=0.3),
                      "factor.check_causality", {}, 2.0)   # est 0.3×8=2.4>2.0
    assert v["verdict"] == "reject" and v["mult"] == 8.0


def test_classify_njit_saves():
    raw = dict(RAW, t_smoke_s=1.5, njit={
        "available": True, "compiled": True, "equiv": True,
        "t_small_s": 0.002, "t_smoke_s": 0.008})
    v = classify_gate(raw, "factor.evaluate", {}, 2.0)
    assert v["verdict"] == "njit" and v["est_full_njit_s"] < 0.5


def test_classify_algo_slow_alpha():
    raw = dict(RAW, t_small_s=0.02, t_smoke_s=0.32, full_cells=40960 * 8)
    v = classify_gate(raw, "factor.evaluate", {}, 2.0)      # α=2 → est 20.5
    assert v["verdict"] == "reject" and v["reason"] == "algo_slow"


# ---- 集成：门挂进 dispatch ----

@pytest.fixture
def gated_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_FACTOR_REWRITE", "0")   # 门测试绝不真调 LLM
    monkeypatch.setattr(bridge_mod, "effective_wall_timeout",
                        lambda base_s: float(base_s))
    from test_bridge_smoke import _make_bridge
    b, _ = _make_bridge(tmp_path)
    b.dispatch("data.load", {"envId": "primary"})
    assert b.execution_mode == "worker"
    b.worker_timeout_ms = 2000                        # 预算 2s（恒等 patch 后）
    b._smoke_min_cells = 0                            # 小面板强制过门
    return b


def test_gate_rejects_slow_zero_trial(gated_bridge):
    b = gated_bridge
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": CPU_BURNER})
    assert ei.value.code == -32003
    msg = str(ei.value)
    assert "烟测门" in msg and "零 trial" in msg
    assert "缩放指数" in msg
    # 零烧名副作用检查：没有 check_causality 的 run 目录残留（烟测临时目录
    # 即用即删）
    runs = Path(b.state_root) / "worker_runs"
    leftovers = []
    for jp in runs.glob("*/job.json"):
        j = json.loads(jp.read_text(encoding="utf-8"))
        if j.get("method") == "factor.check_causality":
            leftovers.append(j)
    assert not leftovers


def test_gate_pass_fast_factor(gated_bridge):
    out = gated_bridge.dispatch("factor.check_causality",
                                {"envId": "primary", "source": FAST_SRC})
    assert isinstance(out, dict) and out.get("verdict") is not None


def test_gate_verdict_cached(gated_bridge, monkeypatch):
    b = gated_bridge
    with pytest.raises(BridgeError):
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": CPU_BURNER})
    assert b._smoke_cache, "烟测结果应入缓存"
    # 缓存命中：第二次拒绝不再触发烟测（_smoke_env_npz 被禁用也不受影响）
    def _boom(env, params):
        raise AssertionError("缓存命中时不应再烟测")
    monkeypatch.setattr(b, "_smoke_env_npz", _boom)
    with pytest.raises(BridgeError):
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": CPU_BURNER})


def test_gate_fail_open(gated_bridge, monkeypatch):
    """门自身 bug 不阻断评估（fail-open 铁律）。"""
    def _broken(*a, **k):
        raise RuntimeError("gate internal bug")
    monkeypatch.setattr(smoke_mod, "classify_gate", _broken)
    out = gated_bridge.dispatch("factor.check_causality",
                                {"envId": "primary", "source": FAST_SRC})
    assert isinstance(out, dict)


def test_gate_min_cells_exemption(gated_bridge, monkeypatch):
    b = gated_bridge
    b._smoke_min_cells = 10 ** 9
    monkeypatch.setattr(b, "_smoke_env_npz",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("豁免面板不应触发烟测")))
    out = b.dispatch("factor.check_causality",
                     {"envId": "primary", "source": FAST_SRC})
    assert isinstance(out, dict)


@pytest.mark.slow
def test_gate_hang_detected(gated_bridge, monkeypatch):
    """死循环源：烟测 worker 自身超时 → hang 类拒绝（60s 门在此调小）。"""
    monkeypatch.setattr(smoke_mod, "SMOKE_TIMEOUT_S", 5.0)
    hang = ("def factor(env):\n"
            "    c = env.c\n"
            "    while True:\n"
            "        pass\n"
            "    return c\n")
    t0 = time.time()
    with pytest.raises(BridgeError) as ei:
        gated_bridge.dispatch("factor.check_causality",
                              {"envId": "primary", "source": hang})
    assert ei.value.code == -32003
    assert "死循环" in str(ei.value)
    assert time.time() - t0 < 30


def test_gate_concurrent_single_smoke(gated_bridge, monkeypatch):
    """并发复审 F1：同源码两线程同时过门——只 spawn 一次烟测，后到者
    等待先来者结果（Event）后直接复用。"""
    import threading
    b = gated_bridge
    calls = []
    release = threading.Event()

    def _slow_smoke(source, sp, env, timeout_s):
        calls.append(source)
        release.wait(timeout=10.0)
        return {"t_small_s": 0.01, "small_cells": 10240,
                "t_smoke_s": 0.02, "smoke_cells": 20480,
                "full_cells": 48000,
                "njit": {"available": False, "compiled": False}}

    monkeypatch.setattr(b, "_run_smoke_worker", _slow_smoke)
    env = next(iter(b.envs.values()))
    outcomes = []

    def _run():
        try:
            b._smoke_gate("factor.evaluate", CPU_BURNER,
                          {"envId": "primary"}, env)
            outcomes.append("pass")
        except BridgeError:
            outcomes.append("reject")

    t1 = __import__("threading").Thread(target=_run)
    t1.start()
    time.sleep(0.4)                 # 让先来者卡在烟测里
    t2 = __import__("threading").Thread(target=_run)
    t2.start()
    time.sleep(0.4)
    assert len(calls) == 1, "先来者未完成时后到者不应再 spawn 烟测"
    release.set()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)
    assert outcomes == ["pass", "pass"]
    assert len(calls) == 1


def test_batch_member_named_in_reject(gated_bridge):
    """并发复审 F5：批量里慢成员被烟测门拒绝时点名成员。"""
    b = gated_bridge
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.evaluate_batch",
                   {"envId": "primary",
                    "sources": {"slow_one": CPU_BURNER, "fast_one": FAST_SRC},
                    "async": False})
    msg = str(ei.value)
    assert ei.value.code == -32003
    assert "批成员「slow_one」" in msg
    assert "烟测门" in msg
