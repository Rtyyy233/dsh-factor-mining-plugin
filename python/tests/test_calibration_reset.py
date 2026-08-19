# coding=utf-8
"""市场口径泛化 + factor_state_reset 测试。

Run: cd python && python -m pytest tests/test_calibration_reset.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError
from dsh_factor_mining.data.adapters import (
    CALIBRATION_PROFILES,
    DataConfig,
    resolve_calibration,
)
from dsh_factor_mining.factor.env import Calibration, FactorEnv
from dsh_factor_mining.factor.evaluate import _forward_returns, evaluate

FACTOR_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _synthetic(T=900, N=40, seed=4, start="2019-01-02", with_limit_bar=False):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            high, low = p * 1.01, p * 0.99
            if with_limit_bar and i == 0 and t == 100:
                high = low = p  # 一字板 bar
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}", "open": p * 1.001,
                         "high": high, "low": low, "close": p,
                         "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    return pd.DataFrame(rows)


def _make_env(df: pd.DataFrame, calibration: Calibration | None = None) -> FactorEnv:
    """直接从长表构建 FactorEnv（绕过 config 管道，单测用）。"""
    piv = df.pivot(index="eob", columns="symbol", values="close")
    dates = piv.index
    symbols = list(piv.columns)
    mats = {}
    for field in ("open", "high", "low", "close", "volume", "amount"):
        mats[field] = df.pivot(index="eob", columns="symbol", values=field).reindex(
            index=dates, columns=symbols)
    valid = mats["close"].notna().values
    return FactorEnv(
        mats["open"].values, mats["high"].values, mats["low"].values,
        mats["close"].values, mats["volume"].values,
        dates, symbols, listed=valid,
        amount=mats["amount"].values, calibration=calibration)


def _auto_regions(df: pd.DataFrame) -> tuple[str, str]:
    """与 build_factor_env 的 60/20/20 自适应保底同式（直测对照用）。"""
    dates = pd.DatetimeIndex(sorted(df["eob"].unique()))
    n = len(dates)
    return str(dates[int(n * 0.6)].date()), str(dates[int(n * 0.8)].date())


def test_calibration_explicit_regions_determinism():
    """同口径（显式分界）两次评估必须逐位一致；分界缺失必须报错不静默。"""
    df = _synthetic()
    F = (df.pivot(index="eob", columns="symbol", values="close") /
         df.pivot(index="eob", columns="symbol", values="close").shift(20) - 1.0).values
    cal = Calibration(dev_end="2021-01-01", sel_end="2024-01-01")
    r1 = evaluate(F.copy(), _make_env(df, cal))
    r2 = evaluate(F.copy(), _make_env(df, cal))
    assert r1["ic_mean_train"] == r2["ic_mean_train"]
    assert r1["ic_ir_train"] == r2["ic_ir_train"]
    # 分界未设置（直调 API）→ 明确报错，不静默用任何默认
    try:
        evaluate(F.copy(), _make_env(df, Calibration()))
        raise AssertionError("分界缺失应报错")
    except ValueError as e:
        assert "三区" in str(e)


def test_regions_auto_fallback_and_report():
    """config 未指定分界 → 60/20/20 自适应保底 + load 报告 regions_mode=auto。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        df = _synthetic(T=1000, N=30)
        data_path = root / "panel.parquet"
        df.to_parquet(data_path)
        b = Bridge(state_root=str(root / "state"))
        cfg = {"version": 1, "environments": {"etf": {
            "source": {"type": "parquet", "path": str(data_path)}, "layout": "long",
            "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                        "low": "low", "close": "close", "volume": "volume",
                        "amount": "amount"}}}}
        b.dispatch("config.save", {"config": cfg})
        loaded = b.dispatch("data.load", {"envId": "primary"})
        cal = loaded["calibration"]
        exp_dev, exp_sel = _auto_regions(df)
        assert cal["dev_end"] == exp_dev and cal["sel_end"] == exp_sel
        assert "auto" in cal["regions_mode"]

        # 显式分界 → regions_mode=manual
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["environments"]["etf"]["calibration"] = {"dev_end": exp_dev, "sel_end": exp_sel}
        b.dispatch("config.save", {"config": cfg2})
        loaded2 = b.dispatch("data.load", {"envId": "primary"})
        assert loaded2["calibration"]["regions_mode"] == "manual"


def test_regions_mismatch_rejected():
    """显式分界与数据不匹配（train 空 / test 空 / 顺序错）→ 拒绝并指明。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        df = _synthetic(T=500, N=30)
        data_path = root / "panel.parquet"
        df.to_parquet(data_path)

        def _load_with(dev_end, sel_end):
            b = Bridge(state_root=str(root / f"state_{dev_end}_{sel_end}"))
            b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": {
                "source": {"type": "parquet", "path": str(data_path)}, "layout": "long",
                "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                            "low": "low", "close": "close", "volume": "volume",
                            "amount": "amount"},
                "calibration": {"dev_end": dev_end, "sel_end": sel_end}}}}})
            return b.dispatch("data.load", {"envId": "primary"})

        try:
            _load_with("2010-01-01", "2024-01-01")  # dev_end 早于数据起点 → train 空
            raise AssertionError("train 空应报错")
        except BridgeError as e:
            assert "train 区为空" in e.message, e.message
        try:
            _load_with("2020-01-01", "2030-01-01")  # sel_end 晚于末端 → test 空
            raise AssertionError("test 空应报错")
        except BridgeError as e:
            assert "test 区为空" in e.message, e.message


def test_calibration_horizon_override_changes_fwd():
    df = _synthetic()
    env_h20 = _make_env(df, Calibration(horizon=20))
    env_h5 = _make_env(df, Calibration(horizon=5))
    f20 = _forward_returns(env_h20)
    f5 = _forward_returns(env_h5)
    assert not np.allclose(f20[np.isfinite(f20)], f5[np.isfinite(f20) & np.isfinite(f5)])
    c = env_h20.c
    # t1 口径手工验证: fwd[t] = c[t+H]/o[t+1]
    h = 5
    expect = c[h, 0] / env_h5.o[1, 0] - 1.0
    assert abs(f5[0, 0] - expect) < 1e-12


def test_calibration_t0_execution():
    df = _synthetic()
    env = _make_env(df, Calibration(execution="t0", horizon=5))
    fwd = _forward_returns(env)
    expect = env.c[5, 0] / env.c[0, 0] - 1.0
    assert abs(fwd[0, 0] - expect) < 1e-12


def test_limit_up_down_mask():
    """一字板 bar（h==l）在 limit_up_down_mask=true 时从可交易 mask 剔除。"""
    from dsh_factor_mining.factor.evaluate import _pit_mask
    df = _synthetic(with_limit_bar=True)
    env_off = _make_env(df, Calibration(limit_up_down_mask=False))
    env_on = _make_env(df, Calibration(limit_up_down_mask=True))
    pit_off = _pit_mask(env_off)
    pit_on = _pit_mask(env_on)
    t = list(env_on.dates).index(list(env_on.dates)[100])
    assert pit_off[t, 0] is np.True_ or bool(pit_off[t, 0])
    assert not bool(pit_on[t, 0]), "一字板 bar 应被 mask 剔除"


def test_resolve_calibration_profiles_and_errors():
    assert resolve_calibration({}) == {}
    cal = resolve_calibration({"profile": "cn_stock_daily"})
    assert cal["limit_up_down_mask"] is True and cal["execution"] == "t1"
    cal_m = resolve_calibration({"profile": "cn_etf_minute"})
    assert cal_m["execution"] == "t0" and cal_m["annualization"] == 48 * 252
    cal_ov = resolve_calibration({"profile": "cn_etf_daily", "horizon": 10})
    assert cal_ov["horizon"] == 10 and cal_ov["limit_up_down_mask"] is False
    try:
        resolve_calibration({"profile": "cn_us_daily"})
        raise AssertionError("未知 profile 应报错")
    except Exception as e:
        assert "cn_etf_daily" in str(e)
    try:
        resolve_calibration({"horizon": 10, "bad_field": 1})
        raise AssertionError("未知字段应报错")
    except Exception as e:
        assert "bad_field" in str(e)


def test_config_calibration_roundtrip_and_worker_apply():
    """config calibration 段 → bridge data.load → worker evaluate 全链生效。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        df = _synthetic()
        data_path = root / "panel.parquet"
        df.to_parquet(data_path)
        b = Bridge(state_root=str(root / "state"))
        env_spec = {
            "label": "synthetic", "kind": "panel",
            "source": {"type": "parquet", "path": str(data_path), "options": {}},
            "layout": "long",
            "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                        "low": "low", "close": "close", "volume": "volume", "amount": "amount"},
            "constraints": {"minSymbols": 10, "minDates": 100,
                            "requireFiniteOhlcv": True, "allowZeroVolume": True},
            "calibration": {"profile": "cn_etf_daily", "horizon": 5, "cost_bps": 5},
        }
        b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": env_spec}}})
        assert b.dispatch("data.load", {"envId": "primary"})["ok"] is True
        # worker 路径（默认 executionMode=worker）必须应用 horizon=5（分界自适应同式）
        diag = b.dispatch("factor.evaluate",
                          {"envId": "primary", "source": FACTOR_SOURCE, "stage": "development"})
        exp_dev, exp_sel = _auto_regions(df)
        env_direct = _make_env(df, Calibration(horizon=5, cost_bps=5,
                                                dev_end=exp_dev, sel_end=exp_sel))
        F_direct = (df.pivot(index="eob", columns="symbol", values="close") /
                    df.pivot(index="eob", columns="symbol", values="close").shift(20) - 1.0).values
        r_direct = evaluate(F_direct, env_direct)
        assert diag["ic_mean_train"] == r_direct["ic_mean_train"], "worker 路径口径必须与直调一致"
        assert diag["ic_n_train"] == r_direct["ic_n_train"]


def test_state_reset_scopes():
    """state.reset：scope 清对应文件 + 备份 + test_lock 永不删。"""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "state"
        state.mkdir(parents=True)
        files = ["trail.json", "explored_paths.json", "search_paths.json",
                 "mining_state.json", "null_landscape.json", "operator_set.json",
                 "registry.json", "data-config.json", "test_lock.json"]
        for f in files:
            (state / f).write_text("{}", encoding="utf-8")
        b = Bridge(state_root=str(state))

        r = b.dispatch("state.reset", {"scope": "mining"})
        assert set(r["removed"]) == {"trail.json", "explored_paths.json",
                                     "search_paths.json", "mining_state.json"}
        assert (state / "null_landscape.json").exists(), "landscape 不受 mining scope 影响"
        backup = Path(r["backup"])
        assert (backup / "trail.json").exists(), "删除前必须备份"

        r_all = b.dispatch("state.reset", {"scope": "all"})
        assert (state / "test_lock.json").exists(), "test_lock 永不被工具重置"
        assert not (state / "data-config.json").exists()
        assert b.dispatch("status", {})["dataConfigured"] is False, "config 清除后内存归零"

        try:
            b.dispatch("state.reset", {"scope": "everything"})
            raise AssertionError("未知 scope 应报错")
        except BridgeError as e:
            assert e.code == -32602


def test_state_reset_config_scope_restores_cold_start():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data_path = root / "panel.parquet"
        _synthetic(T=300, N=20).to_parquet(data_path)
        state = root / "state"
        b = Bridge(state_root=str(state))
        cfg = {"version": 1, "environments": {"etf": {
            "source": {"type": "parquet", "path": str(data_path)},
            "layout": "long",
            "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                        "low": "low", "close": "close", "volume": "volume", "amount": "amount"}}}}
        b.dispatch("config.save", {"config": cfg})
        assert b.dispatch("status", {})["dataConfigured"] is True
        r = b.dispatch("state.reset", {"scope": "config"})
        assert r["removed"] == ["data-config.json"]
        st = b.dispatch("status", {})
        assert st["dataConfigured"] is False and st["nextStep"] == "factor_data_probe"
