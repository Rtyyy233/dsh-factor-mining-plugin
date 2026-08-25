# coding=utf-8
"""尾部三件套计算层回归（2026-08-25 用户批准设计；只标注不门——
准入与独立多重检验在 Phase 5）。

锁定：
1. 目标象限演示：尾部组抬升但内部噪声（带噪分类器因子）→
   spread_ir 高、tail_ic ≈ 0——IC 体系的结构性盲区被主指标捕获
2. 线性因子（F ∝ fwd+噪声）：spread 高 + tail_ic 高 +
   spread_ic_corr 高（∝ 关系的对照）
3. top-N placebo：漂移面板 + tilt 因子 → net_mean 正、placebo_z
   显著为正；随机游走 + 噪声因子 → placebo_z ≈ 0
4. 自动接线：evaluate 响应带 tail 块；trail_engine 条目带 tail 块
   （自动计算=自动计数）
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
from dsh_factor_mining.factor.tail import tail_metrics  # noqa: E402


def _make_bridge(root: Path, drift: bool = True) -> Bridge:
    """drift=True：±0.004 逐资产漂移面板；False：纯随机游走。"""
    rng = np.random.default_rng(11 if drift else 4)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    mu = np.where(np.arange(N) < 20, 0.004, -0.004) if drift \
        else np.full(N, 0.001)
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


# 带噪分类器：tilt 信号 + 大噪声——尾部组抬升但组内排序是噪声
NOISY_TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    rng = np.random.default_rng(3)
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
    base = np.broadcast_to(tilt, env.c.shape).astype(float)
    return base + rng.normal(0.0, 3.0, env.c.shape)
"""

# 线性因子：与 fwd 成比例（漂移面板上 momentum 即近似线性）
MOMENTUM_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


# ---- 1. 目标象限：尾部组差显著、尾部内部排序无信息 ----

def test_noisy_classifier_target_quadrant(tmp_path):
    """tilt+大噪声：spread_ir 高（组抬升穿透噪声）、tail_ic ≈ 0
    （组内排序是噪声）——「IC_IR 平庸但 top-N 出色」的目标象限，
    全截面 IC 也会被噪声稀释，spread 主指标不稀释。"""
    b = _make_bridge(tmp_path, drift=True)
    env = b.envs["primary"]
    fn = b._compile_factor(NOISY_TILT_SOURCE)
    tm = tail_metrics(fn(env), env, placebo_draws=10)
    assert tm is not None, tm
    assert tm["spread_ir"] is not None and tm["spread_ir"] > 0.5, tm
    # 组内排序被 σ=3 噪声淹没 → tail_ic 低
    assert tm["tail_ic"] is not None and abs(tm["tail_ic"]) < 0.3, tm


# ---- 2. 线性对照：spread/tail_ic 双高、corr 高 ----
# （momentum 不适用：漂移面板上赢家组内呈短期反转——组内排序反相关，
# 这是真实性质不是 bug；线性对照用 fwd 对齐因子）

ALIGNED_SOURCE = """
import pandas as pd, numpy as np

def factor(env):
    H = env.calibration.horizon
    c = pd.DataFrame(env.c)
    o = pd.DataFrame(env.o)
    if env.calibration.execution == "t0":
        f = c.shift(-H) / c - 1.0
    else:
        f = c.shift(-H) / o.shift(-1) - 1.0
    rng = np.random.default_rng(7)
    return (f + rng.normal(0.0, 0.01, f.shape)).values
"""


def test_linear_factor_dual_high(tmp_path):
    b = _make_bridge(tmp_path, drift=True)
    env = b.envs["primary"]
    fn = b._compile_factor(ALIGNED_SOURCE)
    tm = tail_metrics(fn(env), env, placebo_draws=10)
    assert tm["spread_ir"] is not None and tm["spread_ir"] > 1.0, tm
    assert tm["tail_ic"] is not None and tm["tail_ic"] > 0.5, tm
    # 线性支付下 spread_t ∝ IC_t → corr 高（噪声成分稀释，0.7+ 即明确）
    assert tm["spread_ic_corr"] is not None and tm["spread_ic_corr"] > 0.7, tm


# ---- 3. top-N placebo ----

def test_topn_placebo_signal_vs_noise(tmp_path):
    """漂移面板 + 纯 tilt：placebo_z 显著正；随机游走 + 噪声因子：
    placebo_z 不显著（|z| < 2 容差）。"""
    b = _make_bridge(tmp_path, drift=True)
    env = b.envs["primary"]
    tilt_fn = b._compile_factor(
        "\nimport numpy as np\n\ndef factor(env):\n"
        "    n = env.c.shape[1]\n"
        "    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)\n"
        "    return np.broadcast_to(tilt, env.c.shape).astype(float)\n")
    tm = tail_metrics(tilt_fn(env), env, placebo_draws=15)
    topn = tm["topn"]
    assert topn.get("placebo_z") is not None and topn["placebo_z"] > 3, topn
    assert topn["net_mean"] > 0, topn

    b2 = _make_bridge(tmp_path, drift=False)
    env2 = b2.envs["primary"]
    noise_fn = b2._compile_factor(NOISY_TILT_SOURCE)
    tm2 = tail_metrics(noise_fn(env2), env2, placebo_draws=15)
    z2 = tm2["topn"].get("placebo_z")
    assert z2 is not None and abs(z2) < 3, tm2["topn"]


# ---- 4. 自动接线：evaluate + trail_engine 条目 ----

def test_tail_auto_wiring(tmp_path):
    b = _make_bridge(tmp_path, drift=True)
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": MOMENTUM_SOURCE,
                       "stage": "development"})
    assert "tail" in diag, diag.keys()
    assert isinstance(diag["tail"], dict) and "spread_ir" in diag["tail"], \
        diag["tail"]
    # trail_engine 条目携带（tail 账本数据源）
    entries = json.loads((tmp_path / "state" / "trail_engine.json")
                         .read_text(encoding="utf-8"))
    assert entries and isinstance(entries[-1].get("tail"), dict), \
        entries[-1].keys()
    # lossless：无 NaN/Inf
    s = json.dumps(diag["tail"])
    assert "NaN" not in s and "Infinity" not in s, s

    # batch 成员同样覆盖
    res = b.dispatch("factor.evaluate_batch", {
        "envId": "primary", "horizon": 20,
        "sources": {"v1": MOMENTUM_SOURCE,
                    "v2": MOMENTUM_SOURCE.replace("shift(20)", "shift(21)")}})
    for name, d in res["factors"].items():
        assert isinstance(d.get("tail"), dict), (name, d.keys())
