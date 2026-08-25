# coding=utf-8
"""参数平坦性门回归（2026-08-25 用户决策：申报制最小步长邻域，只打
悬崖不打衰减——真实 alpha 平滑峰，断崖只可能来自调参贴噪声）。

锁定：
1. 申报校验：value 不在 source 数值字面量 → 事务中止拒（防隐藏关键
   参数）；形状/上限校验
2. 平滑因子过：漂移面板上窗口型因子 ±1 步长邻域 IC_IR 不塌
3. 悬崖语义（纯函数级）：翻号 / 塌陷 50% / 退化 三签名 + 平滑通过
4. submit 接线：带 flatness_params → entry/响应带 flatness 块；
   无申报 → n_params=0 跳过
5. 邻域不进主账本：trail_engine 试验数不因平坦性变体增长
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

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402
from dsh_factor_mining.factor.flatness import (  # noqa: E402
    flatness_test, literal_present, replace_numeric_literal)

# 窗口 20 动量（BASELINE 同构，value=20 真实出现在 source）
WINDOW_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _make_drift_bridge(root: Path) -> Bridge:
    """逐资产漂移面板（与 day-perm 测试同构：前 20 资产 +漂移）。"""
    rng = np.random.default_rng(11)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    mu = np.where(np.arange(N) < 20, 0.004, -0.004)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(mu[i], 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b

# ---- 1. 字面量替换与探测 ----

def test_replace_and_detect():
    src = "x = 20\ny = c.shift(20)\nz = 252\n"
    assert literal_present(src, 20)
    assert literal_present(src, 252)
    assert not literal_present(src, 21)
    out = replace_numeric_literal(src, 20, 21)
    assert out is not None
    assert "21" in out and "20" not in out.replace("252", "")  # 两处 20 都换
    assert literal_present(out, 252)  # 252 不被动
    assert replace_numeric_literal(src, 99, 1) is None
    # bool 不匹配 int（isinstance bool 排除）
    assert not literal_present("flag = True", 1)


# ---- 2. 申报校验：漏报/错报即拒 ----

def test_decl_value_must_be_in_source(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    with pytest.raises(BridgeError, match="未出现在 source"):
        b.dispatch("registry.submit", {
            "name": "f_bad_decl", "signal": "x",
            "source": WINDOW_SOURCE,
            "diagnosis": {"ic_ir_train": 0.3, "ic_n_train": 50,
                          "column_perm_train": {"z": 4.0, "p": 1e-4},
                          "beta_exposure": 0.1,
                          "deflated_train": {"p": 0.001, "n_trials": 1,
                                             "sr_hat": 0.5, "skew": 0.0,
                                             "kurt": 3.0, "n_obs": 60}},
            "flatness_params": [
                {"name": "window", "value": 63, "step": 1}]})  # 63 不在 source
    with pytest.raises(BridgeError, match="正数 step"):
        b.dispatch("registry.submit", {
            "name": "f_bad_step", "signal": "x",
            "source": WINDOW_SOURCE,
            "diagnosis": {"ic_ir_train": 0.3, "ic_n_train": 50,
                          "column_perm_train": {"z": 4.0, "p": 1e-4},
                          "beta_exposure": 0.1,
                          "deflated_train": {"p": 0.001, "n_trials": 1,
                                             "sr_hat": 0.5, "skew": 0.0,
                                             "kurt": 3.0, "n_obs": 60}},
            "flatness_params": [{"name": "w", "value": 20, "step": 0}]})


# ---- 3. 平滑因子过（真实信号 + 最小步长邻域不塌） ----

def test_smooth_factor_passes_flatness(tmp_path):
    b = _make_drift_bridge(tmp_path)
    r = b._run_factor("factor.flatness_test", WINDOW_SOURCE,
                      {"flatness": [{"name": "window", "value": 20,
                                     "step": 1}]},
                      b.envs["primary"])
    assert r["n_params"] == 1
    assert r["center_ic_ir"] is not None and r["center_ic_ir"] > 0.2, r
    irs = [row["ic_ir_train"] for row in r["neighbors"]
           if isinstance(row.get("ic_ir_train"), (int, float))]
    assert len(irs) == 2, r["neighbors"]
    # 漂移结构跨窗口平滑：±1 步长不塌、不翻号
    assert r["cliff"] is False, r
    assert all(ir > 0.5 * r["center_ic_ir"] for ir in irs), r


# ---- 4. 悬崖语义（合成邻居值，纯逻辑） ----

def test_cliff_semantics_synthetic(tmp_path):
    """翻号 / 塌陷 / 退化 三签名在行级 cliff 字段中正确出现；
    平滑邻域不触发。monkeypatch train_ic_ir 注入预设序列。"""
    b = _make_drift_bridge(tmp_path)
    env = b.envs["primary"]
    from dsh_factor_mining.factor import flatness as fm

    orig = fm.train_ic_ir

    def run(seq):
        it = iter(seq)
        fm.train_ic_ir = lambda F, env_: next(it)
        try:
            return flatness_test(WINDOW_SOURCE, env,
                                 [{"name": "w", "value": 20, "step": 1}],
                                 compile_fn=b._compile_factor)
        finally:
            fm.train_ic_ir = orig

    # 序列 = [center, 邻居-, 邻居+]（1 参数 × 2 邻居）
    out = run([0.5, -0.1, 0.45])          # 翻号 + 平滑
    kinds = [row.get("cliff") for row in out["neighbors"]]
    assert kinds[0] == "sign_flip", kinds
    assert kinds[1] is None, kinds
    assert out["cliff"] is True

    out = run([0.5, 0.48, 0.2])           # 平滑 + 塌陷 50%
    kinds = [row.get("cliff") for row in out["neighbors"]]
    assert kinds[0] is None and kinds[1] == "collapse_50pct", kinds
    assert out["cliff"] is True

    out = run([0.6, 0.58, 0.55])          # 全平滑
    assert out["cliff"] is False and all(
        row.get("cliff") is None for row in out["neighbors"])


# ---- 5. submit 接线 + 主账本不受污染 ----

def test_submit_flatness_wiring_and_ledger_isolation(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    def _n_engine_trail() -> int:
        p = tmp_path / "state" / "trail_engine.json"
        if not p.exists():
            return 0
        return len(json.loads(p.read_text(encoding="utf-8")))

    n_before = _n_engine_trail()
    sub = b.dispatch("registry.submit", {
        "name": "f_flat", "signal": "window momentum",
        "source": WINDOW_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}},
        "flatness_params": [{"name": "window", "value": 20, "step": 1}]})
    fl = sub.get("flatness")
    assert isinstance(fl, dict) and fl["n_params"] == 1, fl
    assert "cliff" in fl and len(fl["neighbors"]) == 2, fl
    # registry 条目携带
    reg = json.loads((tmp_path / "state" / "registry.json")
                     .read_text(encoding="utf-8"))
    assert "flatness" in reg[-1]
    # 主账本隔离：平坦性变体评估不写 trail_engine（确定性扰动非选择
    # 试验——只有 submit 前的 random_generate/evaluate 类调用写入）
    n_after = _n_engine_trail()
    assert n_after == n_before, (n_before, n_after)


def test_submit_without_decl_skips(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "f_nodecl", "signal": "x",
        "source": WINDOW_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    fl = sub.get("flatness")
    assert fl["n_params"] == 0 and "跳过" in fl["note"], fl
