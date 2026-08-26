# coding=utf-8
"""WS4 spread 矩入账本 + G3 高阶矩修正回归（2026-08-25 任务书）。

锁定：
1. tail_metrics 产出 spread_moments（g3/g4/n，与 _dsr_p_from_stats 同
   口径：总体矩 / 样本 std(ddof=1) 的幂；g4 ≥ 1 数学下界）
2. 手算验证：给定 (spread_ir, g3, g4, n) → t_adj/denom 与公式逐位一致
3. 无矩回退：legacy 模式 denom=1，判定 = 现行公式（回归保护）
4. 修正前后翻转各验一例：正偏（denom 缩 → 更易过）/厚尾（denom 胀 → 更难）
5. submit 全链：新尾块带矩 → corrected 模式标注 + reason 附修正数字
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.tailgate import (  # noqa: E402
    tail_admission, tail_deflation_bar)

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


# ---- 1. tail_metrics 产出矩 ----

def test_tail_metrics_moments(tmp_path):
    b = _make_drift_bridge(tmp_path)
    d = b.dispatch("factor.evaluate", {"envId": "primary",
                                       "source": TILT_SOURCE,
                                       "stage": "development"})
    mom = d["tail"].get("spread_moments")
    assert isinstance(mom, dict), d["tail"].keys()
    assert mom["n"] >= 10, mom
    assert mom["g4"] >= 1.0, mom               # 总体峰度数学下界
    assert -5.0 <= mom["g3"] <= 5.0, mom
    # trail 条目自然携带（tail 块整体落盘）
    trail = json.loads((tmp_path / "state" / "trail_engine.json")
                       .read_text(encoding="utf-8"))
    assert isinstance(trail[-1]["tail"].get("spread_moments"), dict)
    # lossless：无 NaN/Inf/-0.0
    s = json.dumps(mom)
    assert "NaN" not in s and "Infinity" not in s and "-0.0" not in s


# ---- 2. 手算验证 t_adj/denom ----

def test_t_adj_formula_exact():
    sr, g3, g4, n = 0.42, 0.8, 4.2, 60
    bar = tail_deflation_bar(10, 100, spread_ir=sr,
                             moments={"g3": g3, "g4": g4, "n": n})
    emax = bar["emax_sigma"]
    s0 = 1.0 / math.sqrt(100)
    denom = max(1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr, 1e-8)
    t_manual = (sr - emax * s0) * math.sqrt(n - 1) / math.sqrt(denom)
    assert bar["gate_kind"] == "corrected"
    assert abs(bar["denom"] - denom) < 1e-6
    assert abs(bar["t_adj"] - t_manual) < 1e-3, (bar["t_adj"], t_manual)
    assert bar["pass_g3"] == (t_manual >= bar["z_req"])
    # required 反解与 t_adj 一致（代入 required 恰过门）
    req = bar["required_spread_ir"]
    if req is not None and req > 0:
        check = tail_deflation_bar(10, 100, spread_ir=req,
                                   moments={"g3": g3, "g4": g4, "n": n})
        assert abs(check["t_adj"] - bar["z_req"]) < 0.01


# ---- 3. 无矩回退 = 现行公式（回归保护） ----

def test_legacy_no_moments_denom_one():
    sr, n_days, n_eff = 0.35, 100, 10
    leg = tail_deflation_bar(n_eff, n_days, spread_ir=sr, moments=None)
    assert leg["gate_kind"] == "legacy" and leg["denom"] == 1.0
    assert leg["moments"] == "legacy"
    s0 = 1.0 / math.sqrt(n_days)
    req = leg["required_spread_ir"]
    assert abs(req - (leg["emax_sigma"] * s0 + leg["z_req"] * s0)) < 1e-3
    assert leg["pass_g3"] == (sr >= req)
    # 畸形矩（缺字段/n<5/NaN）同 legacy
    for bad in ({"g3": 0.1}, {"g3": 0.1, "g4": 3.0, "n": 3},
                {"g3": float("nan"), "g4": 3.0, "n": 60}):
        bb = tail_deflation_bar(n_eff, n_days, spread_ir=sr, moments=bad)
        assert bb["gate_kind"] == "legacy", bad


# ---- 4. 修正前后翻转对照 ----

def test_correction_flips_both_ways():
    """同 spread_ir=0.40，n=60，N_eff=10（n_days=60 → s0≈0.129）：
    legacy 门 ≈ emax×0.129 + 1.955×0.129 ≈ 0.456 > 0.40 → 拒；
    正偏 g3=1.5 缩 denom（0.48<1）→ t_adj≈2.18 > 1.955 → 翻转为过；
    厚尾 g4=9 胀 denom（1.32>1）→ t_adj≈1.31 → 维持拒。"""
    n_days, sr = 60, 0.40
    base = tail_deflation_bar(10, n_days, spread_ir=sr, moments=None)
    pos_skew = tail_deflation_bar(10, n_days, spread_ir=sr,
                                  moments={"g3": 1.5, "g4": 3.0, "n": 60})
    fat_tail = tail_deflation_bar(10, n_days, spread_ir=sr,
                                  moments={"g3": 0.0, "g4": 9.0, "n": 60})
    assert base["pass_g3"] is False, base
    assert pos_skew["denom"] < 1.0 < fat_tail["denom"]
    assert pos_skew["t_adj"] > base["t_adj"] > fat_tail["t_adj"]
    assert pos_skew["pass_g3"] is True and fat_tail["pass_g3"] is False
    # tail_admission 全链：G1/G2 通过态下 G3 决定，翻转如实反映
    # （ledger 用 10 个互不重叠名单 → N_eff=10，与 bar 级对照同门）
    from dsh_factor_mining.factor.tailgate import selection_minhash
    ledger = [{"spread_ir": 1.0, "selection_mh": selection_minhash(
        [(d, j + i * 1000) for d in range(50) for j in range(20)])}
        for i in range(10)]
    def _block(mom):
        return {"spread_ir": sr, "topn": {"placebo_z": 10.0,
                                          "periods": n_days},
                **({"spread_moments": mom} if mom else {})}
    a_leg = tail_admission(_block(None), ledger, 0.5)
    a_pos = tail_admission(_block({"g3": 1.5, "g4": 3.0, "n": 60}),
                           ledger, 0.5)
    a_fat = tail_admission(_block({"g3": 0.0, "g4": 9.0, "n": 60}),
                           ledger, 0.5)
    assert a_leg["accepted"] is False and "G3" in a_leg["reason"]
    assert a_pos["accepted"] is True
    assert a_fat["accepted"] is False
    # 拒绝 reason 附修正后数字（PASS-FAIL 教训：多维报告）
    assert "t_adj" in a_fat["reason"] and "denom" in a_fat["reason"], \
        a_fat["reason"]


# ---- 5. submit 全链 corrected 模式 ----

def test_submit_corrected_mode(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "tilt_mom", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": b.dispatch(
            "factor.evaluate", {"envId": "primary", "source": TILT_SOURCE,
                                "stage": "development"}),
        "admit_basis": "tail"})
    bar = sub["tail_track"]["diag"]["bar"]
    assert bar["gate_kind"] == "corrected", bar
    assert bar["g3"] is not None and bar["g4"] is not None
    assert "t_adj" in bar and "denom" in bar
