# coding=utf-8
"""panel_mmap bundle 单测：字节级等价 / 只读映射 / 半写失效。"""
import json

import numpy as np
import pandas as pd
import pytest

from dsh_factor_mining.factor.env import Calibration, FactorEnv
from dsh_factor_mining.panel_mmap import (bundle_valid, bundle_paths,
                                          load_bundle_mmap, save_bundle)


def test_data_quality_roundtrip(tmp_path):
    """Phase 2：bundle 携带 dataQuality，装载经 panel_meta 还原。"""
    env = _make_env()
    base = tmp_path / "fp7"
    dq = {"mode": "report", "preListingHoles": 5, "inWindowHoles": 7,
          "postLastValidHoles": 3, "note": "测试"}
    save_bundle(base, env, data_quality=dq)
    env2 = load_bundle_mmap(base)
    assert getattr(env2, "panel_meta", None) is not None
    assert env2.panel_meta.get("dataQuality") == dq


def test_data_quality_default_none(tmp_path):
    env = _make_env()
    base = tmp_path / "fp8"
    save_bundle(base, env)
    env2 = load_bundle_mmap(base)
    assert env2.panel_meta.get("dataQuality") is None


def _make_env(t=40, n=16, amount=True, listed=True, seed=7):
    rng = np.random.default_rng(seed)
    base = rng.normal(size=(t, n))
    c = base.copy()
    c[0, 0] = np.nan                       # NaN 模式必须逐位保留
    c[:, 3] = np.nan
    env = FactorEnv(
        o=rng.normal(size=(t, n)), h=rng.normal(size=(t, n)),
        l=rng.normal(size=(t, n)), c=c, v=np.abs(rng.normal(size=(t, n))) + 1,
        dates=pd.date_range("2024-01-01", periods=t, freq="D"),
        symbols=[f"SYM{i:03d}" for i in range(n)],
        listed=(rng.random((t, n)) > 0.1) if listed else None,
        amount=(np.abs(rng.normal(size=(t, n))) * 1000) if amount else None,
        calibration=Calibration(horizon=10, cost_bps=30.0, dev_end="2024-12-31"),
    )
    return env


def _assert_env_equal(a, b):
    for f in ("o", "h", "l", "c", "v"):
        assert np.array_equal(getattr(a, f), getattr(b, f), equal_nan=True)
    if a.amount is None:
        assert b.amount is None
    else:
        assert np.array_equal(a.amount, b.amount, equal_nan=True)
    assert np.array_equal(a.listed, b.listed)
    assert list(a.dates) == list(b.dates)
    assert list(a.symbols) == list(b.symbols)
    for k in ("horizon", "cost_bps", "dev_end"):
        assert getattr(a.calibration, k) == getattr(b.calibration, k)


def test_roundtrip_bit_exact(tmp_path):
    env = _make_env()
    base = tmp_path / "fp1"
    save_bundle(base, env)
    assert bundle_valid(base)
    env2 = load_bundle_mmap(base)
    _assert_env_equal(env, env2)


def test_roundtrip_without_amount_listed(tmp_path):
    env = _make_env(amount=False, listed=False)
    base = tmp_path / "fp2"
    save_bundle(base, env)
    env2 = load_bundle_mmap(base)
    assert env2.amount is None
    _assert_env_equal(env, env2)


def test_memmap_readonly_shared(tmp_path):
    """映射必须只读：env 矩阵不可写（防因子侧突变污染共享文件）。"""
    env = _make_env()
    base = tmp_path / "fp3"
    save_bundle(base, env)
    env2 = load_bundle_mmap(base)
    for f in ("o", "h", "l", "c", "v"):
        arr = getattr(env2, f)
        assert not arr.flags.writeable
    with pytest.raises(ValueError):
        env2.c[0, 0] = 1.0


def test_bundle_valid_detects_partial_write(tmp_path):
    """bin 在 meta 不在 = 半写 = 无效（提交判据）。"""
    env = _make_env()
    base = tmp_path / "fp4"
    save_bundle(base, env)
    bin_p, meta_p = bundle_paths(base)
    meta_p.unlink()
    assert not bundle_valid(base)
    with pytest.raises(Exception):
        load_bundle_mmap(base)


def test_truncated_bin_rejected(tmp_path):
    env = _make_env()
    base = tmp_path / "fp5"
    save_bundle(base, env)
    bin_p, meta_p = bundle_paths(base)
    raw = bin_p.read_bytes()
    bin_p.write_bytes(raw[:-8])            # 尾部截断
    assert not bundle_valid(base)
    with pytest.raises(Exception):
        load_bundle_mmap(base)


def test_meta_records_layout(tmp_path):
    env = _make_env()
    base = tmp_path / "fp6"
    save_bundle(base, env)
    bin_p, meta_p = bundle_paths(base)
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    assert meta["total_bytes"] == bin_p.stat().st_size
    assert set(meta["fields"]) == {"o", "h", "l", "c", "v", "amount", "listed"}
    off = 0
    for name in ("o", "h", "l", "c", "v", "amount", "listed"):
        f = meta["fields"][name]
        assert f["offset"] == off
        assert f["shape"] == [40, 16]
        off += int(np.prod(f["shape"])) * (1 if f["dtype"] == "uint8" else 8)
