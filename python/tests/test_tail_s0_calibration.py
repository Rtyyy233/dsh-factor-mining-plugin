# coding=utf-8
"""WS1 尾部经验 null 校准回归（2026-08-25 任务书）——G3 的 s0 从解析式
1/√n_days 升级为 null 地形 spread 段的经验分布 std。

锁定：
1. spread_ir_statistic 的 t_end 参数化：train 行集口径（前段结构不入
   全区间统计）
2. 校准后 landscape 含 spread 段（per-horizon，同构 ic_ir）且指纹绑定
3. _landscape_tail_s0 指纹门：match → std；mismatch / legacy 无段 → None
4. tail_deflation_bar：s0_emp 生效 + s0_source 标注；无效值降级解析式
5. submit 全链：校准后 tracks.tail.diag.bar 带 s0_source="empirical"；
   无地形 → "analytic"（降级不拒绝给门）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.tail import spread_ir_statistic  # noqa: E402
from dsh_factor_mining.factor.tailgate import tail_deflation_bar  # noqa: E402

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


# ---- 1. spread_ir_statistic 的 t_end 参数化 ----

def test_spread_statistic_t_end_cuts():
    """spread 结构只在后半段：t_end 切到中点 → 统计量显著小于全区间。"""
    rng = np.random.default_rng(5)
    T, N = 400, 60
    fwd = rng.normal(0, 0.01, (T, N))
    F = rng.normal(0, 1, (T, N))
    # 后半段：前 12 资产 fwd 系统性 +0.01（尾部组差）
    fwd[T // 2:, :12] += 0.01
    F[T // 2:, :12] += 2.0
    pit = np.ones((T, N), dtype=bool)
    full = spread_ir_statistic(F, fwd, pit, 5)
    half = spread_ir_statistic(F, fwd, pit, 5, t_end=T // 2)
    assert full is not None and full > 0.5, (full, half)
    # 前半段无结构 → t_end 版接近 0（纯噪声 IR）
    assert abs(half) < 0.5, (full, half)
    # t_end=None 与旧签名行为一致（noise/day-perm 调用方零感知）
    fwd2 = F * 0.0
    assert spread_ir_statistic(F, fwd2, pit, 5) == \
        spread_ir_statistic(F, fwd2, pit, 5, t_end=None)


# ---- 2. 校准产出 spread 段 + 指纹绑定 ----

def test_calibration_spread_section(tmp_path):
    b = _make_drift_bridge(tmp_path)
    res = b.dispatch("factor.random_generate",
                     {"envId": "primary", "mode": "null-calibration", "n": 6})
    land = json.loads((tmp_path / "state" / "null_landscape.json")
                      .read_text(encoding="utf-8"))
    assert "spread" in land, land.keys()
    env = b.envs["primary"]
    h = env.calibration.horizon
    sub = land["spread"].get(str(h))
    assert isinstance(sub, dict), land["spread"].keys()
    for k in ("p10", "p50", "p90", "p95", "std", "mean_abs", "n"):
        assert k in sub, sub.keys()
    assert sub["n"] >= 2 and sub["std"] > 0, sub
    # 同一文件 → 指纹绑定自动生效
    assert land.get("env_fingerprint") is not None
    assert b._landscape_fingerprint_status(land, "primary") == "match"
    # factor.null_landscape 查询响应自然带出
    q = b.dispatch("factor.null_landscape", {"envId": "primary"})
    assert "spread" in q and q["spread"][str(h)]["std"] == sub["std"]
    # IC 段不受影响（同批树同条件）
    assert "ic_ir" in land and str(h) in land["ic_ir"]


# ---- 3. _landscape_tail_s0 指纹门 ----

def test_landscape_tail_s0_gates(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    env = b.envs["primary"]
    h = env.calibration.horizon
    std = b._landscape_tail_s0("primary", h)
    land = json.loads((tmp_path / "state" / "null_landscape.json")
                      .read_text(encoding="utf-8"))
    assert std == land["spread"][str(h)]["std"]
    # horizon=None → 主 horizon
    assert b._landscape_tail_s0("primary", None) == std
    # mismatch：改指纹 → None
    land["env_fingerprint"] = "stale"
    (tmp_path / "state" / "null_landscape.json").write_text(
        json.dumps(land), encoding="utf-8")
    assert b._landscape_tail_s0("primary", h) is None
    # legacy：无 spread 段 → None
    land.pop("spread")
    land["env_fingerprint"] = b._env_full_fingerprint("primary")
    (tmp_path / "state" / "null_landscape.json").write_text(
        json.dumps(land), encoding="utf-8")
    assert b._landscape_tail_s0("primary", h) is None
    # 菜单外 horizon → None
    assert b._landscape_tail_s0("primary", h + 999) is None


# ---- 4. tail_deflation_bar 的 s0 来源 ----

def test_deflation_bar_s0_source():
    b_analytic = tail_deflation_bar(10, 100)
    assert b_analytic["ok"] and b_analytic["s0_source"] == "analytic"
    assert abs(b_analytic["s0"] - 1.0 / np.sqrt(100)) < 1e-9
    b_emp = tail_deflation_bar(10, 100, s0_emp=0.5)
    assert b_emp["s0_source"] == "empirical" and b_emp["s0"] == 0.5
    # 门随 s0 缩放（同 N_eff/α）
    assert abs(b_emp["required_spread_ir"]
               - b_analytic["required_spread_ir"]
               * (0.5 / (1.0 / np.sqrt(100)))) < 1e-3
    # 无效经验值（0/负/NaN/None）→ 降级解析式，不拒绝给门
    for bad in (0.0, -0.1, float("nan"), None):
        bb = tail_deflation_bar(10, 100, s0_emp=bad)
        assert bb["ok"] and bb["s0_source"] == "analytic", (bad, bb)
    # 样本不足检查不受 s0_emp 豁免（n_days 是因子自身采样日数）
    assert tail_deflation_bar(10, 5, s0_emp=0.5)["ok"] is False


# ---- 5. submit 全链 s0_source 标注 ----

def test_submit_chain_s0_source(tmp_path):
    b = _make_drift_bridge(tmp_path)
    # 无地形 → analytic 降级
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    sub0 = b.dispatch("registry.submit", {
        "name": "tilt_analytic", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag, "admit_basis": "tail"})
    assert sub0["accepted"] is True, sub0.get("reason")
    assert sub0["tail_track"]["diag"]["bar"]["s0_source"] == "analytic"

    (tmp_path / "b2").mkdir()
    b2 = _make_drift_bridge(tmp_path / "b2")
    b2.dispatch("factor.random_generate",
                {"envId": "primary", "mode": "null-calibration", "n": 5})
    diag2 = b2.dispatch("factor.evaluate", {"envId": "primary",
                                            "source": TILT_SOURCE,
                                            "stage": "development"})
    sub1 = b2.dispatch("registry.submit", {
        "name": "tilt_empirical", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag2, "admit_basis": "tail"})
    tt = sub1["tail_track"]
    assert tt["diag"]["bar"]["s0_source"] == "empirical", tt["diag"]
    # 经验 s0 与解析式同量级（1200 日/步 20 → ~60 采样日 vs 随机树 spread
    # std ~1/√60）：门仍可过（tilt 信号强）
    assert sub1["accepted"] is True, sub1.get("reason")
