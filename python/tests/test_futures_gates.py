# coding=utf-8
"""期货单品种 ts 线门禁与终审分派回归（PLAN-futures-ts-factor-mining-2026-11-07 §五/§八）。

全部合成数据、零网络。锁定九件事：

① ts_null（ts_block_perm_null）纯噪声 p 均匀性（KS）+ 真对齐信号 p 极小
② 噪声世界 ts 版：无预测力因子 |z|<3；前视 artifact 因子 |z|≥3 硬拒
   （管道偏差在噪声世界也系统同向——与 tests/test_noise_gate.py 的
   LOOKAHEAD 靶心案例同构造思路）
③ cs_operator_check：cs_* 源拒、干净 ts 源不拒、CS_OPERATOR_MSG 零数字
④ verify.compute_ic_series ts 分派：全面板重算、dates/values 对齐、
   同参复现、IR 与 ts_product_series 全面板口径一致
⑤ causality ts：正常 ts 因子 causal；shift(-1) 泄漏 → FUTURE_LEAK
⑥ flatness.train_ic_ir ts 分派 == series_ir(ts_product_series)
⑦ day_permutation_test ts：返回 schema 齐、无预测力信号 p_align 不小、
   真动量信号 p_align 小
⑧ ops Series 桥：全部修改过的算子 pd.Series 输入 vs 单列 DataFrame 输入
   逐位一致、返回 Series 带 index；ts_corr Series×Series 与 DataFrame
   两列路径一致
⑨ random_gen ts：ts_opset 无 cs；generate_one(require_cs=False) 生成树 →
   _eval_tree(TsEnv) 有限 (T,)；render_factor_source(series_mode=True)
   源码可 exec 且 factor(env) 输出 (T,)；run_null_calibration_ts 写
   null_landscape.json（line=ts_single + ic_ir 分位键 + env_fingerprint 透传）

面板路径锁定（任务 ⑩）由 tests/test_noise_gate.py、tests/test_day_perm.py、
tests/test_discipline.py、tests/test_param_flatness.py 承担（kind 缺省路径
零损伤）——本文件只测 ts 线分支。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.factor import (causality, flatness, hardening,  # noqa: E402
                                      noise, ops, permute, random_gen,
                                      verify)
from dsh_factor_mining.factor.env import Calibration, TsEnv  # noqa: E402
from dsh_factor_mining.factor.evaluate_ts import (limit_hit_bars,  # noqa: E402
                                                  series_ir,
                                                  ts_block_perm_null,
                                                  ts_product_series)


# ---------------------------------------------------------------------------
# 公共夹具：合成单品种日线环境
# ---------------------------------------------------------------------------
def _ts_env(T=900, drift=None, seed=7, horizon=20, limit_pct=0.06,
            limit_days=0, close2=False, mu_phi=None, mu_sig=0.0,
            day_vol=0.010) -> TsEnv:
    """合成 TsEnv：日收益 = AR(1) 漂移分量（可选 mu_phi/mu_sig）+ 恒定
    drift（可选）+ 高斯噪声（day_vol）；limit_days>0 构造等量隔立的
    涨停日（+limit_pct+1% 跳空，open 贴前收的一字近似）。"""
    rng = np.random.default_rng(seed)
    r = np.zeros(T)
    mu = 0.0
    for t in range(1, T):
        if mu_phi:
            mu = mu_phi * mu + mu_sig * rng.standard_normal()
        r[t] = mu + (drift or 0.0) + day_vol * rng.standard_normal()
    c = 4000.0 * np.cumprod(1.0 + r)
    limit_idx: list[int] = []
    if limit_days:
        limit_idx = [int(t) for t in np.linspace(30, T - 30, limit_days)]
        for t in limit_idx:
            c[t:] = c[t:] * (1.0 + limit_pct + 0.01)
    o = c * np.exp(rng.normal(0, 0.004, T))
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.002, T)))
    l = np.minimum(o, c) / (1 + np.abs(rng.normal(0, 0.002, T)))
    for t in limit_idx:                       # 停板日：一字近似（h 贴 c）
        o[t] = c[t - 1] * 1.001
        h[t] = c[t] * 1.001
        l[t] = min(o[t], c[t]) * 0.999
    v = np.exp(rng.normal(8.0, 0.5, T))
    oi = np.exp(rng.normal(11.0, 0.3, T))
    c2 = c * np.exp(rng.normal(0, 0.006, T)) if close2 else None
    dates = list(pd.bdate_range("2015-01-02", periods=T))
    return TsEnv(o, h, l, c, v, oi, dates, instrument="rb",
                 close2=c2, limit_pct=limit_pct,
                 calibration=Calibration(
                     horizon=horizon,
                     dev_end=str(dates[int(T * 0.6)].date()),
                     sel_end=str(dates[int(T * 0.8)].date())))


MOM_SRC = """
import pandas as pd

def factor(env):
    c = pd.Series(env.c)
    return (c / c.shift(20) - 1.0).values
"""

CLEAN_TS_SRC = """
import pandas as pd
from dsh_factor_mining.factor.ops import ts_zscore

def factor(env):
    return ts_zscore(pd.Series(env.c), 20).values
"""


def _mom(env):
    s = pd.Series(env.c)
    return (s / s.shift(20) - 1.0).values


def _lagged_volume(env):          # 无预测力：滞后噪声（量能变化，与价格无关联）
    s = pd.Series(env.v)
    return s.pct_change().shift(3).values


def _lookahead(env):              # artifact 靶心：明日收益当信号（shift(-1)）
    s = pd.Series(env.c)
    return (s.shift(-1) / s - 1.0).values


def _aligned(env, noise_scale=0.3, seed=7):   # 完美对齐信号（检验功效用）
    h = env.calibration.horizon
    c = pd.Series(env.c)
    f = c.shift(-h) / pd.Series(env.o).shift(-1) - 1.0
    rng = np.random.default_rng(seed)
    return (f + noise_scale * rng.normal(0, float(f.std()), len(f))).values


# ---------------------------------------------------------------------------
# ① ts_null：纯噪声 p 均匀性 + 真对齐信号功效
# ---------------------------------------------------------------------------
def test_ts_null_p_uniformity_ks():
    """30 个纯随机信号在纯噪声环境上的 ts_block_perm_null p 值 ~ U(0,1)
    （KS 检验；scipy 缺席时手写 KS 统计量对照 α=0.01 临界值）。"""
    env = _ts_env(seed=5)
    master = np.random.default_rng(99)
    ps = []
    for i in range(30):
        F = master.normal(0.0, 1.0, env.T)
        res = ts_block_perm_null(F, env, n_perm=200, seed=100 + i)
        assert res["method"] == "ts_block_perm"
        assert np.isfinite(res["p"])
        ps.append(float(res["p"]))
    ps = np.sort(np.asarray(ps))
    n = len(ps)
    # 手写 KS 统计量（与 scipy.stats.kstest 同式）
    d = max(np.max(np.arange(1, n + 1) / n - ps), np.max(ps - np.arange(0, n) / n))
    assert d < 1.63 / np.sqrt(n), f"KS D={d:.4f} 超α=0.01临界 {1.63 / np.sqrt(n):.4f}"
    try:
        from scipy.stats import kstest
        assert kstest(ps, "uniform").pvalue > 0.01
    except ImportError:
        pass


def test_ts_null_true_aligned_signal_power():
    """真对齐信号（前向收益+噪声）→ p 极小、|z|>3（块旋转破坏对齐）。"""
    env = _ts_env(T=2400, seed=5)
    F = _aligned(env)
    res = ts_block_perm_null(F, env, n_perm=200, seed=7)
    assert abs(res["z"]) > 3.0, res
    assert res["p"] < 0.01, res


# ---------------------------------------------------------------------------
# ② 噪声门 ts 版
# ---------------------------------------------------------------------------
def test_noise_world_ts_structure():
    """ts 噪声世界结构性质：正价格、合法 OHLC、口径字段原样、可复现。"""
    env = _ts_env(seed=5, close2=True, limit_days=3)
    assert int(limit_hit_bars(env).sum()) == 3     # 夹具停板日被 G-limit 识别
    w = noise.generate_noise_world(env, np.random.default_rng(7))
    assert isinstance(w, TsEnv) and w.c.shape == (env.T,)
    assert w.dates == env.dates and w.instrument == env.instrument
    assert w.limit_pct == env.limit_pct and w.calibration is env.calibration
    assert (w.c > 0).all() and (w.oi > 0).all()
    assert (w.h >= np.maximum(w.o, w.c)).all()
    assert (w.l <= np.minimum(w.o, w.l * 0 + np.minimum(w.o, w.c))).all()
    assert w.close2 is not None and w.close2.shape == w.c.shape
    w2 = noise.generate_noise_world(env, np.random.default_rng(7))
    assert np.array_equal(w.c, w2.c)               # 同 rng 逐位复现
    w3 = noise.generate_noise_world(_ts_env(seed=5), np.random.default_rng(7))
    assert w3.close2 is None                       # 原 close2 None → None


def test_noise_gate_ts_clean_factor_passes():
    """无预测力因子（滞后量能噪声）：跨噪声世界 z 不显著（|z|<3）。"""
    env = _ts_env(seed=5)
    out = noise.noise_test(_lagged_volume, env, m=12, base_seed=11)
    assert out["artifact"] is False, out
    assert abs(out["z"]) < 3.0, out


def test_noise_gate_ts_artifact_rejected():
    """前视构造因子在噪声世界也系统性同向 → |z|≥3 硬拒（门靶心案例）。"""
    env = _ts_env(seed=5)
    out = noise.noise_test(_lookahead, env, m=15, base_seed=11)
    assert out["artifact"] is True, out
    assert abs(out["z"]) >= 3.0, out


# ---------------------------------------------------------------------------
# ③ cs_operator_check
# ---------------------------------------------------------------------------
def test_cs_operator_check_rejects_cs_operators():
    bad = ("from dsh_factor_mining.factor.ops import cs_rank, ts_zscore\n"
           "import pandas as pd\n"
           "def factor(env):\n"
           "    return cs_rank(ts_zscore(pd.Series(env.c), 20)).values\n")
    r = hardening.cs_operator_check(bad)
    assert r["rejected"] is True and "cs_rank" in r["hits"]

    bad_attr = ("import pandas as pd\n"
                "import dsh_factor_mining.factor.ops as ops\n"
                "def factor(env):\n"
                "    return ops.cs_zscore(pd.Series(env.c)).values\n")
    r2 = hardening.cs_operator_check(bad_attr)
    assert r2["rejected"] is True and "cs_zscore" in r2["hits"]


def test_cs_operator_check_clean_ts_source_passes():
    r = hardening.cs_operator_check(CLEAN_TS_SRC)
    assert r["rejected"] is False and r["hits"] == []
    assert "ts_zscore" not in r["hits"]           # ts 算子合法
    # 文案自足且零数字
    assert not any(ch.isdigit() for ch in hardening.CS_OPERATOR_MSG)
    assert "cs" in hardening.CS_OPERATOR_MSG


# ---------------------------------------------------------------------------
# ④ verify.compute_ic_series ts 分派
# ---------------------------------------------------------------------------
def test_verify_compute_ic_series_ts():
    env = _ts_env(seed=5)
    d1, v1 = verify.compute_ic_series(MOM_SRC, env, None)
    d2, v2 = verify.compute_ic_series(MOM_SRC, env, None)
    assert (d1, v1) == (d2, v2)                   # 同参复现
    assert len(d1) == len(v1) and len(d1) >= 5
    assert all(isinstance(x, str) for x in d1)
    # 与 ts_product_series 全面板口径一致（IR 相等）
    ns = {}
    exec(compile(MOM_SRC, "<factor>", "exec"), ns)
    F = np.asarray(ns["factor"](env), dtype=np.float64)
    s = ts_product_series(F, env, None)
    assert d1 == [str(x.date()) for x in s.index]
    got_ir = float(np.mean(v1)) / float(np.std(v1, ddof=1))
    assert abs(got_ir - series_ir(s)) < 1e-12


def test_verify_compute_ic_series_ts_shape_guard():
    """(T,) 契约破坏 → SegmentsError（fail-closed，不静默）。"""
    from dsh_factor_mining.factor.verify import SegmentsError
    env = _ts_env(T=300, seed=5)
    two_d = ("import pandas as pd\n"
             "def factor(env):\n"
             "    return pd.DataFrame({'a': env.c, 'b': env.o}).values\n")
    with pytest.raises(SegmentsError):
        verify.compute_ic_series(two_d, env, None)


# ---------------------------------------------------------------------------
# ⑤ causality ts
# ---------------------------------------------------------------------------
def test_causality_ts_causal_factor_passes():
    env = _ts_env(seed=5)
    r = causality.check_causality(_mom, env)
    assert r["verdict"] == "causal", r
    r2 = causality.check_causality_nan(_mom, env)
    assert r2["verdict"] == "causal", r2


def test_causality_ts_future_leak_detected():
    env = _ts_env(seed=5)
    r = causality.check_causality(_lookahead, env)
    assert r["verdict"] == "FUTURE_LEAK", r
    r2 = causality.check_causality_nan(_lookahead, env)
    assert r2["verdict"] == "FUTURE_LEAK", r2


# ---------------------------------------------------------------------------
# ⑥ flatness ts 分派
# ---------------------------------------------------------------------------
def test_flatness_train_ic_ir_ts_dispatch():
    env = _ts_env(seed=5, mu_phi=0.99, mu_sig=0.0015)
    F = _mom(env)
    got = flatness.train_ic_ir(F, env)
    want = series_ir(ts_product_series(F, env, end_date=env.calibration.dev_end))
    assert isinstance(got, float) and np.isfinite(got)
    assert got == want                            # 同口径逐位一致


# ---------------------------------------------------------------------------
# ⑦ day_permutation_test ts
# ---------------------------------------------------------------------------
def test_day_perm_ts_schema_and_directions():
    env = _ts_env(T=900, seed=5, mu_phi=0.99, mu_sig=0.0015)
    r = permute.day_permutation_test(_mom, env, m=100, base_seed=0)
    for k in ("observed", "null_mean", "null_q5", "null_q50", "null_q95",
              "p_align", "p_two", "alignment_dependent", "n_valid", "m"):
        assert k in r, k
    assert r["n_valid"] >= 50 and r["observed"] is not None
    # 真动量信号：对齐携带增量 → p_align 小
    assert r["p_align"] < 0.05, r
    # 无预测力信号：任意配对无差异 → p_align 不小
    r0 = permute.day_permutation_test(_lagged_volume, env, m=100, base_seed=0)
    assert r0["p_align"] > 0.05, r0
    assert not r0["alignment_dependent"], r0


# ---------------------------------------------------------------------------
# ⑧ ops Series 桥（全部修改过的算子）
# ---------------------------------------------------------------------------
_BRIDGED_TS_1 = ["ts_slope", "ts_decay_linear", "ts_decay_exp", "ts_arg_max",
                 "ts_arg_min", "ts_winsor", "ts_streak"]
_BRIDGED_TS_2 = ["ts_corr", "ts_cov", "ts_vol_ratio", "ts_resid", "ts_beta",
                 "ts_wmean", "ts_rank_corr", "ts_coskew"]
_BRIDGED_BIN = ["add", "sub", "mul", "div", "minv", "maxv", "signed_diff",
                "sfdiff"]
_BRIDGED_UNARY = ["sigmoid", "tanhx"]
_BRIDGED_CMP = ["gt", "lt"]


@pytest.mark.parametrize("name", _BRIDGED_TS_1 + _BRIDGED_TS_2 + _BRIDGED_BIN
                         + _BRIDGED_UNARY + _BRIDGED_CMP)
def test_ops_series_bridge_parity(name):
    """Series 输入 vs 单列 DataFrame 输入：值 allclose、返回 Series 带 index。"""
    rng = np.random.default_rng(3)
    idx = pd.bdate_range("2024-01-02", periods=60)
    a = pd.Series(100.0 + rng.normal(0, 5, 60).cumsum(), index=idx)
    b = pd.Series(50.0 + rng.normal(0, 3, 60).cumsum(), index=idx)
    da = a.to_frame("__s__")
    db = b.to_frame("__s__")
    w = 10
    if name in _BRIDGED_TS_1:
        got, want = getattr(ops, name)(a, w), getattr(ops, name)(da, w)
    elif name in _BRIDGED_TS_2:
        got, want = getattr(ops, name)(a, b, w), getattr(ops, name)(da, db, w)
    elif name in _BRIDGED_BIN:
        got, want = getattr(ops, name)(a, b), getattr(ops, name)(da, db)
    elif name in _BRIDGED_CMP:
        got, want = getattr(ops, name)(a, b), getattr(ops, name)(da, db)
    elif name in _BRIDGED_UNARY:
        got, want = getattr(ops, name)(a), getattr(ops, name)(da)
    else:
        raise AssertionError(name)
    assert isinstance(got, pd.Series), (name, type(got))
    assert got.index.equals(idx), name
    assert np.allclose(got.values, want["__s__"].values, equal_nan=True), name


def test_ops_series_bridge_triple_and_where():
    rng = np.random.default_rng(4)
    idx = pd.bdate_range("2024-01-02", periods=60)
    a = pd.Series(rng.normal(0, 1, 60), index=idx)
    b = pd.Series(rng.normal(0, 1, 60), index=idx)
    c = pd.Series(rng.normal(0, 1, 60), index=idx)
    da, db, dc = a.to_frame("__s__"), b.to_frame("__s__"), c.to_frame("__s__")
    got = ops.ts_triple_corr(a, b, c, 10)
    want = ops.ts_triple_corr(da, db, dc, 10)
    assert isinstance(got, pd.Series) and got.index.equals(idx)
    assert np.allclose(got.values, want["__s__"].values, equal_nan=True)
    cond = ops.gt(a, b)
    gw = ops.where(cond, a, b)
    ww = ops.where(ops.gt(da, db), da, db)
    assert isinstance(gw, pd.Series) and gw.index.equals(idx)
    assert np.allclose(gw.values, ww["__s__"].values, equal_nan=True)


def test_ops_series_bridge_mixed_returns_dataframe():
    """Series×DataFrame 混合：不炸、返回 DataFrame（双 Series 才回 Series）。"""
    rng = np.random.default_rng(5)
    idx = pd.bdate_range("2024-01-02", periods=40)
    s = pd.Series(rng.normal(0, 1, 40), index=idx)
    df = pd.DataFrame({"a": rng.normal(0, 1, 40), "b": rng.normal(0, 1, 40)},
                      index=idx)
    out = ops.add(s, df)
    assert isinstance(out, pd.DataFrame)
    out2 = ops.ts_corr(s, df, 10)
    assert isinstance(out2, pd.DataFrame)


def test_ops_ts_corr_series_matches_two_column_frame():
    """ts_corr Series×Series 与 DataFrame 两列 rolling corr 路径一致。"""
    rng = np.random.default_rng(6)
    idx = pd.bdate_range("2024-01-02", periods=80)
    df = pd.DataFrame({"A": rng.normal(0, 1, 80), "B": rng.normal(0, 1, 80)},
                      index=idx)
    got = ops.ts_corr(df["A"], df["B"], 10)
    want = df["A"].rolling(10, min_periods=max(3, 10 // 2)).corr(df["B"])
    assert isinstance(got, pd.Series) and got.index.equals(idx)
    assert np.allclose(got.values, want.values, equal_nan=True)


# ---------------------------------------------------------------------------
# ⑨ random_gen ts
# ---------------------------------------------------------------------------
def test_ts_opset_has_no_cs_and_templates_off(tmp_path):
    opset = random_gen.ts_opset(str(tmp_path))
    names = [n for cat in opset["ops"].values() for n in cat]
    assert names and all(not n.startswith("cs_") for n in names)
    assert not opset["templates_enabled"] and opset["template_share"] == 0.0
    assert "ts" in opset["ops"] and opset["ops"]["ts"]


def test_generate_one_require_cs_false_on_ts_env():
    env = _ts_env(seed=5)
    opset = random_gen.ts_opset(None)
    leaves = random_gen.env_leaves(env)
    assert leaves == ["o", "h", "l", "c", "v", "oi"]    # 无 close2
    rng = np.random.default_rng(42)

    def _has_ts(n) -> bool:
        return n.category == "ts" or any(_has_ts(ch) for ch in n.children)

    healthy = None
    for _ in range(10):
        tree, tpl = random_gen.generate_one(rng, opset, leaves=leaves,
                                            require_cs=False)
        assert tpl is None                                    # 模板已关
        assert _has_ts(tree)                                  # 仍强制 ≥1 ts_*
        try:
            val = random_gen._eval_tree(tree, env)
        except Exception:
            continue   # 既有 ts_triple_corr 子树数缺陷：产线同款跳过
        assert isinstance(val, pd.Series) and len(val) == env.T
        arr = np.asarray(val, dtype=np.float64)
        healthy = tree if np.isfinite(arr[env.T // 2:]).mean() > 0.5 else healthy
        if healthy is not None:
            break
    assert healthy is not None, "10 棵树全部病态（异常）"


def test_render_series_mode_exec_and_null_calibration_ts(tmp_path):
    env = _ts_env(seed=5, close2=True)
    assert random_gen.env_leaves(env) == ["o", "h", "l", "c", "v", "oi",
                                          "close2"]
    opset = random_gen.ts_opset(None)
    rng = np.random.default_rng(9)
    rendered = None
    for _ in range(10):
        tree, _ = random_gen.generate_one(rng, opset,
                                          leaves=random_gen.env_leaves(env),
                                          require_cs=False)
        src, used = random_gen.render_factor_source(tree, "t_ts",
                                                    series_mode=True)
        try:
            ns = {}
            exec(compile(src, "<factor>", "exec"), ns)
            out = ns["factor"](env)
        except Exception:
            # 既有树生成器缺陷照产线口径跳过：THREE_INPUT_TS（ts_triple_corr）
            # 历史上只生成 2 个子树（缺 1 参），面板 null 校准对这类树
            # except-continue 静默跳过——本测试沿用同行为（不改面板采样）。
            continue
        assert out.shape == (env.T,)
        if np.isfinite(np.asarray(out, dtype=np.float64)[env.T // 2:]).mean() > 0.5:
            rendered = (src, used)
            break
    assert rendered is not None
    src, used = rendered
    assert used and all(n in dir(ops) for n in used)

    land = random_gen.run_null_calibration_ts(
        env, str(tmp_path), n=30, seed=42, env_fingerprint="fp-ts-test")
    assert land["line"] == "ts_single"
    assert land["env_fingerprint"] == "fp-ts-test"
    assert land["n_generated"] == 30 and land["n_valid"] >= 5
    assert land["spread"] == {} and land["a4_dd"] is None
    h = str(env.calibration.horizon)
    assert h in land["ic_ir"]
    for k in ("p10", "p25", "p50", "p75", "p90", "p95", "p99", "max",
              "mean_abs", "n_valid"):
        assert k in land["ic_ir"][h], k
    p = tmp_path / "null_landscape.json"
    assert p.exists()
    disk = json.loads(p.read_text(encoding="utf-8"))
    assert disk == land                                       # 落盘逐位一致

    F = _mom(env)
    scan = random_gen.light_ic_scan_ts(F, env)
    assert scan["line"] == "ts_single" and scan["n"] >= 5
    assert scan["ic_ir"] is None or np.isfinite(scan["ic_ir"])


def test_run_null_calibration_ts_cross_horizon(tmp_path):
    """多 horizon：逐 horizon ic_ir 段 + 跨 horizon 相关键（<30 共同样本
    记 None）。"""
    env = _ts_env(T=600, seed=5)
    land = random_gen.run_null_calibration_ts(env, str(tmp_path), n=20,
                                              seed=3, horizons=[5, 20])
    assert land["horizons"] == [5, 20]
    assert "5" in land["ic_ir"] and "20" in land["ic_ir"]
    assert land["cross_horizon_corr"] == {"5|20": None}       # 20<30 共同样本
