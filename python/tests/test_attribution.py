# -*- coding: utf-8 -*-
"""逐腿归因产品化（调度器票 2a）：形态识别/置零变换确定性/Δ_k 语义/
缓存 roundtrip/重放注册。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining import derivation
from dsh_factor_mining.discipline import source_fingerprint
from dsh_factor_mining.factor.attribution import (
    cached_attribution, extract_legs, leg_attribution, renormalized_drop_source,
    zero_leg_source)

QUINT = '''def factor(env):
    import pandas as pd
    s1 = env.c * 1.0
    s2 = env.v * 2.0
    s3 = env.amount * 3.0
    return ((s1 + s2 + s3) / 3.0).values
'''

SINGLE = "def factor(env):\n    return (env.c * 1.0).values\n"


def test_extract_legs_multileg_and_single():
    assert extract_legs(QUINT) == ["s1", "s2", "s3"]
    assert extract_legs(SINGLE) is None, "单因子诚实不可拆"


def test_zero_leg_deterministic_and_semantics():
    z1 = zero_leg_source(QUINT, "s2")
    z2 = zero_leg_source(QUINT, "s2")
    assert z1 == z2, "确定性字符串变换（I2 重放基础）"
    assert "s2 = 0.0" in z1
    assert "s1 = env.c * 1.0" in z1 and "s3 = env.amount * 3.0" in z1
    assert source_fingerprint(z1) != source_fingerprint(QUINT)
    assert zero_leg_source(QUINT, "sX") is None, "不存在的腿 → None"


def test_renormalized_drop_only_for_pure_sum():
    r = renormalized_drop_source(QUINT, "s2")
    assert r is not None
    assert "/ 2.0" in r and "s2 = 0.0" in r, "等权和形态：N→N−1 且腿置零"
    # 分子不是纯腿和（含中间量引用）→ None（调用方退回 zero 口径）
    src2 = "def factor(env):\n    s1 = env.c\n    s2 = env.v\n    return (s1 * 2.0 + s2).values\n"
    assert renormalized_drop_source(src2, "s2") is None


def test_leg_attribution_delta_semantics():
    """Δ_k = Q(f) − Q(f\\k)：注入确定性 q_fn（s2 扛旗的极端构造）。"""
    def q_fn(src: str):
        base = 0.0
        if "env.c * 1.0" in src:
            base += 0.05
        if "env.v * 2.0" in src:
            base += 1.0
        if "env.amount * 3.0" in src:
            base += 0.05
        return base
    rep = leg_attribution(QUINT, q_fn)
    assert rep["base_Q"] == pytest.approx(1.1)
    assert rep["deltas"]["s1"] == pytest.approx(0.05), "拆 s1 损失其贡献"
    assert rep["deltas"]["s2"] == pytest.approx(1.0)
    assert rep["deltas"]["s3"] == pytest.approx(0.05)
    assert rep["dominance"] > 0.5, "单腿扛旗 → 集中度高（H_attr 低）"
    assert rep["incomplete"] is False and rep["n_evals"] == 4


def test_leg_attribution_honest_incomplete():
    rep = leg_attribution(QUINT, lambda src: None)
    assert rep is not None and rep["incomplete"] is True
    assert leg_attribution(SINGLE, lambda src: 1.0) is None, \
        "非多腿 → None（不硬拆）"


def test_cache_roundtrip_and_hit_rate(tmp_path):
    calls = {"n": 0}

    def q_fn(src):
        calls["n"] += 1
        return 1.0
    h = source_fingerprint(QUINT)
    rep1, hit1 = cached_attribution(str(tmp_path), QUINT, h, q_fn)
    n_after_miss = calls["n"]
    rep2, hit2 = cached_attribution(str(tmp_path), QUINT, h, q_fn)
    assert hit1 is False and hit2 is True, "第二次命中缓存"
    assert calls["n"] == n_after_miss == 4, "miss 恰算一次（base+3腿），命中零算"
    assert rep1 == rep2


def test_replay_registry_wired():
    child = derivation.replay_child("drop_leg", QUINT, {"leg": "s1"})
    assert child is not None
    assert source_fingerprint(child) == source_fingerprint(
        zero_leg_source(QUINT, "s1")), "注册表重放与直接变换一致"
