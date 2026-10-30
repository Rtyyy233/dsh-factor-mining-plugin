# -*- coding: utf-8 -*-
"""IC 线近失深化遥测（2026-09-15 正修）——loop 响应的 deepen 块。

背景：IC 线此前无近失指令（尾部线才有），深化全靠 agent 自觉（deepdig
清单自写配额执行率 0/30）；直驱 queue/runner 又把模型移出逐响应反馈环。
本测试锁三件事：①近失带 = 现价重算 p∈(0.05,0.30] 的队列组成（带外/
预筛外/已入册排除）②已试变体的引擎计数（_entries_linked 双信号：晚于
种子且同族）③deepen 块进 _loop_directive 返回体。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.bridge import Bridge


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
    return b


def _sketch(seed, n=100):
    return [round(float(x), 4) for x in
            np.random.default_rng(seed).normal(0, 0.02, n)]


def _related_sketch(seed, base, rho=0.95, n=100):
    b = np.asarray(base, dtype=np.float64)
    b = (b - b.mean()) / b.std()
    e = np.random.default_rng(seed).normal(0, 1, n)
    e = (e - e.mean()) / e.std()
    x = rho * b + np.sqrt(1 - rho * rho) * e
    return [round(float(v), 4) for v in x]


def _entry(h, sr, ts, sketch, horizon=20, stage="development", g4=3.0,
           authored=True, origin="hypothesis"):
    return {"ts": ts, "stage": stage, "source_hash": h, "horizon": horizon,
            "ic_ir": sr, "verdict": "needs_review", "authored": authored,
            "origin": origin,
            "dsr_stats": {"sr_hat": sr, "skew": 0.0, "kurt": g4, "n_obs": 85},
            "ic_series_sketch": sketch}


def _rig(bridge, tmp_path, entries, admitted=()):
    """固定计价环境:bar=3.3/pool_std=0.255(墙 0.8415),写 trail+registry。"""
    root = Path(bridge.state_root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "trail_engine.json").write_text(
        json.dumps(entries), encoding="utf-8")
    (root / "registry.json").write_text(
        json.dumps([{"name": f"adm_{a[:6]}", "source_hash": a, "accepted": True}
                    for a in admitted]), encoding="utf-8")
    bridge._n_eff_from_entries = lambda *a, **k: {
        "n_trials": 50, "n_eff": 7.0, "bar_sigma": 3.3, "nu": None}
    bridge._resolve_pool_std = lambda env_id, tsd, horizon=None: (0.255, None)
    return bridge


def test_near_miss_band_composition(bridge, tmp_path):
    s1 = _sketch(1)
    entries = [
        # 带内:sr 0.95 → t≈0.83 → p≈0.20 ✓
        _entry("H1" * 8, 0.95, "2026-09-14T23:00:00", s1),
        # 带下(已过线):sr 1.2 → p≈0.004
        _entry("H2" * 8, 1.20, "2026-09-14T23:05:00", _sketch(2)),
        # 预筛外:sr 0.5 < 0.75
        _entry("H3" * 8, 0.50, "2026-09-14T23:10:00", _sketch(3)),
        # 带上:sr 0.90 → p≈0.33 > 0.30
        _entry("H4" * 8, 0.90, "2026-09-14T23:15:00", _sketch(4)),
        # 已入册:同 sr 但在 registry accepted
        _entry("H5" * 8, 0.95, "2026-09-14T23:20:00", _sketch(5)),
    ]
    _rig(bridge, tmp_path, entries, admitted=["H5" * 8])
    out = bridge._deepen_telemetry(
        json.loads((Path(bridge.state_root) / "trail_engine.json").read_text()))
    qs = [q["hash_prefix"] for q in out["queue"]]
    assert qs == [("H1" * 8)[:12]], f"近失带组成错误: {qs}"
    assert out["queue"][0]["p_now"] == pytest.approx(0.20, abs=0.03)


def test_variant_counting_by_lineage(bridge, tmp_path):
    s1 = _sketch(1)
    entries = [
        _entry("H1" * 8, 0.95, "2026-09-14T23:00:00", s1),
        # 晚于种子 + 相关 0.95 → 计 1
        _entry("H1v" * 7, 0.98, "2026-09-15T00:00:00", _related_sketch(11, s1)),
        # 晚于种子但独立 → 不计
        _entry("H2" * 8, 0.97, "2026-09-15T00:05:00", _sketch(2)),
        # 早于种子的相关条目 → 不计(变体必须晚于种子)
        _entry("H0" * 8, 0.93, "2026-09-14T22:00:00", _related_sketch(12, s1)),
        # farm 撞相关后代(非 authored,origin=random)→ 不计
        # (锤过≠深化过:计数只认 authored/假设来源的同族后继;
        #  sr 压到预筛线以下使其不占队列位,只留计数扫描)
        _entry("H9" * 8, 0.50, "2026-09-15T00:10:00", _related_sketch(13, s1),
               authored=False, origin="random"),
    ]
    _rig(bridge, tmp_path, entries)
    out = bridge._deepen_telemetry(entries)
    q = next(q for q in out["queue"] if q["hash_prefix"] == ("H1" * 8)[:12])
    assert q["variants_tried"] == 1, f"变体计数错误: {q}"
    assert "authored" in out["note"]


def test_deepen_in_loop_directive(bridge, tmp_path):
    s1 = _sketch(1)
    entries = [_entry("H1" * 8, 0.95, "2026-09-14T23:00:00", s1)]
    _rig(bridge, tmp_path, entries)
    loop = bridge._loop_directive()
    assert "deepen" in loop
    assert [q["hash_prefix"] for q in loop["deepen"]["queue"]] == [("H1" * 8)[:12]]
    assert "配额" in loop["deepen"]["note"]


def _campaign_file(bridge, queue, budget):
    (Path(bridge.state_root) / "deepen_campaign.json").write_text(
        json.dumps({"budget": budget, "queue": queue, "history": [],
                    "cursor": 0}), encoding="utf-8")


def test_campaign_active_targets_head(bridge, tmp_path):
    """战役激活:队首=当前目标,带内展示,变体计数进 campaign 块。"""
    s1 = _sketch(1)
    h1 = "H1" * 8
    entries = [
        _entry(h1, 0.95, "2026-09-14T23:00:00", s1),
        _entry("H1v" * 7, 0.50, "2026-09-15T00:00:00", _related_sketch(11, s1),
               authored=True),
    ]
    _rig(bridge, tmp_path, entries)
    _campaign_file(bridge, [h1, "ZZ" * 8], budget=3)
    out = bridge._deepen_telemetry(entries)
    camp = out["campaign"]
    assert camp["state"] == "active"
    assert camp["target"]["hash_prefix"] == h1[:12]
    assert camp["target"]["variants"] == 1 and camp["target"]["budget"] == 3
    assert camp["progress"] == "1/2"
    assert h1[:12] in [q["hash_prefix"] for q in out["queue"]], "目标仍在展示队列"
    assert "深化战役 active" in out["note"]


def test_campaign_engine_exhaustion_advances(bridge, tmp_path):
    """引擎穷尽宣告:authored 变体≥预算 → 出队入 history,文件落盘,
    展示队列过滤;队列清空 → complete。模型无权宣布穷尽。"""
    s1 = _sketch(1)
    h1 = "H1" * 8
    entries = [
        _entry(h1, 0.95, "2026-09-14T23:00:00", s1),
        _entry("H1v" * 7, 0.50, "2026-09-15T00:00:00", _related_sketch(11, s1),
               authored=True),
    ]
    _rig(bridge, tmp_path, entries)
    _campaign_file(bridge, [h1], budget=1)
    out = bridge._deepen_telemetry(entries)
    assert out["campaign"]["state"] == "complete"
    saved = json.loads((Path(bridge.state_root) / "deepen_campaign.json")
                       .read_text(encoding="utf-8"))
    assert saved["history"][0]["hash"] == h1
    assert saved["history"][0]["outcome"] == "exhausted_trials"
    assert saved["history"][0]["variants"] == 1
    assert h1[:12] not in [q["hash_prefix"] for q in out["queue"]], \
        "引擎已决种子不得留在展示队列"


def test_campaign_out_of_band_advances(bridge, tmp_path):
    """出带推进:目标现价不在带内(如预筛外)→ out_of_band 出队。"""
    hz = "HZ" * 8
    entries = [
        _entry(hz, 0.50, "2026-09-14T23:00:00", _sketch(9)),  # 预筛外
        _entry("H1" * 8, 0.95, "2026-09-14T23:30:00", _sketch(1)),
    ]
    _rig(bridge, tmp_path, entries)
    _campaign_file(bridge, [hz], budget=5)
    out = bridge._deepen_telemetry(entries)
    assert out["campaign"]["state"] == "complete"
    saved = json.loads((Path(bridge.state_root) / "deepen_campaign.json")
                       .read_text(encoding="utf-8"))
    assert saved["history"][0]["outcome"] == "out_of_band"


def test_campaign_fresh_window_ignores_stock(bridge, tmp_path):
    """新鲜窗:战役启动(started)之后的历史 authored 变体不计入预算——
    老种子存量表亲不清零会未挖先'穷尽'。"""
    s1 = _sketch(1)
    h1 = "H1" * 8
    entries = [
        _entry(h1, 0.95, "2026-09-14T23:00:00", s1),
        # 战役启动前的 authored 变体(09-15)——不计
        _entry("H1v" * 7, 0.50, "2026-09-15T00:00:00", _related_sketch(11, s1),
               authored=True),
    ]
    _rig(bridge, tmp_path, entries)
    (Path(bridge.state_root) / "deepen_campaign.json").write_text(
        json.dumps({"budget": 1, "queue": [h1], "history": [], "cursor": 0,
                    "started": "2026-09-15T12:00:00"}), encoding="utf-8")
    out = bridge._deepen_telemetry(entries)
    camp = out["campaign"]
    assert camp["state"] == "active" and camp["target"]["variants"] == 0, \
        "启动前存量不得计入战役预算"


def test_campaign_budget_defaults_to_plugin_cap(bridge, tmp_path):
    """预算缺省与 DSH 插件一致:取 max_cluster_trials(用户拍板);战役
    文件显式给 N 才覆盖。"""
    from dsh_factor_mining.bridge import MINING_CONFIG
    s1 = _sketch(1)
    h1 = "H1" * 8
    entries = [_entry(h1, 0.95, "2026-09-14T23:00:00", s1)]
    _rig(bridge, tmp_path, entries)
    (Path(bridge.state_root) / "deepen_campaign.json").write_text(
        json.dumps({"budget": None, "queue": [h1], "history": [],
                    "cursor": 0}), encoding="utf-8")
    camp = bridge._deepen_telemetry(entries)["campaign"]
    assert camp["target"]["budget"] == MINING_CONFIG["max_cluster_trials"], \
        "缺省预算须随引擎簇试验上限"


def test_campaign_family_win_via_variant(bridge, tmp_path):
    """家族胜利:目标本体未入册,但其更晚同族条目入册 →
    admitted_via_variant 出队(深化的正解几乎总是变体入册)。"""
    s1 = _sketch(1)
    h1 = "H1" * 8
    hv = "H1v" * 7
    entries = [
        _entry(h1, 0.95, "2026-09-14T23:00:00", s1),
        _entry(hv, 0.50, "2026-09-15T00:00:00", _related_sketch(11, s1),
               authored=True),  # 预筛外不进带,但其 hash 入册
    ]
    _rig(bridge, tmp_path, entries, admitted=[hv])
    _campaign_file(bridge, [h1], budget=5)
    out = bridge._deepen_telemetry(entries)
    assert out["campaign"]["state"] == "complete"
    saved = json.loads((Path(bridge.state_root) / "deepen_campaign.json")
                       .read_text(encoding="utf-8"))
    assert saved["history"][0]["outcome"] == "admitted_via_variant"
    assert saved["history"][0]["via"] == hv[:12]


def test_campaign_force_resolve_backfill(bridge, tmp_path):
    """执行器回填:复合与腿的 sketch 相关常 <0.6,自动家族胜利判不出
    ——force_resolve 显式裁决(带 via+note)由引擎优先消费转录进
    history,已决种子移出展示队列。"""
    s1 = _sketch(1)
    h1, h2 = "H1" * 8, "H2" * 8
    entries = [
        _entry(h1, 0.95, "2026-09-14T23:00:00", s1),
        _entry(h2, 0.94, "2026-09-14T23:30:00", _sketch(2)),
    ]
    _rig(bridge, tmp_path, entries)
    (Path(bridge.state_root) / "deepen_campaign.json").write_text(
        json.dumps({"budget": 5, "queue": [h1, h2], "history": [],
                    "cursor": 0,
                    "force_resolve": [{"hash": h1, "outcome":
                                       "admitted_via_variant",
                                       "via": "AB" * 8,
                                       "note": "复合入册,腿源解决"}]}),
        encoding="utf-8")
    out = bridge._deepen_telemetry(entries)
    camp = out["campaign"]
    assert camp["state"] == "active"
    assert camp["target"]["hash_prefix"] == h2[:12], "h1 须被回填解决,目标推进到 h2"
    assert camp["progress"] == "2/2"
    saved = json.loads((Path(bridge.state_root) / "deepen_campaign.json")
                       .read_text(encoding="utf-8"))
    assert saved["history"][0]["hash"] == h1
    assert saved["history"][0]["via"] == ("AB" * 8)[:12]
    assert h1[:12] not in [q["hash_prefix"] for q in out["queue"]]
