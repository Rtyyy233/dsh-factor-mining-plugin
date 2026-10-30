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


def _poll_evaluate(bridge, submit, timeout_s: float = 120.0):
    """层 0c（2026-08-31）：worker 模式 evaluate 默认异步——提交返回 job_id，
    轮询直到 diagnosis/异常（缓存命中会同步直接返回 diagnosis，两种都收）。"""
    import time as _t
    deadline = _t.monotonic() + timeout_s
    if "job_id" not in submit:
        return submit  # 缓存命中（R2 契约分叉的同步侧）
    while _t.monotonic() < deadline:
        r = bridge.dispatch("factor.evaluate",
                            {"envId": "primary", "job_id": submit["job_id"]})
        if r.get("status") in ("running", "queued", "submitted"):
            _t.sleep(0.3)
            continue
        # 作业线程收尾（job.json 落 done + 从登记表摘除）先于测试清理——
        # Windows 下并发打开的文件删不掉（WinError 32），双条件等静止
        settle = _t.monotonic() + 15.0
        while _t.monotonic() < settle:
            settled = submit["job_id"] not in getattr(bridge, "_async_jobs", {})
            if settled:
                try:
                    jp = Path(bridge.state_root) / "worker_runs" / submit["job_id"] / "job.json"
                    if json.loads(jp.read_text(encoding="utf-8")).get("status") in ("done", "failed"):
                        break
                except Exception:
                    pass
            _t.sleep(0.2)
        return r  # done → _async_result 返回完整 diagnosis（或抛错）
    raise AssertionError(f"作业 {submit['job_id']} 超时未完成: "
                         + str(list((Path(bridge.state_root) / "worker_runs").glob("*/*"))))


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

        diag = _poll_evaluate(bridge, bridge.dispatch(
            "factor.evaluate",
            {"envId": "primary", "source": BASELINE_SOURCE, "stage": "development"}))
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

        # worker 运行临时目录不应残留（每次调用后清理）——异步作业目录
        # （job-*）按 C2 设计保留收尸，不计残留；只查同步路径的 tmp* 目录
        worker_runs = Path(bridge.state_root) / "worker_runs"
        if worker_runs.exists():
            leftovers = [p for p in worker_runs.iterdir()
                         if p.is_dir() and p.name.startswith("tmp")]
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
    """pytest 入口：registry_submit 不执行 factor/环境，只收诊断对象落盘。

    2026-08-19 A3 起 submit 是 deflated 门控执行点：诊断须带完整
    deflated_train 充分统计量（sr_hat/skew/kurt/n_obs）供提交时刻重算。
    本测试的去耦合语义不变：不 require env、不编译、不评估。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        bridge, _ = _make_bridge(root)
        # v3（2026-08-21）：冷启动即有选运底价（M=1 → E|Z|≈0.798），deflated
        # 门需 pool_std 基线——先跑 null 校准供基线。submit 本身仍不执行
        # factor/不编译/不评估（去耦合语义不变）。
        bridge.dispatch("data.load", {"envId": "primary"})
        bridge.dispatch("factor.random_generate",
                        {"envId": "primary", "mode": "null-calibration", "n": 5})
        diagnosis = {"ic_ir_train": 0.25, "column_perm_train": {"z": 4.0, "p": 0.0001},
                     "beta_exposure": 0.1, "ic_n_train": 50,
                     "deflated_train": {"p": 0.001, "n_trials": 1,
                                        "sr_hat": 0.5, "skew": 0.0, "kurt": 3.0,
                                        "n_obs": 60}}
        res = bridge.dispatch("registry.submit", {
            "name": "candidate_a", "signal": "momentum20",
            "diagnosis": diagnosis, "source": BASELINE_SOURCE,
        })
        assert res["accepted"] is True, res
        # 2026-08-27 紧凑投影：count/accepted/entries（无 agent 侧全量入口）
        got = bridge.dispatch("registry.get", {})
        assert got["count"] == 1 and got["accepted"] == 1
        assert got["entries"][0]["name"] == "candidate_a"
        assert got["entries"][0]["accepted"] is True
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
        # 2026-08-28 加固配套：test 只测已入册候选——先入册再消费
        from dsh_factor_mining.discipline import source_fingerprint
        from dsh_factor_mining.state import write_registry
        write_registry([{"name": "baseline_mom",
                         "source_hash": source_fingerprint(BASELINE_SOURCE),
                         "accepted": True, "source": BASELINE_SOURCE}],
                       root=str(root / "state"))
        r1 = _poll_evaluate(bridge, bridge.dispatch(
            "factor.evaluate",
            {"envId": "primary", "source": BASELINE_SOURCE, "stage": "test"}),
            timeout_s=180.0)
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
