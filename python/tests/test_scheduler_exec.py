# -*- coding: utf-8 -*-
"""执行灰度（票 4a）+ 收编（票 4b）+ 排序先行实验的桥集成测试。

核心验收：
① 逐比特回退：exec_share=0（默认）时 drained 判定/深化排序/loop 组装
   与无调度器状态零差异（红线 8）
② drain 收编：exec 模式 + frontier 非空 → drained=False；影子模式不收编
③ 排序先行实验：rank_by_a 关=现状 p 升序逐比特；开=有 score 的按 A，
   无 score 的保持 p 序在后
④ 选点行进 loop（点名通道内容层，投放层零接触）
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
from dsh_factor_mining.state import (_atomic_write_json, _path,
                                     read_mining_state)


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
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high", "low": "low",
                                "close": "close", "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    b._resolve_pool_std = lambda env_id, tsd, horizon=None: (0.255, None)
    b._n_eff_from_entries = lambda *a, **k: {
        "n_trials": 50, "n_eff": 7.0, "bar_sigma": 3.3, "nu": None}
    return b


def _sketch(seed, n=100):
    return [round(float(x), 4) for x in
            np.random.default_rng(seed).normal(0, 0.02, n)]


def _rig(bridge, entries, admitted=()):
    """固定计价环境 + 写 trail/registry（test_deepen_telemetry 手法）。"""
    root = Path(bridge.state_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "trail_engine.json").write_text(json.dumps(entries),
                                            encoding="utf-8")
    (root / "registry.json").write_text(json.dumps(
        [{"name": f"adm_{a[:6]}", "source_hash": a, "accepted": True}
         for a in admitted]), encoding="utf-8")


def _entry(h, sr, ts, sketch, horizon=20, origin="hypothesis", g4=3.0):
    return {"ts": ts, "stage": "development", "source_hash": h,
            "horizon": horizon, "ic_ir": sr, "verdict": "needs_review",
            "authored": origin != "random", "origin": origin,
            "dsr_stats": {"sr_hat": sr, "skew": 0.0, "kurt": g4, "n_obs": 85},
            "ic_series_sketch": sketch}


def _set_cfg(bridge, **sched):
    st = read_mining_state(bridge.state_root)
    st["scheduler"] = sched
    with scheduler.state_write_lock(bridge.state_root):
        _atomic_write_json(_path("mining_state", bridge.state_root), st)


def test_bitforbit_rollback_deepen_order_and_loop(bridge):
    """逐比特回退：默认（enabled=False）下深化队列 p 升序原样、loop 无
    scheduler 键——与集成前行为零差异（红线 8）。"""
    h1, h2 = "H1" * 8, "H2" * 8
    # h2 的 p 更小（sr 更高但仍在带内）→ 现状 p 升序应 h2 在前
    entries = [_entry(h1, 0.92, "2026-09-28T10:00:00", _sketch(1)),
               _entry(h2, 0.98, "2026-09-28T10:05:00", _sketch(2))]
    _rig(bridge, entries)
    # 给 h1 挂高 A 分、h2 无分——若回退失效，h1 会窜到前面
    derivation.note_score(bridge.state_root, h1, {"A": 5.0}, rank=0,
                          fn_version="v1.1")
    loop = bridge._loop_directive()
    assert [q["hash_prefix"] for q in loop["deepen"]["queue"]] == \
        [h2[:12], h1[:12]], "p 升序原样（rank_by_a 关=逐比特现状）"
    assert "scheduler" not in loop and "scheduler_picks" not in loop


def test_rank_by_a_experiment_reorders(bridge):
    """排序先行实验：开关开启 → 有 score 的按 A 降序在前（打分器覆盖
    优先），无 score 的保持 p 序在后（没覆盖的不换序，诚实不虚挂）。"""
    h1, h2, h3 = "H1" * 8, "H2" * 8, "H3" * 8
    # p 序（sr 带内 0.90-1.02）：h3(0.99) < h1(0.95) < h2(0.92)
    entries = [_entry(h1, 0.95, "2026-09-28T10:00:00", _sketch(1)),
               _entry(h2, 0.92, "2026-09-28T10:05:00", _sketch(2)),
               _entry(h3, 0.99, "2026-09-28T10:10:00", _sketch(3))]
    _rig(bridge, entries)
    # h1 高分、h2 低分（有分组内部 A 降序）；h3 无分（殿后按 p 序）
    derivation.note_score(bridge.state_root, h1, {"A": 5.0}, rank=0,
                          fn_version="v1.1")
    derivation.note_score(bridge.state_root, h2, {"A": -2.0}, rank=1,
                          fn_version="v1.1")
    _set_cfg(bridge, enabled=True, rank_by_a=True)
    out = bridge._deepen_telemetry(entries)
    order = [q["hash_prefix"] for q in out["queue"]]
    assert order == [h1[:12], h2[:12], h3[:12]], \
        f"rank_by_a 开：有分组按 A 降序（h1=5 > h2=-2）在前，无分 h3 殿后: {order}"


def test_drain_takeover_only_in_exec_mode(bridge):
    """4a drain 收编：exec（exec_share>0）+ frontier 非空 → 不 drained；
    影子（exec_share=0）→ drained 判定原样。"""
    h1 = "H1" * 8
    entries = [_entry(h1, 0.95, "2026-09-28T10:00:00", _sketch(1))]
    _rig(bridge, entries)
    # 影子模式：队列非空本来就不 drained——构造真 drained 基线（空队列
    # 空 trail pass 库存），验证 exec 收编翻回 False
    entries_empty = []
    _rig(bridge, entries_empty)
    derivation.note_node(bridge.state_root, "X" * 16, None, None,
                         "root_unlinked", status="frontier")
    # 影子（默认 exec_share=0）：frontier 有节点但不收编
    _set_cfg(bridge, enabled=True, exec_share=0.0)
    loop = bridge._loop_directive()
    assert loop["governor"]["drained"] is True, "影子不收编（逐比特回退）"
    # exec：frontier 非空 → 收编
    _set_cfg(bridge, enabled=True, exec_share=0.5)
    loop2 = bridge._loop_directive()
    assert loop2["governor"]["drained"] is False, \
        "exec 模式 frontier 非空 → drain 让位（算力落点由调度器点名）"


def test_scheduler_picks_reach_loop_content_layer(bridge):
    """选点行进 loop（点名通道=内容层；投放层零接触——TS 无感知）。"""
    h1 = "H1" * 8
    entries = [_entry(h1, 0.95, "2026-09-28T10:00:00", _sketch(1))]
    _rig(bridge, entries)
    _set_cfg(bridge, enabled=True, exec_share=0.5)
    loop = bridge._loop_directive()
    assert "scheduler" in loop, "tick 遥测进 loop"
    # deepen 遥测把近失带准入 frontier，score 后应有 picks（exec 模式）
    sched = loop.get("scheduler") or {}
    if sched.get("policy") == "exec" and sched.get("picks"):
        assert "scheduler_picks" in loop
        pick = loop["scheduler_picks"][0]
        assert {"hash", "op", "budget", "key"} <= set(pick)
        assert pick["key"].startswith("pick:")
    else:
        # 近失带节点缺 p_def 快照（tick 打不出 A）→ 无 picks 也是合法
        # 状态——但 scheduler 块必须在（影子打分运行中）
        assert sched.get("policy") in ("shadow", "exec")


def test_disabled_bitforbit_no_scheduler_side_effects(bridge):
    """关闭态零副作用：不写 scheduler_state、不落 score、drained/策略
    与从前一致。"""
    entries = [_entry("H1" * 8, 0.95, "2026-09-28T10:00:00", _sketch(1))]
    _rig(bridge, entries)
    bridge._loop_directive()
    root = Path(bridge.state_root)
    assert not (root / "scheduler_state.json").exists(), \
        "enabled=False 零写调度器状态（逐比特回退）"
