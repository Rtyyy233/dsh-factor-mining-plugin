# -*- coding: utf-8 -*-
"""regime 分解 + 分裂复验门（调度器票 2b，F5 诚实性控制）。"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.factor.env import env_from_arrays
from dsh_factor_mining.factor.regime import (
    forward_returns, realized_vol_regimes, regime_rank_ic,
    split_replication_gate, u_regime, u_regime_gated)

T, N = 900, 60


def _env(seed=7, horizon=10):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.01, (T, N))
    # 三段波动 regime：中段波动放大 3 倍
    vol = np.ones(T)
    vol[T // 3: 2 * T // 3] = 3.0
    ret *= vol[:, None]
    c = 100 * np.cumprod(1 + ret, axis=0)
    o = c * (1 + rng.normal(0, 0.002, (T, N)))
    h = np.maximum(o, c) * 1.01
    l = np.minimum(o, c) * 0.99
    v = np.abs(rng.normal(1e5, 1e4, (T, N)))
    return env_from_arrays(o=o, h=h, l=l, c=c, v=v, amount=v * c,
                           dates=np.arange(T),
                           symbols=[f"S{i}" for i in range(N)])


def _factor_regime_aware(env, horizon=10):
    """因子只在低波动 regime 有真信号：低波动日=真实前向收益（加小噪），
    其余=噪声——信号按**波动 regime** 而非时序段构造（分裂两半的 low
    段都携带信号，方向可复制）。"""
    rng = np.random.default_rng(11)
    fwd = forward_returns(env, horizon)
    noise = rng.normal(0, 0.02, (T, N))
    low = realized_vol_regimes(env)["low"]
    F = noise.copy()
    ok = ~np.isnan(fwd)
    F[low] = np.where(ok[low], fwd[low], 0.0) + noise[low] * 0.3
    return F


def test_regime_masks_partition():
    env = _env()
    masks = realized_vol_regimes(env)
    union = np.zeros(T, dtype=bool)
    for m in masks.values():
        union |= m
    assert union.sum() >= int(T * 0.95), "三段近似全覆盖"
    # 中段（构造的高波动）应主要落入 high
    mid = np.zeros(T, dtype=bool)
    mid[T // 3: 2 * T // 3] = True
    high_share = (masks["high"] & mid).sum() / mid.sum()
    assert high_share > 0.6, f"构造的高波动段应主要被 high 捕获: {high_share}"


def test_u_regime_detects_instability():
    env = _env()
    F = _factor_regime_aware(env)
    out = u_regime(F, env, horizon=10)
    assert out is not None
    assert out["u"] > 1.0, f"regime 依赖因子 U 应显著为正: {out}"
    # 纯噪声因子 U 应低
    noise = np.random.default_rng(3).normal(0, 1, (T, N))
    out_n = u_regime(noise, env, horizon=10)
    assert out_n is None or out_n["u"] < out["u"], \
        "噪声因子的 U 不应超过 regime 依赖因子"


def test_split_gate_blocks_noise_hunting():
    """复验门：真 regime 结构 → keep；纯噪声的偶然极值 → 挡。"""
    env = _env()
    F = _factor_regime_aware(env)
    gate = split_replication_gate(F, env, horizon=10)
    assert gate["keep"] is True and gate["cos"] > 0, \
        f"两半方向复制（低段高、高段低）：{gate}"
    g = u_regime_gated(F, env, horizon=10)
    assert g["u"] is not None and g["u"] > 0 and g["gate"] == "passed"
    # 纯噪声：分裂半样本方向随机——门后的 U 应大量被归零/不可算
    rng = np.random.default_rng(99)
    blocked = 0
    for seed in range(6):
        noise = rng.normal(0, 1, (T, N))
        gg = u_regime_gated(noise, env, horizon=10)
        if gg["u"] in (None, 0) or gg["gate"] != "passed":
            blocked += 1
    assert blocked >= 4, f"6 个噪声因子至少 4 个被门挡住: {blocked}"


def test_gated_u_zero_semantics():
    """复验不复制 → U_regime 归零（不是 None——门主动压零有记录）。"""
    env = _env()
    rng = np.random.default_rng(5)
    noise = rng.normal(0, 1, (T, N))
    out = u_regime_gated(noise, env, horizon=10)
    if out.get("gate") == "split_replication_failed":
        assert out["u"] == 0.0, "归零语义：U_regime=0 且标注 gate 原因"
