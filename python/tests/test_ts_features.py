# coding=utf-8
"""高频特征低频化（f_* 特征字段）专项单测（2026-11-07 追加需求）。

覆盖：
- TsEnv 特征字段契约（f_ 命名空间/非法名拒/长度校验/属性访问/horizon 视图）
- adapters：自动探测 f_* 数值列 + 显式 features 映射 + 暖期 NaN 允许并报告
- worker npz 特征序列化往返
- causality：**经由特征字段读未来被抓**（FUTURE_LEAK）——特征不扰动的
  通路在此测试锁死；干净因子 causal
- noise：噪声世界特征保持真实（真特征-假价格语义）
- random_gen：叶子面含 f_*；series_mode 渲染可 exec
- bridge 端到端：含特征 parquet → load → evaluate 因子消费 f_ 字段
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from dsh_factor_mining.data.adapters import (DataConfig, DataError,
                                             build_ts_env,
                                             normalize_environment)
from dsh_factor_mining.factor.causality import (check_causality,
                                                check_causality_nan)
from dsh_factor_mining.factor.env import Calibration, TsEnv
from dsh_factor_mining.factor.noise import generate_noise_world
from dsh_factor_mining.factor.random_gen import (env_leaves, generate_one,
                                                 render_factor_source,
                                                 ts_opset)


def _mk_env(T=600, seed=7, with_close2=False, features=None) -> TsEnv:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-02", periods=T)
    c = 3500.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, T))
    o = c * (1.0 + rng.normal(0.0, 0.002, T))
    cal = Calibration(horizon=20, dev_end="2021-06-01", sel_end="2022-06-01")
    return TsEnv(o, np.maximum(o, c) * 1.002, np.minimum(o, c) * 0.998,
                 c, rng.lognormal(9.0, 0.4, T), rng.lognormal(12.0, 0.3, T),
                 dates, "rb",
                 close2=(c * 1.001 if with_close2 else None),
                 limit_pct=0.06, calibration=cal, features=features)


# ---------------------------------------------------------------------------
# TsEnv 契约
# ---------------------------------------------------------------------------

def test_feature_namespace_and_validation():
    T = 50
    good = {"f_intraday_vol": np.arange(T, dtype=float)}
    env = _mk_env(T=T, features=good)
    assert env.features["f_intraday_vol"].shape == (T,)
    assert np.array_equal(env.f_intraday_vol, good["f_intraday_vol"])
    assert env.feature_nan_counts() == {"f_intraday_vol": 0}
    # 非法名：无 f_ 前缀 / 大写 / 数字开头后缀
    for bad in ({"intraday_vol": np.arange(T, dtype=float)},
                {"f_IntradayVol": np.arange(T, dtype=float)},
                {"f_1abc": np.arange(T, dtype=float)}):
        with pytest.raises(ValueError):
            _mk_env(T=T, features=bad)
    # 长度不齐
    with pytest.raises(ValueError):
        _mk_env(T=T, features={"f_x": np.arange(T - 1, dtype=float)})


def test_feature_warmup_nan_allowed():
    T = 50
    f = np.full(T, np.nan)
    f[10:] = 1.0
    env = _mk_env(T=T, features={"f_warm": f})
    assert env.feature_nan_counts() == {"f_warm": 10}


def test_horizon_view_keeps_features():
    from dsh_factor_mining.factor.evaluate import _env_horizon_view
    env = _mk_env(T=300, features={"f_x": np.arange(300, dtype=float)})
    v = _env_horizon_view(env, 10)
    assert v.calibration.horizon == 10
    assert v.features["f_x"].shape == (300,)


# ---------------------------------------------------------------------------
# adapters：自动探测 + 显式映射
# ---------------------------------------------------------------------------

def _write_feature_parquet(tmp_path, name="ts_feat.parquet", T=400):
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2019-01-02", periods=T)
    c = 3000.0 * np.cumprod(1.0 + rng.normal(0.0, 0.01, T))
    df = pd.DataFrame({
        "date": [str(d.date()) for d in dates],
        "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": rng.lognormal(9, 0.4, T), "oi": rng.lognormal(12, 0.3, T),
        "f_intraday_vol": rng.gamma(2.0, 0.001, T),
        "f_vwap_dev": rng.normal(0, 0.002, T),
        "not_a_feature": rng.normal(0, 1, T),        # 无 f_ 前缀：不探测
    })
    df.loc[0:4, "f_intraday_vol"] = np.nan           # 暖期 NaN
    p = tmp_path / name
    df.to_parquet(p)
    return p, df


def _ts_spec(path, features=None):
    return {
        "source": {"type": "parquet", "path": str(path)},
        "layout": "long",
        "kind": "ts_single",
        "mapping": {"date": "date", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "volume",
                    "oi": "oi"},
        "calibration": {"horizon": 20, "dev_end": "2019-12-01",
                        "sel_end": "2020-04-01", "limit_pct": 0.06},
        **({"features": features} if features is not None else {}),
    }


def test_adapters_auto_detect_and_explicit(tmp_path):
    p, df = _write_feature_parquet(tmp_path)
    cfg = DataConfig.from_dict({"version": 1, "environments": {
        "rb": _ts_spec(p)}})
    mats = normalize_environment(cfg.environments["rb"])
    assert sorted(mats["features"]) == ["f_intraday_vol", "f_vwap_dev"]
    assert mats["features_auto_detected"] is True
    assert int(np.isnan(mats["features"]["f_intraday_vol"]).sum()) == 5
    env = build_ts_env(mats, calibration=cfg.environments["rb"].calibration,
                       instrument="rb")
    assert env.f_vwap_dev.shape == (400,)
    # 显式映射：只取一列、且可映射到非 f_ 前缀列名
    cfg2 = DataConfig.from_dict({"version": 1, "environments": {
        "rb": _ts_spec(p, features={"f_proxy": "not_a_feature"})}})
    mats2 = normalize_environment(cfg2.environments["rb"])
    assert list(mats2["features"]) == ["f_proxy"]
    assert mats2["features_auto_detected"] is False
    # 显式非法字段名 → 拒
    with pytest.raises(DataError):
        cfg3 = DataConfig.from_dict({"version": 1, "environments": {
            "rb": _ts_spec(p, features={"bad_name": "f_vwap_dev"})}})
        normalize_environment(cfg3.environments["rb"])


def test_worker_npz_roundtrip_with_features(tmp_path):
    from dsh_factor_mining.worker import load_env_npz, write_env_npz
    T = 120
    env = _mk_env(T=T, seed=5, with_close2=True,
                  features={"f_a": np.arange(T, dtype=float),
                            "f_b": np.full(T, 0.5)})
    p = tmp_path / "env.npz"
    write_env_npz(str(p), env)
    back = load_env_npz(str(p))
    assert isinstance(back, TsEnv)
    assert sorted(back.features) == ["f_a", "f_b"]
    assert np.array_equal(back.f_a, env.f_a)
    assert back.instrument == "rb" and back.limit_pct == 0.06
    assert back.close2 is not None
    # 无特征往返不变
    env2 = _mk_env(T=T, seed=5)
    p2 = tmp_path / "env2.npz"
    write_env_npz(str(p2), env2)
    back2 = load_env_npz(str(p2))
    assert back2.features == {}


# ---------------------------------------------------------------------------
# causality：特征字段的全覆盖扰动（关键正确性点）
# ---------------------------------------------------------------------------

def test_causality_catches_leak_via_feature_field():
    """经由特征字段读未来 → FUTURE_LEAK（特征扰动覆盖锁死）。

    泄漏构造：信号 = shift(-5) 的特征（未来 5 日的特征值）。若特征
    字段不在扰动面内，F[:t0] 不会因扰动改变 → 漏检。"""
    T = 400
    rng = np.random.default_rng(11)
    f = rng.normal(0, 1, T)
    env = _mk_env(T=T, seed=11, features={"f_leak": f})

    def fn_future(env_):
        return pd.Series(env_.f_leak).shift(-5).values

    r = check_causality(fn_future, env, t0_samples=4)
    assert r["verdict"] == "FUTURE_LEAK"
    r2 = check_causality_nan(fn_future, env, t0_samples=4)
    assert r2["verdict"] == "FUTURE_LEAK"

    # 干净因子（只用过去特征）：causal
    def fn_clean(env_):
        return pd.Series(env_.f_leak).rolling(20).mean().values

    r3 = check_causality(fn_clean, env, t0_samples=4)
    assert r3["verdict"] == "causal"


def test_causality_all_nan_warmup_feature_no_crash():
    """暖期全 NaN 的特征字段：扰动尺度 NaN 安全兜底（不炸不误报）。"""
    T = 300
    f = np.full(T, np.nan)
    f[200:] = np.random.default_rng(2).normal(0, 1, T - 200)
    env = _mk_env(T=T, features={"f_warm": f})

    def fn(env_):
        return pd.Series(env_.f_warm).fillna(0.0).rolling(20).mean().values

    r = check_causality(fn, env, t0_samples=3)
    assert r["verdict"] == "causal"


# ---------------------------------------------------------------------------
# noise：真特征-假价格
# ---------------------------------------------------------------------------

def test_noise_world_keeps_features_real():
    T = 300
    feats = {"f_x": np.arange(T, dtype=float)}
    env = _mk_env(T=T, seed=9, features=feats)
    world = generate_noise_world(env, np.random.default_rng(4))
    assert isinstance(world, TsEnv)
    assert np.array_equal(world.f_x, feats["f_x"])   # 特征原样
    assert not np.allclose(world.c, env.c)           # 价格已换


# ---------------------------------------------------------------------------
# random_gen：f_* 叶子
# ---------------------------------------------------------------------------

def test_env_leaves_include_features():
    env = _mk_env(T=100, features={"f_b": np.ones(100), "f_a": np.ones(100)})
    leaves = env_leaves(env)
    assert "f_a" in leaves and "f_b" in leaves and "oi" in leaves


def test_render_series_mode_with_feature_leaf(tmp_path):
    """explore 树含 f_* 叶子 → 渲染源码可 exec 且输出 (T,)。"""
    env = _mk_env(T=300, seed=21, features={
        "f_intraday_vol": np.random.default_rng(1).gamma(2, 0.001, 300)})
    opset = ts_opset(tmp_path)
    rng = np.random.default_rng(6)
    got = None
    for _ in range(30):
        tree, _tpl = generate_one(rng, opset, leaves=env_leaves(env),
                                  require_cs=False)
        src, _imports = render_factor_source(tree, "t", series_mode=True)
        if "f_intraday_vol" in src:
            got = src
            break
    if got is None:   # 30 棵未命中特征叶（概率低）——直接手构树验证渲染
        from dsh_factor_mining.factor.random_gen import Node
        tree = Node("leaf", op="f_intraday_vol")
        got, _ = render_factor_source(tree, "t", series_mode=True)
    ns = {}
    exec(got, ns)
    out = np.asarray(ns["factor"](env))
    assert out.ndim == 1 and out.shape[0] == env.T


# ---------------------------------------------------------------------------
# bridge 端到端
# ---------------------------------------------------------------------------

def test_bridge_e2e_feature_factor(tmp_path):
    """含特征 parquet → data.load 报告特征 → evaluate 消费 f_ 字段 →
    诊断同形。"""
    from dsh_factor_mining.bridge import Bridge
    p, df = _write_feature_parquet(tmp_path)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "rb": _ts_spec(p)}}})
    ld = b.dispatch("data.load", {"envId": "rb"})
    assert ld["ok"] and sorted(ld["features"]) == ["f_intraday_vol", "f_vwap_dev"]
    assert ld["featureNaN"]["f_intraday_vol"] == 5
    src = ("import pandas as pd\n\n"
           "def factor(env):\n"
           "    v = pd.Series(env.f_intraday_vol)\n"
           "    d = pd.Series(env.f_vwap_dev)\n"
           "    sig = -v * 100.0 + d * 10.0\n"
           "    z = (sig - sig.rolling(250, min_periods=60).mean()) \\\n"
           "        / sig.rolling(250, min_periods=60).std()\n"
           "    return z.values\n")
    diag = b.dispatch("factor.evaluate", {"envId": "rb", "source": src})
    assert "ic_ir_train" in diag and not diag.get("error"), diag.get("error")
    # 因果门活体（含特征扰动）
    caus = b.dispatch("factor.check_causality", {"envId": "rb", "source": src})
    assert caus["causal"] is True


# ---------------------------------------------------------------------------
# horizon 菜单宽度按线分（ts 20 / 面板 6，2026-11-07 用户拍板 1-20 全菜单）
# ---------------------------------------------------------------------------

def test_horizon_menu_cap_line_aware(tmp_path):
    """ts_single 菜单 20 个（1-20 全集）通过；21 个拒；面板 7 个仍拒（红线）。"""
    p, _ = _write_feature_parquet(tmp_path)
    menu_20 = list(range(1, 21))
    cfg = DataConfig.from_dict({"version": 1, "environments": {
        "rb": _ts_spec(p, features=None)}})
    # 直接在 spec dict 上带 horizons 菜单（_ts_spec 不含 features 键时省略）
    env_raw = _ts_spec(p)
    env_raw["calibration"]["horizons"] = menu_20
    cfg20 = DataConfig.from_dict({"version": 1, "environments": {"rb": env_raw}})
    assert cfg20.environments["rb"].calibration["horizons"] == menu_20
    # ts 21 个 → 拒
    env_raw["calibration"]["horizons"] = list(range(1, 22))
    with pytest.raises(DataError):
        DataConfig.from_dict({"version": 1, "environments": {"rb": env_raw}})
    # 面板 7 个 → 仍拒（截面线行为零变更）
    with pytest.raises(DataError):
        DataConfig.from_dict({"version": 1, "environments": {
            "etf": {"source": {"type": "parquet", "path": str(p)},
                    "layout": "long",
                    "mapping": {"symbol": "close", "date": "date",
                                "open": "open", "high": "high", "low": "low",
                                "close": "close", "volume": "volume"},
                    "calibration": {"horizon": 1,
                                    "horizons": list(range(1, 8))}}}})
