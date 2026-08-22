# coding=utf-8
"""v4 多重检验校正回归（2026-08-22）。

四项（session.jsonl 轨迹取证驱动）：
  1. pool_std landscape-only：真信号混入 trail 不得抬高 null 尺度
     （amihud 事故：trail_std=0.30 赢 max → sr0 膨胀 70% → 0.590 被拒）
  2. signal_detector：trail_std ≥1.5×null 报「大概率含真信号」
  3. reset 硬拦：有分量轨迹时 agent 自助 scope=mining 被拒
  4. ledger.json：submit 落盘永久账本，reset 不清除
  5. batch selection_cost：族选择成本透明化
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _make_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
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
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    return b


# ---- 1+2: pool_std landscape-only + 信号探测器 ----

def test_pool_std_not_inflated_by_signal(tmp_path):
    """核心回归：真信号撑大 trail_std 不得抬高门（v3 max 的 bug）。"""
    b = _make_bridge(tmp_path)
    # null 校准后拿 landscape 口径
    _, det = b._resolve_pool_std("primary", trail_pool_std=0.5, horizon=20)
    # pool_std 只取 landscape（合成数据下 ~0.2x），绝不被 0.5 污染
    from dsh_factor_mining.factor import random_gen
    land = random_gen.read_null_landscape(b.state_root)
    land_std = b._landscape_pool_std(land, "primary", 20)
    assert land_std is not None and 0 < land_std < 0.5
    # 探测器：trail_std=0.5 远超 null → 报真信号
    assert det is not None
    assert det["trail_std"] == 0.5
    assert det["ratio"] > 1.5
    assert "真信号" in det["signal_likelihood"]


def test_pool_std_detector_quiet_when_clean(tmp_path):
    """trail_std ≈ null 宽度 → 探测器不报真信号（正常探索）。"""
    b = _make_bridge(tmp_path)
    # 用 landscape 自身宽度作为 trail_std → ratio≈1，无真信号提示
    from dsh_factor_mining.factor import random_gen
    land = random_gen.read_null_landscape(b.state_root)
    land_std = b._landscape_pool_std(land, "primary", 20)
    assert land_std is not None
    _, det = b._resolve_pool_std("primary", trail_pool_std=land_std, horizon=20)
    assert det is not None
    assert abs(det["ratio"] - 1.0) < 0.05
    assert "真信号" not in (det.get("signal_likelihood") or "")


# ---- 3: reset 硬拦 ----

def test_reset_hard_block_with_heavy_trail(tmp_path):
    """50+ 条试验轨迹时，agent 自助 scope=mining 必须被拒。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    # 直写 60 条 trail（事故现场：有分量的搜索史）
    entries = [{"ts": f"2026-08-22T00:00:{i:02d}", "envId": "primary",
                "source_hash": f"h{i:03d}", "stage": "development",
                "horizon": 20, "ic_ir": 0.1,
                "ic_series_sketch": [0.1] * 40}
               for i in range(60)]
    (state / "trail_engine.json").write_text(json.dumps(entries), encoding="utf-8")
    try:
        b.dispatch("state.reset", {"scope": "mining"})
        raise AssertionError("60 条轨迹的自助 reset 未被拒——作弊路径开放")
    except BridgeError as e:
        assert e.code == -32003, e.code
        assert "搜索史不可由 agent 单方面抹除" in str(e)
    # 用户显式授权（confirm + reason）→ 放行
    res = b.dispatch("state.reset", {"scope": "mining", "confirm": True,
                                     "reason": "换数据集开新研究（用户授权）"})
    assert res["ok"] is True


def test_reset_allows_light_trail_without_confirm(tmp_path):
    """轻量轨迹（<50 条且 registry 空）→ 无需 confirm（正常冷启动清理）。"""
    b = _make_bridge(tmp_path)
    res = b.dispatch("state.reset", {"scope": "mining"})
    assert res["ok"] is True


def test_reset_blocked_when_registry_nonempty(tmp_path):
    """registry 非空时即使 trail 轻，自助 reset 也被拒（防洗账重登）。"""
    b = _make_bridge(tmp_path)
    b.dispatch("registry.submit", {
        "name": "reg_a", "signal": "x", "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    try:
        b.dispatch("state.reset", {"scope": "mining"})
        raise AssertionError("registry 非空时自助 reset 未被拒")
    except BridgeError as e:
        assert e.code == -32003


# ---- 4: ledger ----

def test_ledger_permanent_and_survives_reset(tmp_path):
    """submit 落 ledger.json；scope=mining reset 不清除（永久审计）。"""
    b = _make_bridge(tmp_path)
    b.dispatch("registry.submit", {
        "name": "led_a", "signal": "x", "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    ledger_path = tmp_path / "state" / "ledger.json"
    assert ledger_path.exists(), "submit 未写 ledger"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger) == 1 and ledger[0]["name"] == "led_a"
    assert "bar_sigma" in ledger[0] and "p" in ledger[0]
    # 轻量 reset（轨迹轻、registry 非空需 confirm）
    b.dispatch("state.reset", {"scope": "mining", "confirm": True,
                               "reason": "测试 ledger 存活性"})
    ledger2 = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert len(ledger2) == 1, "reset 清掉了 ledger——账本必须永久"


# ---- 5: batch selection_cost 透明度 ----

def test_batch_selection_cost_visible(tmp_path):
    """batch 响应带 selection_cost：K 选 1 的选择价格明示。"""
    b = _make_bridge(tmp_path)
    res = b.dispatch("factor.evaluate_batch", {
        "envId": "primary", "horizon": 20,
        "sources": {"v1": BASELINE_SOURCE,
                    "v2": BASELINE_SOURCE.replace("shift(20)", "shift(21)")}})
    sc = res.get("batch", {}).get("selection_cost")
    assert sc is not None, "batch 响应缺 selection_cost"
    assert "delta_sigma" in sc and "note" in sc
    # 数字自洽：global_after ≥ batch_family_bar（包络单调）
    assert sc["global_bar_after"] >= sc["batch_family_bar"] - 1e-9
