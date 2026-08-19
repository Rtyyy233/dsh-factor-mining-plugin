# coding=utf-8
"""Bridge dispatch smoke test on synthetic long-format data.

Run: PYTHONPATH=src python tests/test_bridge_smoke.py
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

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _synthetic_long(T=1200, N=40, seed=4, start="2019-01-02"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            price = max(float(c[t]), 0.5)
            rows.append({
                "eob": dates[t], "symbol": f"S{i:02d}",
                "open": price * 1.001, "high": price * 1.01, "low": price * 0.99,
                "close": price, "volume": float(rng.integers(100, 10000)),
                "amount": float(rng.integers(1000, 100000)),
            })
    return pd.DataFrame(rows)


def _make_bridge(root: Path, T=1200, N=40, seed=4, start="2019-01-02") -> tuple[Bridge, Path]:
    """构造一个临时 bridge（合成数据 + 独立 state_root）。"""
    df = _synthetic_long(T=T, N=N, seed=seed, start=start)
    data_path = root / "panel.parquet"
    df.to_parquet(data_path)
    config = {
        "version": 1,
        "stateRoot": str(root / "state"),
        "environments": {
            "primary": {
                "label": "synthetic", "kind": "panel",
                "source": {"type": "parquet", "path": str(data_path), "options": {}},
                "layout": "long",
                "mapping": {
                    "symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "volume", "amount": "amount",
                },
                "constraints": {"minSymbols": 10, "minDates": 100,
                                "requireFiniteOhlcv": True, "allowZeroVolume": True},
            }
        },
    }
    config_path = root / "factor-mining.config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    bridge = Bridge(state_root=str(root / "state"), data_config_path=str(config_path))
    return bridge, data_path


def _run_bridge_smoke():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bridge, data_path = _make_bridge(root)

        assert bridge.dispatch("ping", {})["pong"] is True
        status = bridge.dispatch("status", {})
        assert status["dataConfigured"] is True
        # worker 隔离是默认执行模式（DESIGN §10）
        assert status["executionMode"] == "worker", status

        loaded = bridge.dispatch("data.load", {"envId": "primary"})
        assert loaded["T"] == 1200 and loaded["N"] == 40, loaded

        causal = bridge.dispatch("factor.check_causality",
                                 {"envId": "primary", "source": BASELINE_SOURCE})
        assert causal["verdict"] == "causal", causal

        diag = bridge.dispatch("factor.evaluate",
                               {"envId": "primary", "source": BASELINE_SOURCE, "stage": "development"})
        assert "ic_ir_train" in diag and "column_perm_train" in diag, diag
        assert np.isfinite(diag["ic_ir_train"]), diag

        bridge.dispatch("paths.append", {"layer": "trail",
                                         "entry": {"round": 1, "signal": "baseline",
                                                   "attribution": "smoke",
                                                   "next_hypothesis": "none",
                                                   "new_information": "baseline momentum only"}})
        hits = bridge.dispatch("paths.query", {"layer": "explored", "query": "smoke"})
        assert hits["layer"] == "explored"

        lib = bridge.dispatch("library.query", {"query": "momentum"})
        assert lib["configured"] is False and lib["hits"] == []

        # worker 运行临时目录不应残留（每次调用后清理）
        worker_runs = Path(bridge.state_root) / "worker_runs"
        if worker_runs.exists():
            leftovers = [p for p in worker_runs.iterdir() if p.is_dir()]
            assert leftovers == [], f"worker_runs 残留 {len(leftovers)} 个临时目录"

        return {"status": status["environments"], "ic_ir_train": diag["ic_ir_train"],
                "column_perm_z": diag["column_perm_train"]["z"]}


def test_bridge_dispatch_end_to_end():
    """pytest 入口：完整 bridge 链路（worker 隔离模式）。"""
    summary = _run_bridge_smoke()
    assert summary["status"], "应有至少一个环境"
    assert np.isfinite(summary["ic_ir_train"]), "ic_ir_train 应有限"


def test_bridge_in_process_mode():
    """pytest 入口：in_process 调试模式仍可用（不 spawn 子进程）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        df = _synthetic_long()
        data_path = root / "panel.parquet"
        df.to_parquet(data_path)
        config = {
            "version": 1,
            "stateRoot": str(root / "state"),
            "environments": {
                "primary": {
                    "label": "synthetic", "kind": "panel",
                    "source": {"type": "parquet", "path": str(data_path), "options": {}},
                    "layout": "long",
                    "mapping": {
                        "symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                        "low": "low", "close": "close", "volume": "volume", "amount": "amount",
                    },
                    "constraints": {"minSymbols": 10, "minDates": 100,
                                    "requireFiniteOhlcv": True, "allowZeroVolume": True},
                }
            },
        }
        config_path = root / "factor-mining.config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        bridge = Bridge(state_root=str(root / "state"), data_config_path=str(config_path),
                        execution_mode="in_process")
        causal = bridge.dispatch("factor.check_causality",
                                 {"envId": "primary", "source": BASELINE_SOURCE})
        assert causal["verdict"] == "causal", causal
        diag = bridge.dispatch("factor.evaluate",
                               {"envId": "primary", "source": BASELINE_SOURCE, "stage": "development"})
        assert np.isfinite(diag["ic_ir_train"]), diag


def test_registry_submit_decoupled():
    """pytest 入口：registry_submit 不执行 factor/环境，只收诊断对象落盘。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bridge, _ = _make_bridge(root)
        diagnosis = {"ic_ir_train": 0.25, "column_perm_train": {"z": 4.0, "p": 0.0001},
                     "beta_exposure": 0.1, "ic_n_train": 50}
        res = bridge.dispatch("registry.submit", {
            "name": "candidate_a", "signal": "momentum20",
            "diagnosis": diagnosis, "source": BASELINE_SOURCE,
        })
        assert res["accepted"] is True, res
        got = bridge.dispatch("registry.get", {})
        assert len(got["registry"]) == 1
        assert got["registry"][0]["name"] == "candidate_a"
        # 缺 diagnosis 应报错（不静默执行 factor）
        try:
            bridge.dispatch("registry.submit", {"name": "bad"})
            raise AssertionError("缺 diagnosis 应报错")
        except BridgeError as e:
            assert e.code == -32602, e.code


def test_bridge_test_stage_consumes_lock():
    """pytest 入口：test stage 一次性锁（evaluate_test 消费后 raise，跨 worker 子进程可见）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # 早起点 + 长周期，让 test 区 [2024-01-01, end) 有足够信号日样本
        bridge, _ = _make_bridge(root, T=2600, start="2015-01-05")
        r1 = bridge.dispatch("factor.evaluate",
                             {"envId": "primary", "source": BASELINE_SOURCE, "stage": "test"})
        assert r1.get("region") == "test", r1
        try:
            bridge.dispatch("factor.evaluate",
                            {"envId": "primary", "source": BASELINE_SOURCE, "stage": "test"})
            raise AssertionError("第二次 test 评估应 raise（test_lock 一次性）")
        except BridgeError as e:
            assert e.code == -32003, e.code
            assert "test 已被消费" in e.message, e.message


def main():
    try:
        summary = _run_bridge_smoke()
        print("BRIDGE_SMOKE PASS")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"BRIDGE_SMOKE FAIL: {e!r}")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
