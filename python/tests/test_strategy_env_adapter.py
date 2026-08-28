# coding=utf-8
"""P1 env 适配测试：物理截断、walk-forward 日程、结构身份。"""
import numpy as np
import pytest

from dsh_strategy_lab.env_adapter import (
    env_identity,
    fold_envs,
    region_span,
    truncate_env,
    walk_forward_schedule,
)
from _strategy_fixtures import make_env


def test_truncate_env_physical_isolation():
    env = make_env(T=30, N=3)
    cut = truncate_env(env, 9)
    assert cut.T == 10 and len(cut.dates) == 10
    assert cut.N == env.N and cut.symbols == env.symbols
    np.testing.assert_array_equal(cut.c, env.c[:10])
    # 拷贝隔离：改原数组不影响截断 env（防策略原地改写共享内存）
    env.c[5, 0] = -999.0
    assert cut.c[5, 0] != -999.0
    with pytest.raises(ValueError):
        truncate_env(env, 30)     # 越界（含端点，最大 T-1）
    with pytest.raises(ValueError):
        truncate_env(env, -1)


def test_truncate_env_carries_masks():
    env = make_env(T=20, N=2, unlisted_bars={(7, 1): True})
    cut = truncate_env(env, 9)
    assert cut.listed.shape == (10, 2)
    assert not cut.listed[7, 1]
    assert cut.amount is not None and cut.amount.shape == (10, 2)


def test_walk_forward_schedule_and_fold_envs():
    env = make_env(T=60, N=3)
    folds = walk_forward_schedule(env, n_folds=3, embargo_bars=5)
    assert [f["fold"] for f in folds] == [0, 1, 2]
    # 折连续覆盖 [0, T)，折边界互邻
    assert folds[0]["apply_t0_idx"] == 0
    for a, b in zip(folds, folds[1:]):
        assert a["apply_t1_idx"] == b["apply_t0_idx"]
    assert folds[-1]["apply_t1_idx"] == env.T
    # fit 末 = apply 首 − embargo（物理隔离）
    for f in folds:
        assert f["fit_end_idx"] == f["apply_t0_idx"] - 5
        fit_env, apply_env = fold_envs(env, f)
        assert apply_env.T == f["apply_t1_idx"]
        if f["fit_end_idx"] >= 1:
            assert fit_env.T == f["fit_end_idx"]
    # 首折 fit 区存在（embargo=0 才允许 fit_end=0）
    assert folds[0]["fit_end_idx"] < 0 or folds[0]["fit_end_idx"] >= 1


def test_walk_forward_insufficient_raises():
    env = make_env(T=10, N=2)
    with pytest.raises(ValueError, match="样本不足"):
        walk_forward_schedule(env, n_folds=8)


def test_region_span_and_identity_stability():
    env = make_env(T=40, N=3)
    t0, t1 = region_span(env, t0_date=env.dates[10], t1_date=env.dates[30])
    assert (t0, t1) == (10, 30)
    assert region_span(env) == (0, env.T)
    id1, id2 = env_identity(env), env_identity(truncate_env(env, 39))
    assert id1 == id2                      # 同面板（截断到最后一天）身份一致
    assert id1["T"] == 40 and id1["N"] == 3
    assert len(id1["symbols_hash"]) == 16
