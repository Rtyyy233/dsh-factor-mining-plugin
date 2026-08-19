# coding=utf-8
"""Synthetic-fixture tests for the user-data adaptation layer.

Run: PYTHONPATH=src python tests/test_data_adapters.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.data.adapters import (
    DataConfig,
    EnvironmentSpec,
    MappingSpec,
    SourceSpec,
    build_factor_env,
    normalize_environment,
    probe_file,
)


def _synthetic_long(T=520, N=40, seed=3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=T)
    rows = []
    for i, sym in enumerate([f"X{i:02d}" for i in range(N)]):
        price = 20 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            c = max(price[t], 0.5)
            rows.append({
                "eob": dates[t],
                "symbol": sym,
                "open": c * 1.001,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "volume": float(rng.integers(100, 10000)),
                "amount": float(rng.integers(1000, 100000)),
            })
    return pd.DataFrame(rows)


def _spec(path, layout="long"):
    return EnvironmentSpec(
        id="primary",
        source=SourceSpec(type="parquet", path=str(path)),
        layout=layout,
        mapping=MappingSpec(symbol="symbol", date="eob", open="open", high="high",
                            low="low", close="close", volume="volume", amount="amount"),
        constraints={"minSymbols": 5, "minDates": 50, "requireFiniteOhlcv": True,
                     "allowZeroVolume": True},
    )


def test_long_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        df = _synthetic_long()
        path = Path(d) / "long.parquet"
        df.to_parquet(path)
        report = probe_file(str(path))
        assert report["ok"], report
        assert report["suggested_mapping"]["close"] == "close"

        mats = normalize_environment(_spec(path))
        env = build_factor_env(mats)
        assert env.T == df["eob"].nunique()
        assert env.N == df["symbol"].nunique()
        assert env.amount is not None


def test_long_minimal_csv():
    with tempfile.TemporaryDirectory() as d:
        df = _synthetic_long().drop(columns=["amount"])
        path = Path(d) / "long.csv"
        df.to_csv(path, index=False)
        spec = _spec(path)
        spec.source.type = "csv"
        mats = normalize_environment(spec)
        env = build_factor_env(mats)
        assert env.amount is None


def test_wide_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        df = _synthetic_long()
        wide = df.pivot(index="eob", columns="symbol", values=["open", "high", "low", "close", "volume", "amount"])
        wide.columns = pd.MultiIndex.from_tuples([(c[1], c[0]) for c in wide.columns], names=["symbol", "field"])
        path = Path(d) / "wide.parquet"
        wide.to_parquet(path)
        spec = _spec(path, layout="multiindex")
        spec.mapping.date = None
        mats = normalize_environment(spec)
        env = build_factor_env(mats)
        assert env.T == wide.shape[0]
        assert env.N == wide.columns.get_level_values(0).nunique()


def test_per_symbol_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        df = _synthetic_long(T=260, N=6)
        for sym, g in df.groupby("symbol"):
            g.to_parquet(Path(d) / f"{sym}.parquet", index=False)
        spec = EnvironmentSpec(
            id="primary",
            source=SourceSpec(type="glob", path=str(Path(d) / "*.parquet")),
            layout="per_symbol",
            mapping=MappingSpec(symbol=None, date="eob", open="open", high="high",
                                low="low", close="close", volume="volume", amount="amount"),
            constraints={"minSymbols": 3, "minDates": 50, "requireFiniteOhlcv": True,
                         "allowZeroVolume": True},
        )
        mats = normalize_environment(spec)
        env = build_factor_env(mats)
        assert env.N == 6


def test_config_roundtrip():
    cfg = DataConfig(environments={"primary": _spec("unused.parquet")})
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        cfg.save(path)
        cfg2 = DataConfig.load(path)
        assert cfg2.environments["primary"].mapping.close == "close"


if __name__ == "__main__":
    failures = []
    for fn in [test_long_roundtrip, test_long_minimal_csv, test_wide_roundtrip,
               test_per_symbol_roundtrip, test_config_roundtrip]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e!r}")
    if failures:
        raise SystemExit(1)
    print("DATA_ADAPTER_TESTS PASS")
