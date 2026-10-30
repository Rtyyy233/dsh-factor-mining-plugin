# -*- coding: utf-8 -*-
"""方案B 批次1(影子分层)+ 批次0(候补线视图)— 2026-09-13。

批次1:registered_ledger 台账落盘(时机+复杂度)/影子双价(门不变)。
批次0:standby.view 派生视图(谓词/去重/层标签/排除已入册)。
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.bridge import Bridge

SRC_A = ("def factor(env):\n    import pandas as pd\n"
         "    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n")
SRC_B = ("def factor(env):\n    import pandas as pd\n"
         "    v = pd.DataFrame(env.v)\n    return -v.pct_change(5).values\n")


def _panel(path: Path, T=1200, N=40, seed=4):
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
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 6})
    return b


# ---- 批次1:台账 + 影子双价 ----

def test_registered_ledger_written_on_mirror(bridge, tmp_path):
    b = bridge
    b.dispatch("journal.append", {
        "content": "## H-1 registered 预期测试\n预期:IC为正",
        "source": SRC_A})
    r = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC_A,
                                       "stage": "development", "async": False})
    assert "journal_mirror" in r
    lp = tmp_path / "state" / "registered_ledger.jsonl"
    assert lp.exists()
    rows = [json.loads(x) for x in lp.read_text(encoding="utf-8").splitlines() if x]
    assert len(rows) == 1
    row = rows[0]
    assert row["journal_id"] == "H-1" and row["ops"] >= 2
    assert row["ts_registered"] and row["ts_registered"] <= row["ts_eval"], row
    # 时机证据:journal 里的引擎 ts 章 ≤ eval
    live = b.dispatch("journal.read", {"level": 1})["live"]
    assert "ts:" in live


def test_shadow_stratum_price_on_registered_only(bridge):
    b = bridge
    # 注册 A → evaluate:A 带影子机制层价;B 未注册 → 无影子字段
    b.dispatch("journal.append", {
        "content": "## H-2 registered A\n预期:x", "source": SRC_A})
    ra = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC_A,
                                        "stage": "development", "async": False})
    dp = ra["deflated_train"]
    assert "shadow_stratum" in dp, dp
    sh = dp["shadow_stratum"]
    assert sh["alpha_layer"] == 0.0253 and sh["n_trials_mechanism"] >= 1
    assert sh["bar_sigma_mechanism"] <= dp["bar_sigma"] + 1e-9  # 子集 → 更便宜
    rb = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC_B,
                                        "stage": "development", "async": False})
    assert "shadow_stratum" not in rb["deflated_train"]
    # 门不变:verdict/red_flags 语义与全局口径一致(影子只报不判)
    assert ra.get("verdict") in ("pass", "fail", "needs_review")


def test_ledger_never_in_reset_scopes():
    import dsh_factor_mining.bridge as bm
    all_targets = [f for files in bm.Bridge.RESET_TARGETS.values() for f in files]
    assert "registered_ledger.jsonl" not in all_targets
    assert "journal" not in all_targets


# ---- 批次0:候补线视图 ----

def _mk_entry(h, sr, horizon=20, origin="hypothesis", sketch=None, stage="development"):
    return {"source_hash": h, "stage": stage, "horizon": horizon,
            "origin": origin, "ts": "2026-09-13T10:00:00",
            "ic_series_sketch": sketch,
            "dsr_stats": {"sr_hat": sr, "skew": 0.0, "kurt": 3.0, "n_obs": 200}}


def test_standby_view_filters_dedup_and_tags(tmp_path):
    st = tmp_path / "state"
    st.mkdir()
    rng = np.random.default_rng(7)
    base = list(rng.normal(0, 1, 84))
    entries = [
        _mk_entry("aaa1", 1.10, sketch=base),                       # 候补(强)
        _mk_entry("bbb2", 0.90, sketch=[0.95 * x for x in base]),   # 同族 → 去重掉
        _mk_entry("ccc3", 0.85, sketch=list(rng.normal(0, 1, 84))),  # 候补(独立)
        _mk_entry("ddd4", 0.50, sketch=list(rng.normal(0, 1, 84))),  # 弱 → 谓词外
        _mk_entry("eee5", 0.95, sketch=list(rng.normal(0, 1, 84))),  # 已入册 → 排除
        _mk_entry("fff6", 1.05, origin="random",
                  stage="batch", sketch=list(rng.normal(0, 1, 84))),  # batch 也收
        _mk_entry("ggg7", 1.02, sketch=list(rng.normal(0, 1, 84))),  # 台账注册 → mechanism
    ]
    (st / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    (st / "registry.json").write_text(json.dumps(
        [{"name": "x", "accepted": True, "source_hash": "eee5"}]), encoding="utf-8")
    (st / "registered_ledger.jsonl").write_text(
        json.dumps({"ts_registered": "2026-09-13T09:00:00",
                    "ts_eval": "2026-09-13T09:05:00",
                    "source_hash": "ggg7", "ops": 3, "depth": 2}), encoding="utf-8")

    from dsh_factor_mining.standby import standby_view
    v = standby_view(st)
    hashes = [r["hash"] for r in v["rows"]]
    assert v["count_raw"] == 5                      # eee5/ddd4 出局
    assert "bbb2" not in hashes and "eee5" not in hashes and "ddd4" not in hashes
    assert set(hashes) == {"aaa1", "ccc3", "fff6", "ggg7"}
    tags = {r["hash"]: r["stratum"] for r in v["rows"]}
    assert tags["ggg7"] == "mechanism" and tags["aaa1"] == "mechanical"
    assert v["count_family_dedup"] == 4
    # 排序:|sr| 降序
    assert [r["abs_sr"] for r in v["rows"]] == sorted(
        [r["abs_sr"] for r in v["rows"]], reverse=True)


def test_standby_view_rpc(bridge):
    r = bridge.dispatch("standby.view", {})
    assert r["exists"] is False and r["rows"] == []
