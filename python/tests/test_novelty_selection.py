# -*- coding: utf-8 -*-
"""P0a 反同质化选种(2026-09-13)— _novelty_greedy 单元 + explore 全链。

标定依据:生产端 23/20 个 farm 周期反事实(λ=0.3 → 选种内两两|ρ| 降
54%/40%,IC 牺牲 10%/7%);设计锚 REPORT-p0-estimation-2026-09-13。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.bridge import Bridge, _novelty_corr, _novelty_greedy


# ---- 单元:贪心的多样性/质量权衡 ----

def test_novelty_corr_basics():
    rng = np.random.default_rng(1)
    a = list(rng.normal(0, 1, 40))
    b = [0.9 * x for x in a]
    assert _novelty_corr(a, b) > 0.8
    c = list(rng.normal(0, 1, 40))
    assert abs(_novelty_corr(a, c)) < 0.5
    assert _novelty_corr(a, a[:15]) is None  # 短序列 → None(独立计)


def test_greedy_prefers_independent_over_cluster_second():
    """族高分同质 + 独立候选:第二席应给独立的(rank 差 < λ 时;λ=0.5
    演示机制——位置 rank 在小样本极化,生产 24-83 候选下 rank 差细密)。"""
    rng = np.random.default_rng(2)
    base = list(rng.normal(0, 1, 60))
    cands = [
        {"name": "f1", "score": 0.95, "series": base},
        {"name": "f2", "score": 0.90, "series": [0.95 * x for x in base]},  # 同族
        {"name": "i4", "score": 0.85, "series": list(rng.normal(0, 1, 60))},
        {"name": "f3", "score": 0.80, "series": [0.9 * x for x in base]},   # 同族
        {"name": "i5", "score": 0.75, "series": list(rng.normal(0, 1, 60))},
    ]
    chosen, inner = _novelty_greedy(cands, lambda c: c["score"],
                                    lambda c: c["series"], 2, [], lam=0.5)
    names = [c["name"] for c in chosen]
    assert names[0] == "f1" and names[1] == "i4", names   # 首席=最高分;次席=独立
    assert inner is not None and inner < 0.5
    # λ=0:纯 argmax 回旧行为(f1,f2)
    chosen0, _ = _novelty_greedy(cands, lambda c: c["score"],
                                 lambda c: c["series"], 2, [], lam=0.0)
    assert [c["name"] for c in chosen0] == ["f1", "f2"]


def test_greedy_history_reference_avoids_cross_cycle_attractor():
    """历史参照(上周期已选)把同族挡在次席外——跨周期防吸引子。"""
    rng = np.random.default_rng(3)
    base = list(rng.normal(0, 1, 60))
    cands = [
        {"name": "x1", "score": 0.95, "series": base},                      # 与历史同族(首席仍可)
        {"name": "x2", "score": 0.90, "series": [0.92 * x for x in base]},  # 与历史同族
        {"name": "x3", "score": 0.80, "series": list(rng.normal(0, 1, 60))},
        {"name": "x4", "score": 0.70, "series": list(rng.normal(0, 1, 60))},
    ]
    chosen, _ = _novelty_greedy(cands, lambda c: c["score"], lambda c: c["series"],
                                2, hist=[base], lam=0.5)
    names = [c["name"] for c in chosen]
    assert names == ["x1", "x3"], names   # 次席跳过与历史同族的 x2


# ---- 全链:explore 携带 novelty 遥测与历史持久化 ----

BASELINE_SOURCE = (
    "def factor(env):\n    import pandas as pd\n"
    "    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n")


def _panel(path: Path, T=800, N=40, seed=4):
    rng = np.random.default_rng(seed)
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
    pd.DataFrame(rows).to_parquet(path)


@pytest.fixture()
def bridge(tmp_path):
    data = tmp_path / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


def test_explore_novelty_telemetry_and_seeds_persist(bridge, tmp_path):
    r1 = bridge.dispatch("factor.random_generate",
                         {"envId": "primary", "mode": "explore",
                          "n": 8, "top_k": 3})
    assert r1["top"], "explore 应有幸存者"
    assert "novelty" in r1 and r1["novelty"]["lambda"] == 0.3
    seeds = tmp_path / "state" / "explore_seeds.json"
    assert seeds.exists()
    sd = json.loads(seeds.read_text(encoding="utf-8"))
    assert sd["primary"], "选种历史应已持久化"
    assert all(len(s) <= 200 for s in sd["primary"])
    # 候选携带 ic_series(novelty 的原料)
    assert all("ic_series" in r["light_ic"] for r in r1["top"])


def test_explore_lambda_zero_disables(bridge):
    r = bridge.dispatch("factor.random_generate",
                        {"envId": "primary", "mode": "explore",
                         "n": 6, "top_k": 2, "novelty_lambda": 0})
    assert r["novelty"]["lambda"] == 0.0
    assert r["top"]
