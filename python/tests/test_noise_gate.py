# coding=utf-8
"""过拟合层新检验回归（2026-08-24 用户设计决策）。

实审结论：column-perm 测单因子样本内截面关联、deflation 是解析式
选择运气校正——过拟合层没有蒙特卡洛检验。用户拍板硬门：
**直接看因子在随机噪声上的表现**（真 alpha 按构造不可预测噪声；
噪声上仍显著 = 因子公式在拟合评价 artifact，无条件拒收）。

本文件锁定：
1. 噪声世界生成（PIT/日历保留、价格为正、合法 OHLC）
2. 干净因子在噪声上 z 不显著（|z|<3）
3. 前视构造因子在噪声上被硬门拒收（z 爆表）——这是门的靶心案例
4. submit 集成：artifact 因子被拒且 reason 说明；干净因子通过
5. date-shift placebo：真因子 shift 后 IC 崩向 0
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

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.env import Calibration  # noqa: E402
from dsh_factor_mining.factor.noise import generate_noise_world  # noqa: E402


CLEAN_MOMENTUM = """
import numpy as pd_frame
import numpy as np

def factor(env):
    c = env.c
    mom = c / np.roll(c, 20, axis=0) - 1.0
    mom[:21] = np.nan
    return mom
"""

# 前视构造：直接读未来收盘（shift(-1)）——在真实数据和噪声数据上
# 都会系统性产生 IC。causality 检查会拦它，但若 causality 被绕过
# （缓存污染/实现漏洞），噪声硬门是第二道独立防线。
LOOKAHEAD = """
import numpy as np

def factor(env):
    c = env.c
    f = np.full_like(c, np.nan)
    f[:-1] = c[1:] / c[:-1] - 1.0  # 今天的"信号" = 明天的收益（前视）
    return f
"""


def _make_bridge(root: Path, noise_m: int = 12) -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 900, 30
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        drift = 0.0004 * ((i % 3) - 1)  # 三组漂移：正/零/负（真截面结构）
        c = 10 * np.exp(np.cumsum(rng.normal(drift, 0.012, T)))
        mom = np.full(T, 0.0)
        mom[20:] = 0.02 * ((i % 3) - 1)  # 慢因子结构：组别与未来漂移相关
        for t in range(T):
            p = float(c[t]) * float(np.exp(mom[t]))
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    state = root / "state"
    state.mkdir(exist_ok=True)
    (state / "mining_state.json").write_text(json.dumps(
        {"noise_gate_m": noise_m, "fam_conv_window": 0,
         "finalized": False}), encoding="utf-8")
    b = Bridge(state_root=str(state), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"},
                    "calibration": {"dev_end": "2021-06-01",
                                    "sel_end": "2022-01-01"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


# ---- 1. 噪声世界结构性质 ----

def test_noise_world_properties(tmp_path):
    b = _make_bridge(tmp_path)
    env = b._require_panel_env("primary")
    rng = np.random.default_rng(7)
    w = generate_noise_world(env, rng)
    assert w.c.shape == env.c.shape
    assert w.dates == env.dates and w.symbols == env.symbols
    assert np.array_equal(w.listed, env.listed)  # PIT 掩码原样保留
    vals = w.c[np.isfinite(w.c)]
    assert (vals > 0).all()  # 随机游走保证价格为正
    fin = np.isfinite(w.c)
    # 合法 OHLC：h ≥ max(o,c)，l ≤ min(o,c)
    assert (w.h[fin] >= np.maximum(w.o, w.c)[fin]).all()
    assert (w.l[fin] <= np.minimum(w.o, w.c)[fin]).all()
    # 掩码外为 NaN（可得性结构与真实一致）
    assert np.all(~np.isfinite(w.c[~w.listed]))


# ---- 2. 干净因子：噪声上不显著 ----

def test_clean_factor_passes_noise(tmp_path):
    b = _make_bridge(tmp_path)
    r = b.dispatch("factor.noise_test",
                   {"envId": "primary", "source": CLEAN_MOMENTUM,
                    "m": 20, "seed": 11})
    assert r["artifact"] is False, r
    assert abs(r["z"]) < 3.0, r


# ---- 3. 前视构造因子：噪声上被硬门击杀（门的靶心案例） ----

def test_lookahead_factor_killed_by_noise_gate(tmp_path):
    b = _make_bridge(tmp_path)
    r = b.dispatch("factor.noise_test",
                   {"envId": "primary", "source": LOOKAHEAD,
                    "m": 20, "seed": 11})
    assert r["artifact"] is True, r
    assert r["z"] > 3.0, r  # 前视因子在噪声世界也系统性 IC 为正
    assert "artifact" in r["gate"]


# ---- 4. submit 集成：噪声硬门拒收 artifact 因子 ----

def test_submit_noise_gate_rejects_lookahead(tmp_path):
    """causality 之外的独立第二防线：即使某种实现缺陷让前视因子过了
    causality 缓存，submit 的噪声硬门仍然拒收。"""
    b = _make_bridge(tmp_path)
    # 直接构造带 receipt 的合法 diagnosis（复用现有 submit 测试模式）
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    # 用干净源评估出合法诊断骨架，再把 source 换成前视（模拟 causality
    # 失守的最坏情形）——噪声门在 submit 内独立运行，必须抓住
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": CLEAN_MOMENTUM,
                       "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "lookahead_evil", "signal": "test",
        "source": LOOKAHEAD,
        "diagnosis": diag})
    assert sub["accepted"] is False, sub.get("reason")
    assert "噪声硬门" in sub["reason"], sub["reason"]
    assert sub["entry"]["noise_gate"]["artifact"] is True


# ---- 6. pw15_compD_5050 事故回归（2026-08-25 实证 L39866-40046） ----
# 事故链：慢因子（复合 ~7s/次）× 噪声门 m=50 > worker 300s 超时 →
# 旧 fail-closed 落 accepted=false 条目 → 铁律烧名 → 重试被
# 「不得重复入册」挡死。三项修复各锁一个环节。


def test_infra_failure_aborts_without_write(tmp_path, monkeypatch):
    """修复 1：噪声门基础设施失败 = 事务中止（BridgeError），registry
    零写入——不是落一条拒绝条目。重试同名字畅通。"""
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": CLEAN_MOMENTUM,
                       "stage": "development"})
    # 模拟 worker 超时：噪声门 raise
    import dsh_factor_mining.bridge as bridge_mod

    def boom(self, params):
        raise RuntimeError("worker 超时（>300s），已终止")

    monkeypatch.setattr(bridge_mod.Bridge, "_factor_noise_test", boom)
    with pytest.raises(bridge_mod.BridgeError, match="事务中止"):
        b.dispatch("registry.submit", {
            "name": "infra_victim", "signal": "t",
            "source": CLEAN_MOMENTUM, "diagnosis": diag})
    import json as _json
    reg = _json.loads((tmp_path / "state" / "registry.json").read_text(
        "utf-8")) if (tmp_path / "state" / "registry.json").exists() else []
    assert not any(e.get("name") == "infra_victim" for e in reg), \
        "基础设施失败不得写 registry（烧名事故根因）"
    # 修复后重试（恢复噪声门）同名字应畅通走到实质判定
    monkeypatch.undo()
    sub = b.dispatch("registry.submit", {
        "name": "infra_victim", "signal": "t",
        "source": CLEAN_MOMENTUM, "diagnosis": diag})
    assert "不得重复入册" not in str(sub.get("reason", "")), sub.get("reason")


def test_infra_failed_entry_healed_on_resubmit(tmp_path):
    """修复 2：已烧名字治愈——旧事故条目（accepted=false + reason 噪声
    硬门执行失败开头）在重 submit 时被清除放行。"""
    import json as _json
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    # 直接构造事故现场（模拟生产 registry 里的烧名条目）
    burned = {"name": "pw15_compD_5050", "accepted": False,
              "reason": "噪声硬门执行失败（fail-closed，可重试）："
                        "BridgeError: worker 超时（>300s），已终止",
              "source_hash": None, "diagnosis": {}}
    (state / "registry.json").write_text(
        _json.dumps([burned], ensure_ascii=False), encoding="utf-8")
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": CLEAN_MOMENTUM,
                       "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "pw15_compD_5050", "signal": "healed",
        "source": CLEAN_MOMENTUM, "diagnosis": diag})
    # 不再被铁律拦截（走到噪声门实质判定或通过）
    assert "不得重复入册" not in str(sub.get("reason", "")), sub.get("reason")
    reg = _json.loads((state / "registry.json").read_text("utf-8"))
    assert len([e for e in reg if e.get("name") == "pw15_compD_5050"]) == 1


def test_noise_budget_adaptive_truncation():
    """修复 3：预算自适应——慢因子在 budget 内截断 m（下限 10），z 仍可判。"""
    import time as _time
    from dsh_factor_mining.factor.noise import noise_test

    class _SlowEnv:
        pass

    def slow_fn(env):
        _time.sleep(0.05)
        return None  # noise_world_ic_ir 会因 fn 返回 None 抛错→ir None
    # 直接构造：让每个世界耗时 ~0.06s，budget=1.2s → 约 20 世界截断
    # 用真实小环境跑（构造 env 成本高）→ 改为直接测循环逻辑：
    # 用 monkeypatch 不便，这里用真实小面板太重；改为验证快路径不截断
    # + 逻辑单元（per-world 预算判定）通过纯数学验证
    # ——轻量方案：构造假 fn 让 noise_world_ic_ir 每次睡 0.05 后成功
    from dsh_factor_mining.factor import noise as noise_mod

    calls = {"n": 0}

    def fake_icir(fn, env, rng):
        _time.sleep(0.05)
        calls["n"] += 1
        return 0.05 * ((-1) ** calls["n"])  # 交替小值，z≈0

    orig = noise_mod.noise_world_ic_ir
    noise_mod.noise_world_ic_ir = fake_icir
    try:
        out = noise_test(lambda env: None, _SlowEnv(), m=50,
                         base_seed=1, budget_secs=1.0)
    finally:
        noise_mod.noise_world_ic_ir = orig
    assert out["n_valid"] < 50, out  # 截断发生
    assert out["n_valid"] >= 10, out  # 下限保证
    assert out["artifact"] is False, out
    assert "预算自适应截断" in out.get("note", ""), out

# ---- 5. date-shift placebo：一日记忆结构的因子时移后 IC 崩 0 ----

def test_date_shift_placebo_unit():
    """直测 _date_shift_placebo：构造 horizon=1、完美一日预测的 F——
    未时移 IC 强，时移 ≥5 后应崩向 0（时间对齐破坏）。慢结构（组别
    漂移类 regime 巧合）不适用于此断言——它们恰恰是本检验要暴露
    而非通过的结构。"""
    from dsh_factor_mining.factor.evaluate import (
        _date_shift_placebo, _forward_returns, _pit_mask,
    )
    from dsh_factor_mining.factor.env import Calibration

    rng = np.random.default_rng(9)
    T, N = 400, 40  # N 必须 > MIN_POOL(30)
    rets = rng.normal(0, 0.015, (T, N))
    c = 10 * np.cumprod(1 + rets, axis=0)
    o = c / (1 + rng.normal(0, 0.003, (T, N)))
    h = np.maximum(o, c) * 1.001
    l = np.minimum(o, c) * 0.999
    v = np.abs(rng.normal(1e5, 1e4, (T, N)))
    dates = list(pd.bdate_range("2020-01-01", periods=T))
    env = _env(o, h, l, c, v, dates, Calibration(horizon=1))
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    # 完美一日预测：今天的因子 = 明日收益的秩（时间对齐即全部信息）
    F = np.full_like(c, np.nan)
    F[:-1] = fwd[:-1]
    ds = _date_shift_placebo(F, fwd, pit, env)
    assert abs(ds["unshifted_mean_ic"]) > 0.5, ds  # 完美对齐 → IC≈1
    for k, v_ic in ds["shifted_mean_ic"].items():
        assert abs(v_ic) < 0.15, (k, v_ic, ds)  # 错位后应崩向 0


def _env(o, h, l, c, v, dates, cal):
    from dsh_factor_mining.factor.env import FactorEnv
    return FactorEnv(o, h, l, c, v, dates,
                     [f"S{i:02d}" for i in range(c.shape[1])],
                     calibration=cal)
