# -*- coding: utf-8 -*-
"""分支级事件日志（调度器票 1b，2026-09-29 任务书）：expand 事件挂点。

锁四件事：
① 新试验入账 → expand 事件落盘（含算力消耗 cpu_s/slots、new_candidate
   dedup-miss 标志、家族归属 family）+ 图节点同步
② 边来源三纪律：origin=random → farm_seed 根；同族前驱 → declared；
   推断不出 → 诚实根 root_unlinked
③ 重评同 (hash,stage,horizon) → new_candidate=False（统计记账开关）
④ 红线 5：事件落账零计入活动数（act_n 不动）——打分不是挖掘活动
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining import derivation, scheduler
from dsh_factor_mining.bridge import Bridge
from dsh_factor_mining.state import lane_activity, read_mining_state


def _panel(path: Path, T=600, N=40, seed=4):
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
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high", "low": "low",
                                "close": "close", "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    # 固定计价环境（同 test_deepen_telemetry._rig 手法）：无 null landscape
    # 的测试桥上现价快照诚实缺失——patch 使池参数可用
    b._resolve_pool_std = lambda env_id, tsd, horizon=None: (0.255, None)
    b._n_eff_from_entries = lambda *a, **k: {
        "n_trials": 50, "n_eff": 7.0, "bar_sigma": 3.3, "nu": None}
    return b


def _sketch(seed, n=100, rho=None, base=None):
    if rho is not None and base is not None:
        b = np.asarray(base, dtype=np.float64)
        e = np.random.default_rng(seed).normal(0, 1, n)
        x = rho * (b - b.mean()) / b.std() + np.sqrt(1 - rho * rho) * e
        return [round(float(v), 4) for v in x]
    return [round(float(x), 4) for x in
            np.random.default_rng(seed).normal(0, 0.02, n)]


def _result(sr=1.0, sketch=None, cpu_s=3.2, horizon=20):
    return {"ic_ir_train": sr, "verdict": "needs_review",
            "horizon": horizon, "ic_series_train": sketch or _sketch(1),
            "perf": {"cpu_s": cpu_s},
            "deflated_train": {"sr_hat": sr, "skew": 0.0, "kurt": 3.0,
                               "n_obs": 85}}


def _append(bridge, hash_, sketch_seed, ts, origin="hypothesis", sr=1.0,
            sketch_override=None):
    """绕过评估直接驱动入账路径（expand 事件挂点在此）。"""
    sketch = sketch_override if sketch_override is not None else _sketch(sketch_seed)
    bridge._inspiration_origin = lambda h, lane: origin
    item = [(hash_, "development", _result(sr=sr, sketch=sketch), None)]
    bridge._append_engine_trail_batch("primary", item)


def test_expand_event_fields_and_graph_node(bridge, tmp_path):
    root = Path(bridge.state_root)
    _append(bridge, "A" * 16, 1, "t1")
    evs = derivation.load_events(root)
    assert [e["ev"] for e in evs] == ["expand"]
    ev = evs[0]
    assert ev["child"] == "A" * 16 and ev["new_candidate"] is True
    assert ev["compute"]["cpu_s"] == 3.2 and ev["compute"]["slots"] == 1
    assert ev["origin"] == "hypothesis" and ev["lane"] == "default"
    assert "family" in ev and "dsr" in ev and "n_trials" in ev, \
        "评估时点快照必须自带（r̂/对账/重放自足）"
    nodes = derivation.load_nodes(root)
    assert "A" * 16 in nodes
    assert nodes["A" * 16]["edge_source"] == "root_unlinked", \
        "首条无前驱——诚实根"


def test_farm_seed_and_declared_edges(bridge, tmp_path):
    root = Path(bridge.state_root)
    s1 = _sketch(10)
    _append(bridge, "F1" * 8, 10, "t1", origin="random")
    # 与 F1 同族（sketch 相关 0.95）+ authored → declared 边
    _append(bridge, "V1" * 8, 11, "t2", origin="hypothesis",
            sketch_override=_sketch(11, rho=0.95, base=s1))
    nodes = derivation.load_nodes(root)
    assert nodes["F1" * 8]["edge_source"] == "farm_seed"
    assert nodes["F1" * 8]["parent"] is None, "农场种子=根"
    assert nodes["V1" * 8]["edge_source"] == "declared"
    assert nodes["V1" * 8]["parent"] == "F1" * 8, "同族前驱挂边"
    fam = derivation.family_root(root, "V1" * 8)
    assert fam == "F1" * 8
    evs = derivation.load_events(root)
    assert evs[1]["family"] == "F1" * 8, "家族归属随事件落账"


def test_reeval_not_new_candidate(bridge, tmp_path):
    root = Path(bridge.state_root)
    _append(bridge, "B" * 16, 5, "t1")
    _append(bridge, "B" * 16, 5, "t2")  # 同 (hash,stage,horizon) 重评
    evs = [e for e in derivation.load_events(root) if e["ev"] == "expand"]
    assert len(evs) == 2, "重评也落 expand（算力钱照记）"
    assert evs[0]["new_candidate"] is True
    assert evs[1]["new_candidate"] is False, \
        "dedup 内候选不付统计钱——缓存命中只付算力钱"


def test_events_do_not_touch_activity_counters(bridge, tmp_path):
    """红线 5：事件落账零计入 lane 活动数（act_n 不动）。"""
    root = Path(bridge.state_root)
    before = lane_activity(read_mining_state(root), "default")
    _append(bridge, "C" * 16, 7, "t1")
    after = lane_activity(read_mining_state(root), "default")
    assert before == after, "记账不是挖掘活动——不得喂饱增长双轴"


def test_scheduler_disabled_zero_behavior(bridge, tmp_path):
    """红线 8/I5：scheduler.enabled=False（默认）→ loop 无 scheduler 块，
    事件照记（事实层独立于调度器开关）。"""
    root = Path(bridge.state_root)
    _append(bridge, "D" * 16, 8, "t1")
    loop = bridge._loop_directive()
    assert "scheduler" not in loop, "影子未开启——零行为（逐比特回退态）"
    assert derivation.load_events(root), "但事实账照记（1b 独立于开关）"


def test_pick_consumption_via_order_edge(bridge, tmp_path):
    """4a 消费记账闭环：挂出 pick → 同族新试验入账 → order 边挂上、
    pick 被消费（pending 清空）。"""
    root = Path(bridge.state_root)
    s1 = _sketch(20)
    _append(bridge, "P1" * 8, 20, "t1")
    scheduler._mut_scheduler_state(
        root, pending_picks=[{"hash": "P1" * 8, "op": "deepen_variant",
                              "budget": 1, "key": "pick:P1:1"}])
    bridge._append_engine_trail_batch("primary", [
        ("P1v" * 7, "development",
         _result(sketch=_sketch(21, rho=0.95, base=s1)), None)])
    nodes = derivation.load_nodes(root)
    assert nodes["P1v" * 7]["edge_source"] == "order", \
        "点名命中的试验挂 order 边"
    assert nodes["P1v" * 7]["parent"] == "P1" * 8
    st = scheduler.read_scheduler_state(root)
    assert not st.get("pending_picks"), "pick 已消费（不重复挂出）"
    assert st["consumed_picks"][0]["consumed_by"] == "P1v" * 7
    evs = derivation.load_events(root)
    assert evs[-1].get("via_pick") == "pick:P1:1", "消费记账键随 expand 落账"
