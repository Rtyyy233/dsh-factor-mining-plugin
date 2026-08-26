# coding=utf-8
"""WS2 G1 placebo 门判定移 submit + 样本加厚回归（2026-08-25 任务书）。

锁定：
1. topn_placebo 公共函数：截断路径如实报 draws（budget 命中 → ≥60 +
   truncated 标注）；evaluate 轻量语义不变（tail 块 placebo_z 照写）
2. submit 后 diag 带 placebo_m ≥ 60、placebo_degraded=False
3. 缓存命中第二次不重算（计数 monkeypatch）
4. infra 失败：tail 轨事务中止且 registry 无条目；ic 轨降级轻量值
5. 逃生门：mining_state.tail_placebo_m=0 → 不重算、degraded=True
6. ledger 取证：tail_placebo_m / tail_placebo_z
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402
from dsh_factor_mining.factor.tail import topn_placebo  # noqa: E402

TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
    return np.broadcast_to(tilt, env.c.shape).astype(float)
"""


def _make_drift_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(11)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    mu = np.where(np.arange(N) < 20, 0.004, -0.004)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(mu[i], 0.01, T))
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
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high",
                                "low": "low", "close": "close",
                                "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


def _submit_tail(b: Bridge, name: str, diag=None, source=TILT_SOURCE):
    if diag is None:
        diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                              "source": source,
                                              "stage": "development"})
    return b.dispatch("registry.submit", {
        "name": name, "signal": "x", "source": source,
        "diagnosis": diag, "admit_basis": "tail"})


# ---- 1. topn_placebo 截断路径 ----

def test_topn_placebo_budget_truncation(tmp_path):
    b = _make_drift_bridge(tmp_path)
    env = b.envs["primary"]
    fn = b._compile_factor(TILT_SOURCE)
    F = fn(env)
    from dsh_factor_mining.factor.evaluate import _forward_returns, _pit_mask
    fwd, pit = _forward_returns(env), _pit_mask(env)
    t_end = int(np.searchsorted(
        env.dates, pd.Timestamp(env.calibration.dev_end)))
    full = topn_placebo(F, fwd, pit, env, t_end, 70, 20260825)
    assert full["draws"] == 70 and "truncated" not in full, full
    # 极小预算：≥60 已跑才截断 → draws=60 + truncated 标注
    trunc = topn_placebo(F, fwd, pit, env, t_end, 70, 20260825,
                         budget_secs=1e-9)
    assert trunc["truncated"] is True, trunc
    assert trunc["draws"] == 60, trunc
    assert "截断" in trunc["note"]
    # 同 seed 确定性：前缀一致（60 draws 的 z 与 70 draws 版本同号）
    assert trunc["z"] is not None and trunc["z"] > 3
    # draws 不足 5 → 不给 z
    tiny = topn_placebo(F, fwd, pit, env, t_end, 3, 1)
    assert tiny["draws"] == 0 and "z" not in tiny


# ---- 2. submit 后 diag 带 placebo_m ≥ 60 ----

def test_submit_placebo_m_thick(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    sub = _submit_tail(b, "tilt_m")
    assert sub["accepted"] is True, sub.get("reason")
    diag = sub["tail_track"]["diag"]
    assert diag["placebo_m"] is not None and diag["placebo_m"] >= 60, diag
    assert diag["placebo_degraded"] is False, diag
    assert diag["placebo_z"] == diag.get("placebo_z")  # gate 值在场
    # evaluate 轻量语义不变：tail 块 placebo_z 照写（自动计数）
    d = b.dispatch("factor.evaluate", {"envId": "primary",
                                       "source": TILT_SOURCE,
                                       "stage": "development"})
    assert isinstance(d["tail"]["topn"].get("placebo_z"), float)


# ---- 3. 缓存命中第二次不重算 ----

def _reset_registry(b: Bridge):
    """清空 registry（铁律按名/按 hash 查重——缓存测试需要同 source
    二次 submit，只能直接改写 fixture 状态绕开查重）。"""
    (Path(b.state_root) / "registry.json").write_text("[]", encoding="utf-8")


def test_placebo_cache_hit(tmp_path, monkeypatch):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    from dsh_factor_mining.factor import tail as tail_mod
    calls = []          # 只数 submit 侧重算（带 budget_secs 的调用）
    real = tail_mod.topn_placebo

    def counting(*a, **kw):
        if kw.get("budget_secs") is not None:
            calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(tail_mod, "topn_placebo", counting)
    _submit_tail(b, "tilt_c1")
    assert len(calls) == 1, "第一次 submit 应真算一次（重 placebo）"
    _reset_registry(b)
    _submit_tail(b, "tilt_c2")      # 同 source 第二次 submit：缓存命中
    assert len(calls) == 1, "缓存命中后 submit 不应重算 placebo"
    # 缓存文件在场且值带指纹
    cache = json.loads((tmp_path / "state" / "placebo_cache.json")
                       .read_text(encoding="utf-8"))
    assert cache["entries"], cache
    e = next(iter(cache["entries"].values()))
    assert e["env_fingerprint"] == b._env_full_fingerprint("primary")
    assert e["value"]["draws"] >= 60
    # 指纹不匹配 = 不命中（跨环境不得误命中）
    e["env_fingerprint"] = "other-env"
    (tmp_path / "state" / "placebo_cache.json").write_text(
        json.dumps(cache), encoding="utf-8")
    calls.clear()
    _reset_registry(b)
    _submit_tail(b, "tilt_c3")
    assert len(calls) >= 1, "指纹不匹配的缓存必须 miss"


# ---- 4. infra 失败：tail 轨中止 / ic 轨降级 ----

def test_placebo_infra_failure_transaction(tmp_path, monkeypatch):
    from dsh_factor_mining.factor import tail as tail_mod

    def boom(*a, **kw):
        raise RuntimeError("worker exploded")

    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    monkeypatch.setattr(tail_mod, "topn_placebo", boom)
    # tail 轨 → 事务中止，registry 不落盘
    try:
        _submit_tail(b, "boom_tail", diag=diag)
        raised = False
    except BridgeError as e:
        raised = True
        assert "placebo 执行失败" in str(e), str(e)
    assert raised
    assert not (tmp_path / "state" / "registry.json").exists()
    # ic 轨 → 主判定不受影响，尾轨标注降级（degraded 轻量值兜底）
    monkeypatch.undo()
    diag2 = b.dispatch("factor.evaluate", {"envId": "primary",
                                           "source": TILT_SOURCE,
                                           "stage": "development"})
    monkeypatch.setattr(tail_mod, "topn_placebo", boom)
    sub = b.dispatch("registry.submit", {
        "name": "boom_ic", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag2})
    tt = sub["tracks"]["tail"]
    assert tt["diag"]["placebo_degraded"] is True, tt
    assert "degraded" in tt["reason"], tt["reason"]
    # registry 条目在场（ic 轨不因 placebo 降级而中止）
    reg = json.loads((tmp_path / "state" / "registry.json")
                     .read_text(encoding="utf-8"))
    assert reg[-1]["name"] == "boom_ic"


# ---- 5. 逃生门：tail_placebo_m=0 ----

def test_placebo_escape_hatch(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    ms = tmp_path / "state" / "mining_state.json"
    ms.write_text(json.dumps({"tail_placebo_m": 0}), encoding="utf-8")
    sub = _submit_tail(b, "tilt_escape")
    diag = sub["tail_track"]["diag"]
    assert diag["placebo_degraded"] is True, diag
    assert diag["placebo_m"] is None, diag
    # 轻量值兜底（evaluate 已算），判定照常可过
    assert sub["accepted"] is True, sub.get("reason")
    assert not (tmp_path / "state" / "placebo_cache.json").exists()


# ---- 6. ledger 取证 ----

def test_ledger_placebo_evidence(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    _submit_tail(b, "tilt_ledger")
    led = json.loads((tmp_path / "state" / "ledger.json")
                     .read_text(encoding="utf-8"))
    assert led, "submit 应写 ledger"
    last = led[-1]
    assert last["tail_placebo_m"] >= 60, last
    assert isinstance(last["tail_placebo_z"], (int, float)), last
