# coding=utf-8
"""三原语判定层回归（2026-10-02 总任务书任务 A）。

覆盖：C1-C4 欠账扫描、E1 前沿枚举（家族预算+已试轴扣减）、E3 载荷、
R1-R3 成熟检查、穷尽谓词、漂移报警、影子 tick 记账（exec=false 逐比特
现状=指令仍由旧阶梯产出）、exec=true 接管与防复读护栏。
红线复验：shadow tick 零调用 lane_activity_tick（记账不是挖掘活动）。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import primitives  # noqa: E402
from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.state import lane_activity  # noqa: E402


def _make_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 600, 40
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
    return b


def _ctx(root, **over):
    base = {
        "lane": "default", "state_root": str(root), "now": time.time(),
        "pending": None, "pending_rejected": None,
        "engine_trail": [], "engine_own": [], "agent_trail": [],
        "growth_ts": None, "running_jobs": [], "last_poll_ts": None,
        "fam_marg": None, "plateau": False, "deepen_tel": {"queue": []},
        "strategy": None, "agent_rounds": 0, "n_trials": 0,
        "n_trials_lane": 0, "streak": 0, "arc": 0, "max_rounds": 50,
        "max_cluster_trials": 200, "lit_search_count": 0,
        "accepted_n": 0, "milestone_fired": None,
        "random_available": True, "mechanism_available": True,
        "inspiration_reset": None,
    }
    base.update(over)
    return base


# ---- C 类欠账 ----

def test_c1_settlement_debt(tmp_path):
    """journal adjudicated（镜像已出未结算）→ C1 欠账；结算后消账。"""
    root = tmp_path / "state"
    d = root / "journal" / "default"
    d.mkdir(parents=True)
    (d / "live.md").write_text(
        "## H-01 adjudicated 旧标题\nref:abc123456789\n"
        "ts:2026-10-02T10:00:00\n> [update adjudicated] 镜像:评估已完成,待结算\n\n"
        "## H-02 open 新假设\n", encoding="utf-8")
    debts = primitives.scan_debts(_ctx(root))
    c1 = [x for x in debts if x["code"] == "C1"]
    assert len(c1) == 1 and c1[0]["subject"] == "journal:H-01", debts
    assert "factor_journal_update" in c1[0]["settle"]


def test_c2_dispatch_debt(tmp_path):
    """pending 声明 + 增长时钟停在 16min 前 → C2；窗内不触发。"""
    root = tmp_path / "state"
    now = time.time()
    ctx = _ctx(root, pending="测动量反转",
               growth_ts=now - 16 * 60, now=now)
    debts = primitives.scan_debts(ctx)
    assert [x["code"] for x in debts if x["code"] == "C2"], debts
    ctx2 = _ctx(root, pending="测动量反转",
                growth_ts=now - 5 * 60, now=now)
    assert not [x for x in primitives.scan_debts(ctx2)
                if x["code"] == "C2"]


def test_c3_poll_debt(tmp_path):
    """本线在途作业 + 6min 未轮询 → C3；fresh 作业（刚派发）不触发。"""
    root = tmp_path / "state"
    now = time.time()
    ctx = _ctx(root, running_jobs=[("job-1", now - 20 * 60)],
               last_poll_ts=now - 6 * 60, now=now)
    debts = primitives.scan_debts(ctx)
    assert [x["code"] for x in debts if x["code"] == "C3"], debts
    ctx2 = _ctx(root, running_jobs=[("job-1", now - 60)],
                last_poll_ts=None, now=now)
    assert not [x for x in primitives.scan_debts(ctx2)
                if x["code"] == "C3"]


def test_c4_record_debt(tmp_path):
    """引擎评估比叙事 trail 新 11min+ → C4。"""
    root = tmp_path / "state"
    now = time.time()
    eng = [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now)), "source_hash": "a"}]
    nar = [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now - 12 * 60))}]
    debts = primitives.scan_debts(_ctx(root, engine_own=eng,
                                       agent_trail=nar, now=now))
    assert [x["code"] for x in debts if x["code"] == "C4"], debts


def test_debts_oldest_first(tmp_path):
    """欠账排序=最旧者优先（age 大者前）。"""
    root = tmp_path / "state"
    now = time.time()
    eng = [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now))}]
    nar = [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                                time.localtime(now - 20 * 60))}]
    ctx = _ctx(root, pending="x", growth_ts=now - 16 * 60,
               engine_own=eng, agent_trail=nar, now=now)
    debts = primitives.scan_debts(ctx)
    ages = [d["age_s"] for d in debts if d["age_s"] is not None]
    assert ages == sorted(ages, reverse=True), debts


# ---- E1 前沿枚举 ----

def test_e1_frontier_budget_and_tried_ops(tmp_path):
    """图上 frontier 节点×合法轴−已试轴；家族预算耗尽 → 前沿空。"""
    from dsh_factor_mining import derivation
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    derivation.note_node(root, "h1", None, None, "root_unlinked")
    derivation.note_node(root, "h2", "h1", "winsorize", "order")
    out = primitives.enumerate_frontier(root, family_streak=3,
                                        max_cluster_trials=200, lane=None)
    ops_h1 = {c["op"] for c in out["candidates"] if c["parent"] == "h1"}
    assert "winsorize" not in ops_h1, "已试轴必须扣除"
    assert ops_h1, "未试轴应在前沿"
    assert out["budget_left"] == 197
    # 预算耗尽（streak >= max）→ 候选空
    out2 = primitives.enumerate_frontier(root, family_streak=200,
                                         max_cluster_trials=200, lane=None)
    assert out2["candidates"] == [], out2
    assert out2["budget_left"] == 0


# ---- E3 载荷 ----

def test_e3_payload_fields(tmp_path):
    top = {"parent": "h1", "op": "lag_decay", "family": "h1",
           "score": {"A": 1.2, "Q": 1.0, "U": None, "D": 0.2, "E": 0.4,
                     "C": 0.1}}
    trail = [{"source_hash": "h1", "ic_ir": 0.2, "verdict": "pass"},
             {"source_hash": "hx", "ic_ir": 0.0, "verdict": "fail"}]
    p = primitives.expand_payload(top, {"enough_data": True,
                                        "marginal": -0.07,
                                        "family_best": 0.5,
                                        "plateau_trials": 9},
                                  plateau=False, budget_left=12,
                                  engine_trail=trail,
                                  events=[{"ev": "expand", "child": "hx",
                                           "family": "h1"}])
    assert p["coordinate"]["variant_direction"] == "lag_decay"
    assert p["coordinate"]["trail_entry"] == 0
    assert p["budget_left"] == 12
    assert p["plateau"]["flag"] is True
    assert p["falsified_siblings"][0]["hash"] == "hx"


# ---- 穷尽谓词 ----

def test_exhaustion_predicate():
    empty_frontier = {"candidates": []}
    no_rescue = {"r1": [], "r2": None, "r3": []}
    dry = {"near_miss_n": 0, "standby_n": 0, "incubation_open_n": 0}
    exh = primitives.exhaustion_check([], empty_frontier, no_rescue,
                                      dry, pending=None)
    assert exh["exhausted"] is True
    assert "数据集轮换" in exh["suggestion"]
    # 任一来源非空 → 不穷尽
    wet = dict(dry, near_miss_n=2)
    assert primitives.exhaustion_check(
        [], empty_frontier, no_rescue, wet, None)["exhausted"] is False
    assert primitives.exhaustion_check(
        [{"code": "C1"}], empty_frontier, no_rescue, dry,
        None)["exhausted"] is False


# ---- 漂移报警 ----

def test_drift_circle_alarm():
    """最近 20 试验 8+ 同构零产出 → 绕圈红字。"""
    fp = ["fpA"] * 8
    trials = [{"source_hash": f"h{i}", "construction_fp": fp,
               "verdict": "fail"} for i in range(8)]
    trials += [{"source_hash": f"g{i}", "construction_fp": [f"f{i}"],
                "verdict": "fail"} for i in range(4)]
    alarms = primitives.drift_alarms(trials, window=20, min_cluster=8)
    assert any(a["code"] == "D-circle" for a in alarms), alarms


def test_drift_no_alarm_on_healthy():
    trials = [{"source_hash": f"h{i}", "construction_fp": [f"f{i}"],
               "verdict": "pass" if i % 3 == 0 else "fail"}
              for i in range(10)]
    assert primitives.drift_alarms(trials, window=20, min_cluster=8) == []


# ---- 影子 tick：逐比特现状 + 记账 + 红线 5 ----

def test_shadow_tick_logs_and_keeps_old_ladder(tmp_path):
    """exec=false：指令仍由旧阶梯产出（loop.strategy 不变）；影子日志落盘
    记录新旧对照；tick 不喂活动轴（红线 5）。"""
    b = _make_bridge(tmp_path)
    root = Path(b.state_root)
    (root / "mining_state.json").write_text(json.dumps(
        {"loop_primitives": {"enabled": True, "exec": False,
                             "tick_min_interval_s": 0}}), encoding="utf-8")
    act0 = lane_activity(json.loads(
        (root / "mining_state.json").read_text(encoding="utf-8")), "default")
    loop = b._loop_directive()
    assert "primitives" in loop, "影子状态页必须在 loop 上"
    page = loop["primitives"]
    assert page["policy"] == "shadow"
    assert set(page["account"]) >= {"trials_global", "wall_b_N",
                                    "family_budget_left"}
    assert "map" in page and "directive_coordinate" in page
    # 指令未被接管：与旧阶梯产物一致（无 kind 标记）
    assert "kind" not in (loop["strategy"] or {})
    log = (root / primitives.SHADOW_FILE).read_text(encoding="utf-8")
    assert "old_ladder" in log and "primitive" in log
    act1 = lane_activity(json.loads(
        (root / "mining_state.json").read_text(encoding="utf-8")), "default")
    assert act1 == act0, "影子 tick 不得喂活动轴（红线 5）"


def test_disabled_zero_footprint(tmp_path):
    """enabled=false：loop 无 primitives 键、无影子日志（逐比特现状）。"""
    b = _make_bridge(tmp_path)
    root = Path(b.state_root)
    (root / "mining_state.json").write_text(json.dumps(
        {"loop_primitives": {"enabled": False}}), encoding="utf-8")
    loop = b._loop_directive()
    assert "primitives" not in loop
    assert not (root / primitives.SHADOW_FILE).exists()


# ---- exec 接管 ----

def test_exec_takes_over_strategy(tmp_path):
    """exec=true：三原语接管 strategy（带 kind 标记）；C 欠账优先于
    expand；欠账指令可重推（type=continue）。"""
    b = _make_bridge(tmp_path)
    root = Path(b.state_root)
    (root / "mining_state.json").write_text(json.dumps(
        {"loop_primitives": {"enabled": True, "exec": True,
                             "tick_min_interval_s": 0}}), encoding="utf-8")
    # 制造 C4 欠账：引擎评估新、叙事旧
    now = time.time()
    eng = [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source_hash": "a", "stage": "development",
            "ic_ir": 0.1, "ic_series_sketch": [0.1] * 64}]
    (root / "trail_engine.json").write_text(
        json.dumps(eng), encoding="utf-8")
    (root / "trail.json").write_text(json.dumps(
        [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.localtime(now - 20 * 60)), "lane": "default",
          "round": 1}]), encoding="utf-8")
    loop = b._loop_directive()
    strat = loop["strategy"]
    assert strat is not None and strat.get("kind") == "continue", strat
    assert strat["code"] in ("C1", "C2", "C3", "C4"), strat
    assert strat["type"] == "continue", "欠账指令可重推（防复读护栏在引擎侧）"
    assert loop["primitives"]["policy"] == "exec"


def test_exec_fallback_e4_never_none(tmp_path):
    """exec=true 且前沿/欠账/救援全空 → E4 回退指令（永续：永不 None）。"""
    b = _make_bridge(tmp_path)
    root = Path(b.state_root)
    (root / "mining_state.json").write_text(json.dumps(
        {"loop_primitives": {"enabled": True, "exec": True,
                             "tick_min_interval_s": 0}}), encoding="utf-8")
    loop = b._loop_directive()
    strat = loop["strategy"]
    assert strat is not None and strat.get("kind") == "fallback", strat
    assert strat["type"] in ("literature", "random", "mechanism"), strat



# ---- review 修复回归（缓存线隔离/exec 回放/governor 让位/R2 边沿/穷尽 None/cap） ----

def test_shadow_cache_lane_isolation_and_exec_replay(tmp_path):
    """缓存键 (root,lane)：双线同账本互不串页；节流回放的 exec 指令可
    反复取用（pop 不破坏缓存）。"""
    b = _make_bridge(tmp_path)
    root = Path(b.state_root)
    (root / "mining_state.json").write_text(json.dumps(
        {"loop_primitives": {"enabled": True, "exec": True,
                             "tick_min_interval_s": 3600}}), encoding="utf-8")
    # 线 A 全量计算（exec 无欠账无前沿 → E4 决策）
    pa = primitives.shadow_tick(b, _ctx(root, lane="laneA"))
    assert pa is not None and pa["lane"] == "laneA", pa
    assert pa["policy"] == "exec" and "exec_strategy" in pa
    # 线 B 同账本：不受 A 的节流窗影响（独立键），拿到自己的页
    pb = primitives.shadow_tick(b, _ctx(root, lane="laneB"))
    assert pb is not None and pb["lane"] == "laneB", pb
    # 线 A 节流回放 ×2：pop 掉 exec_strategy 后仍能再次拿到（缓存未破坏）
    r1 = primitives.shadow_tick(b, _ctx(root, lane="laneA"))
    assert r1 is not None and "exec_strategy" in r1
    r1.pop("exec_strategy")
    r2 = primitives.shadow_tick(b, _ctx(root, lane="laneA"))
    assert r2 is not None and "exec_strategy" in r2, "节流回放必须重附挂 exec 指令"


def test_exec_yields_to_governor(tmp_path):
    """超时换向（rotate）时 exec 不接管——它是卡死场景的唯一安全网
    （pending 在场使 drained=False，旧阶梯 continue，2h 零增长 → 治理
    rotate_emit）；状态页标 exec-yielded-to-governor。drain 不让位
    （exec 期供数统一走 E4，迁移映射 8.5）。"""
    b = _make_bridge(tmp_path)
    root = Path(b.state_root)
    now = time.time()
    (root / "mining_state.json").write_text(json.dumps({
        "loop_primitives": {"enabled": True, "exec": True,
                            "tick_min_interval_s": 0},
        "fam_conv_window": 0, "max_cluster_trials": 999,
        "lanes": {"default": {"drive_bk": {"growth_ts": now - 7200,
                                           "growth_trials": 0,
                                           "growth_act": 0}}},
    }), encoding="utf-8")
    # pending 在场（drained=False）+ 零增长超窗 → 治理超时换向
    (root / "trail.json").write_text(json.dumps([{
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S",
                            time.localtime(now - 7200)),
        "round": 1, "signal": "s", "attribution": "a",
        "next_hypothesis": "测X", "new_information": "无"}]),
        encoding="utf-8")
    loop = b._loop_directive()
    strat = loop["strategy"]
    assert strat is not None and "kind" not in strat, (
        "governor 活跃时指令必须保持治理产出（无 exec kind 标记）", strat)
    assert strat["key"].startswith("rotate-timeout"), strat
    assert loop["primitives"]["policy"] == "exec-yielded-to-governor", (
        loop["primitives"]["policy"])


def test_r2_edge_consumption_prefix_aligned(tmp_path, monkeypatch):
    """R2 边沿消费：全量 hash 对 12 前缀对齐——发射一次后同组合不再成熟。"""
    from dsh_factor_mining import standby as standby_mod
    comp = "deadbeef" * 8
    monkeypatch.setattr(standby_mod, "combine",
                        lambda root, k=4, ortho_th=0.3, min_abs_sr=0.75: {
                            "ok": True,
                            "members": [{"hash": comp, "sr": 1.0,
                                         "abs_sr": 1.0,
                                         "stratum": "mechanical"}],
                            "composite_hash": comp, "rule": {}})
    root = tmp_path / "state"
    ctx = _ctx(root)
    rescue = primitives.check_rescue(None, ctx)
    assert rescue["r2"] is not None, "首次检查应成熟"
    cfg = primitives.primitives_config()
    decision = primitives.decide(
        None, ctx, cfg, [], {"candidates": [], "budget_left": 5}, [],
        rescue, {"exhausted": False}, {})
    assert decision["code"] == "R2", decision
    primitives._after_emit(ctx, decision)
    rescue2 = primitives.check_rescue(None, ctx)
    assert rescue2["r2"] is None, "同组合发射后不得再成熟（前缀对齐）"


def test_exhaustion_none_is_unknown_not_dry():
    """水情子查询失败（None）不算井干——穷尽告警不得误触发。"""
    out = primitives.exhaustion_check(
        [], {"candidates": []}, {"r1": [], "r2": None, "r3": []},
        {"near_miss_n": None, "standby_n": None, "incubation_open_n": None},
        None)
    assert out["exhausted"] is False, out


def test_frontier_top_n_cap(tmp_path):
    """E1 节点上限：候选父节点 ⊆ 最新 top_n；frontier_n 仍计全量。"""
    from dsh_factor_mining import derivation
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    for i in range(15):
        derivation.note_node(root, f"h{i:02d}", None, None, "root_unlinked",
                             ts=f"2026-10-01T00:{i:02d}:00")
    out = primitives.enumerate_frontier(root, 0, 200, lane=None, top_n=5)
    parents = {c["parent"] for c in out["candidates"]}
    assert len(parents) <= 5, parents
    assert out["frontier_n"] == 15, out["frontier_n"]


def test_anti_repeat_guardrail_escalates(tmp_path):
    """防复读护栏：同欠账推 3 次未消账 → 指令带升级告警且继续推（不停推）。"""
    root = tmp_path / "state"
    ctx = _ctx(root, now=time.time(),
               pending="x", growth_ts=time.time() - 99 * 60)
    debts = primitives.scan_debts(ctx)
    d = {"code": debts[0]["code"],
         "directive": debts[0]["directive"], "subject": debts[0]["subject"],
         "age_s": debts[0]["age_s"]}
    texts = []
    for _ in range(4):
        s = primitives.decide(None, ctx, primitives.primitives_config(),
                              [d], {"candidates": []}, [], {"r1": [], "r2": None,
                                                           "r3": []},
                              {"exhausted": False}, {})
        texts.append(s["directive"])
    assert "引擎告警" in texts[3], texts[3]
    assert "继续" in texts[3] or "执行" in texts[3]
