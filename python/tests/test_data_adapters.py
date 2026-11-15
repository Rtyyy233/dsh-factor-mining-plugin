# coding=utf-8
"""Synthetic-fixture tests for the user-data adaptation layer.

Run: PYTHONPATH=src python tests/test_data_adapters.py
（ts_single 新用例走 pytest：cd python && python -m pytest tests/test_data_adapters.py -q）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.data.adapters import (
    DataConfig,
    DataError,
    EnvironmentSpec,
    MappingSpec,
    SourceSpec,
    build_factor_env,
    build_ts_env,
    normalize_environment,
    probe_file,
    validate_env_lightweight,
)
from dsh_factor_mining.factor.env import Calibration, FactorEnv, TsEnv
from dsh_factor_mining.worker import load_env_npz, write_env_npz


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


# ---------------------------------------------------------------------------
# ts_single（factor-mining-futures 线）：from_dict → normalize → build_ts_env
# ---------------------------------------------------------------------------

def _ts_parquet(path, T=520, with_close2=True, with_oi=True, hole=None):
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2015-01-02", periods=T)
    close = 3000.0 + np.cumsum(rng.normal(0.0, 8.0, T))
    df = pd.DataFrame({
        "date": dates,
        "open": close * 1.001, "high": close * 1.01, "low": close * 0.99,
        "close": close,
        "volume": rng.integers(1000, 50000, T).astype(float),
        "oi": rng.integers(50000, 200000, T).astype(float),
    })
    if with_close2:
        df["close2"] = close * 1.02
    if not with_oi:
        df = df.drop(columns=["oi"])
    if hole is not None:
        df.loc[hole, "close"] = np.nan
    df.to_parquet(path, index=False)
    return df


def _ts_config(path, mapping_extra=None, calibration=None):
    mapping = {"date": "date", "open": "open", "high": "high", "low": "low",
               "close": "close", "volume": "volume", "oi": "oi"}
    if mapping_extra:
        mapping.update(mapping_extra)
    env = {"kind": "ts_single",
           "source": {"type": "parquet", "path": str(path)},
           "mapping": mapping}
    if calibration:
        env["calibration"] = calibration
    return {"version": 1, "environments": {"rb": env}}


def test_ts_single_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        dd = Path(d)
        path = dd / "rb_main_daily.parquet"
        df = _ts_parquet(path)
        # 同目录数据指纹存在 → 附在 normalize 输出（不存在不报错）
        (dd / "tsdata_fingerprint.json").write_text(
            json.dumps({"sha256": "abc123def4567890", "instrument": "rb"}),
            encoding="utf-8")
        cfg = DataConfig.from_dict(_ts_config(path, {"close2": "close2"},
                                              {"limit_pct": 0.06, "horizon": 10}))
        spec = cfg.environments["rb"]
        assert spec.kind == "ts_single"
        assert spec.mapping.oi == "oi" and spec.mapping.close2 == "close2"
        mats = normalize_environment(spec)
        assert mats["kind"] == "ts_single"
        assert mats["tsdata_fingerprint"]["sha256"] == "abc123def4567890"
        T = len(df)
        assert len(mats["dates"]) == T
        for f in ("open", "high", "low", "close", "volume", "oi"):
            assert isinstance(mats[f], np.ndarray)
            assert mats[f].shape == (T,) and mats[f].dtype == np.float64
        assert mats["close2"].shape == (T,)

        env = build_ts_env(mats, calibration=spec.calibration, instrument="rb")
        assert isinstance(env, TsEnv)
        assert env.T == T and env.instrument == "rb"
        assert env.limit_pct == 0.06
        assert env.calibration.cost_bps == 2.0          # 期货单品种缺省
        assert env.calibration.horizon == 10
        assert env.calibration.regions_auto is True     # 60/20/20 auto 回填
        assert pd.Timestamp(env.calibration.dev_end) < pd.Timestamp(env.calibration.sel_end)
        assert env.close2 is not None and env.close2.shape == (T,)

        # 显式 cost_bps 覆盖缺省
        cfg2 = DataConfig.from_dict(_ts_config(
            path, {"close2": "close2"},
            {"limit_pct": 0.05, "cost_bps": 5.0, "horizon": 10}))
        env2 = build_ts_env(normalize_environment(cfg2.environments["rb"]),
                            calibration=cfg2.environments["rb"].calibration,
                            instrument="rb")
        assert env2.calibration.cost_bps == 5.0

        # 显式三区：regions_auto=False、原值透传
        cfg3 = DataConfig.from_dict(_ts_config(
            path, {"close2": "close2"},
            {"dev_end": "2016-01-04", "sel_end": "2016-08-01", "horizon": 10}))
        env3 = build_ts_env(normalize_environment(cfg3.environments["rb"]),
                            calibration=cfg3.environments["rb"].calibration,
                            instrument="rb")
        assert env3.calibration.regions_auto is False
        assert env3.calibration.dev_end == "2016-01-04"
        assert env3.calibration.sel_end == "2016-08-01"

        # close2 未映射 → None；指纹不存在的目录 → 键不存在（不报错）
        with tempfile.TemporaryDirectory() as d2:
            path2 = Path(d2) / "solo.parquet"
            _ts_parquet(path2)
            cfg4 = DataConfig.from_dict(_ts_config(path2))
            mats4 = normalize_environment(cfg4.environments["rb"])
            assert "tsdata_fingerprint" not in mats4
            env4 = build_ts_env(mats4,
                                calibration=cfg4.environments["rb"].calibration,
                                instrument="rb")
            assert env4.close2 is None


def test_ts_single_missing_oi_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ts.parquet"
        _ts_parquet(path)
        raw = _ts_config(path)
        del raw["environments"]["rb"]["mapping"]["oi"]
        with pytest.raises(DataError, match="oi"):
            DataConfig.from_dict(raw)      # 写配置时即拒：ts_single 必填 date+OHLCV+oi


def test_ts_single_holes_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ts.parquet"
        _ts_parquet(path, hole=100)
        spec = DataConfig.from_dict(_ts_config(path)).environments["rb"]
        with pytest.raises(DataError, match="洞"):
            normalize_environment(spec)


def test_ts_single_lightweight_and_limit_pct_domain():
    with tempfile.TemporaryDirectory() as d:
        # oi 列不存在于数据 → validate_env_lightweight（ts 分支必查 oi）拒绝
        path = Path(d) / "nooi.parquet"
        _ts_parquet(path, with_oi=False)
        spec = DataConfig.from_dict(
            _ts_config(path, {"oi": "oi"})).environments["rb"]
        with pytest.raises(DataError, match="oi"):
            validate_env_lightweight(spec)
        # limit_pct 域：0.005–0.5 之外拒绝（面板不消费该键，行为不变）
        path2 = Path(d) / "ok.parquet"
        _ts_parquet(path2)
        with pytest.raises(DataError, match="limit_pct"):
            DataConfig.from_dict(_ts_config(path2, calibration={"limit_pct": 0.6}))
        with pytest.raises(DataError, match="limit_pct"):
            DataConfig.from_dict(_ts_config(path2, calibration={"limit_pct": 0.001}))
        cfg = DataConfig.from_dict(_ts_config(path2, calibration={"limit_pct": 0.05}))
        assert cfg.environments["rb"].calibration["limit_pct"] == 0.05


def test_ts_env_npz_roundtrip():
    from dataclasses import asdict

    rng = np.random.default_rng(5)
    T = 300
    dates = list(pd.bdate_range("2015-01-02", periods=T))
    c = 3000.0 + np.cumsum(rng.normal(0.0, 5.0, T))
    cal = Calibration(horizon=10, dev_end="2015-09-01", sel_end="2015-12-01",
                      cost_bps=3.0)
    for close2 in (c * 1.01, None):          # close2 两形态：有/无
        env = TsEnv(
            c * 1.001, np.maximum(c * 1.001, c), np.minimum(c * 0.999, c), c,
            np.abs(rng.normal(1000, 50, T)), np.abs(rng.normal(90000, 500, T)),
            dates, instrument="rb", close2=close2, limit_pct=0.06,
            calibration=cal)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "env.npz"
            write_env_npz(str(p), env)
            env2 = load_env_npz(str(p))
        assert isinstance(env2, TsEnv)
        for f in ("o", "h", "l", "c", "v", "oi"):
            assert np.array_equal(getattr(env2, f), getattr(env, f))
        if close2 is None:
            assert env2.close2 is None
        else:
            assert np.array_equal(env2.close2, env.close2)
        assert [str(x) for x in env2.dates] == [str(x) for x in env.dates]
        assert env2.instrument == "rb" and env2.limit_pct == 0.06
        assert asdict(env2.calibration) == asdict(env.calibration)


def test_panel_env_npz_roundtrip_unchanged():
    """面板 npz 往返（红线 1：panel 路径行为不变，meta 无 instrument 键）。"""
    rng = np.random.default_rng(6)
    T, N = 60, 5
    dates = list(pd.bdate_range("2020-01-02", periods=T))
    syms = [f"S{i}" for i in range(N)]
    c = 10 + np.cumsum(rng.normal(0, 0.1, (T, N)), axis=0)
    env = FactorEnv(c * 1.001, c * 1.01, c * 0.99, c, np.abs(rng.normal(1e4, 1e3, (T, N))),
                    dates, syms)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "panel.npz"
        write_env_npz(str(p), env)
        env2 = load_env_npz(str(p))
    assert isinstance(env2, FactorEnv)
    assert env2.T == T and env2.N == N
    for f in ("o", "h", "l", "c", "v"):
        assert np.array_equal(getattr(env2, f), getattr(env, f))


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
