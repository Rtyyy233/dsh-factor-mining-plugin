# coding=utf-8
"""day-perm 精确置换 null 回归（2026-08-25 用户决策：真实结构上的时序
置换检验——「对真实市场结构过拟合」层此前没有任何蒙特卡洛 null）。

IC 总量 = 时序对齐分量 + 持久截面结构分量；day-perm 与 column-perm
各证其一。本文件锁定三件事：

1. 分解演示：对齐因子（F = fwd + 噪声）observed 远超随机配对 null →
   p_two 小、alignment_dependent=True；持久倾斜因子（常数 tilt ×
   逐资产漂移面板）observed ≈ null（任意配对都复现 IC）→ p_two 大——
   **这是合法截面 alpha 形态，day-perm 不否决**（report-only 的原因）
2. 慢因子不误杀：mom250 型慢因子随机配对下无退化（全配对而非小 k
   循环移位——期望重叠 → 1/T）
3. submit 接线：registry 条目带 day_perm 块；基础设施失败 = 事务中止
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
from dsh_factor_mining.factor.permute import (  # noqa: E402
    day_permutation_test, ic_ir_statistic)

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""

# 对齐因子：F = 引擎同口径 fwd + 固定种子噪声（rank IC ~0.9/日）
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
    noise = rng.normal(0.0, 0.02, f.shape)
    return (f + noise).values
"""

# 慢因子：250 日动量（循环移位 k=5 时 ≈ 自身的退化场景）
SLOW_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(250) - 1.0).values
"""


def _make_drift_bridge(root: Path) -> Bridge:
    """逐资产漂移面板：前 20 资产 μ=+0.004、后 20 资产 μ=-0.004，
    vol 0.01——截面漂移结构在任意一天的横截面上都复现（持久结构）。"""
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
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
    return np.broadcast_to(tilt, env.c.shape).astype(float)
"""


def _make_plain_bridge(root: Path) -> Bridge:
    """普通随机游走面板（对齐因子演示用，与 loop 测试同构）。"""
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


# ---- 1. 分解演示：对齐分量 vs 持久结构分量 ----

def test_aligned_factor_is_alignment_dependent(tmp_path):
    """F = fwd + 噪声（构造性对齐）→ observed 远超随机配对 null →
    p_two 小、alignment_dependent=True。"""
    b = _make_plain_bridge(tmp_path)
    r = b.dispatch("factor.day_perm_test",
                   {"envId": "primary", "source": ALIGNED_SOURCE, "m": 100})
    assert r["n_valid"] >= 50, r
    assert r["observed"] is not None and r["observed"] > 1.0, r
    # 方向性 p 判定（p_two 有 2/(m+1) 粒度下限，m=100 时达不到 0.01）
    assert r["p_align"] < 0.01, r
    assert r["alignment_dependent"] is True, r
    assert r["obs_percentile"] > 99.0, r
    assert "report-only" in r["gate"], r["gate"]


def test_persistent_tilt_not_alignment_dependent(tmp_path):
    """逐资产漂移面板 + 常数 tilt 因子：任意配对都复现 IC →
    observed ≈ null → p_two 不小 → alignment_dependent=False。

    这是 day-perm 不做无条件硬门的实证原因：持久溢价类因子
    （如非流动性）的 IC 由随机配对完全解释——合法截面 alpha 形态，
    由 column-perm 认证。"""
    b = _make_drift_bridge(tmp_path)
    r = b.dispatch("factor.day_perm_test",
                   {"envId": "primary", "source": TILT_SOURCE, "m": 100})
    assert r["n_valid"] >= 50, r
    # 观测与 null 均显著为正（漂移结构在两种配对下都在）
    assert r["observed"] > 0.3, r
    assert r["null_mean"] > 0.3, r
    # observed 不显著超出 null → 不是对齐依赖
    assert not r["alignment_dependent"], r
    assert 5.0 < r["obs_percentile"] < 95.0, r


# ---- 2. 慢因子不误杀（循环移位退化场景的对照） ----

def test_slow_factor_no_degenerate_rejection(tmp_path):
    """mom250 型慢因子在全随机配对下正常计算（无小 k 循环移位的
    「移位后 ≈ 自身」退化）；普通随机游走面板上它没有对齐信息，
    observed 落在 null 内 → 不是对齐依赖（正确的阴性）。"""
    b = _make_plain_bridge(tmp_path)
    r = b.dispatch("factor.day_perm_test",
                   {"envId": "primary", "source": SLOW_SOURCE, "m": 60})
    assert r["n_valid"] >= 30, r
    assert r["observed"] is not None and np.isfinite(r["observed"]), r
    # 随机游走无可预测结构：mom250 无信息，observed 在 null 分布内
    assert not r["alignment_dependent"], r


# ---- 3. 统计量参数化（tail 线复用同一台机器） ----

def test_custom_statistic_pluggable(tmp_path):
    """statistic 可注入：返回值取决于配对首行 → observed 与 null
    由注入统计量计算（尾部组差 spread 版 Phase 4 复用此通道）。"""
    b = _make_plain_bridge(tmp_path)
    env = b.envs["primary"]

    def spread_stat(F, fwd, pit, step):
        vals = []
        for t in range(0, F.shape[0], max(step, 1)):
            m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
            n = int(m.sum())
            if n < 5:
                continue
            k = max(1, n // 5)
            top = np.partition(fwd[t][m], -k)[-k:]
            vals.append(float(top.mean() - fwd[t][m].mean()))
        if len(vals) < 2:
            return None
        s = np.std(vals, ddof=1)
        return float(np.mean(vals) / s) if s > 0 else None

    from dsh_factor_mining.bridge import source_fingerprint
    fn = b._compile_factor(BASELINE_SOURCE)
    r = day_permutation_test(fn, env, m=30, base_seed=5,
                             statistic=spread_stat)
    assert r["n_valid"] >= 20, r
    assert "observed" in r and "p_two" in r, r
    assert source_fingerprint(BASELINE_SOURCE)  # import 冒烟


# ---- 4. submit 接线：registry 条目带 day_perm 块 ----

def test_submit_carries_day_perm(tmp_path):
    """submit 完成 day-perm 计算并写入条目（report-only：拒绝路径
    不因 day_perm 触发，但 infra 失败 = 事务中止）。"""
    b = _make_plain_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "weak_dp", "signal": "weak",
        "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    # 弱因子被 deflation 拒（与 day-perm 无关——report-only）
    assert sub["accepted"] is False
    dp = sub.get("day_perm")
    assert isinstance(dp, dict) and dp.get("n_valid", 0) >= 20, dp
    # 落盘的 registry 条目同样携带
    reg = json.loads((tmp_path / "state" / "registry.json")
                     .read_text(encoding="utf-8"))
    assert reg and "day_perm" in reg[-1], reg[-1].keys()


def test_ic_ir_statistic_matches_noise_semantics(tmp_path):
    """默认统计量与噪声门 IC_IR 同语义（同 fast_rank_ic + mean/std）。"""
    b = _make_plain_bridge(tmp_path)
    env = b.envs["primary"]
    from dsh_factor_mining.factor.evaluate import _forward_returns, _pit_mask
    from dsh_factor_mining.factor.noise import fast_rank_ic
    fn = b._compile_factor(BASELINE_SOURCE)
    F = fn(env)
    ic = fast_rank_ic(F, _forward_returns(env), _pit_mask(env),
                      env.calibration.sample_step)
    want = float(ic.mean() / ic.std(ddof=1)) if ic.std(ddof=1) > 0 else None
    got = ic_ir_statistic(F, _forward_returns(env), _pit_mask(env),
                          env.calibration.sample_step)
    assert want is not None and got is not None
    assert abs(want - got) < 1e-12
