# coding=utf-8
"""并行线（lane）隔离回归 — 2026-08-31 方案 A。

事故背景：dsh web 一个 host 共享一个 Python bridge 进程，多会话并行
挖掘时意图层状态互相污染——B 线被 obligation 强制消化 A 线的
pending_hypothesis；双线交错写入让 _family_chain 从全局尾部回走被外线
条目截断（streak/族边际/断链全错）；arc 方向预算双倍速消耗且 A 线换向
解锁 B 线预算；n_trials/bar 跳变被 agent 脑补成自己超时调用的完成。

方案 A 边界（用户拍板 2026-08-31）：
- 意图层（pending/frozen/族链/arc 预算/cluster_trials）按 lane 隔离
- 计价层（n_trials/deflation 计数）保持全局——其它线的试验照常抬 bar
- 事实层（explored 证伪记忆）保持全局共享
- 历史（无 lane 字段）条目归 default 线，数据不迁移

本文件锁定以上行为。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.lane import DEFAULT_LANE  # noqa: E402
from dsh_factor_mining.lane import reset_request_lane, set_request_lane  # noqa: E402
from dsh_factor_mining.state import (  # noqa: E402
    arc_rounds_bump,
    lane_arc_rounds,
    read_mining_state,
)


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
    return b


def _family_sketches(n: int, seed: int = 7) -> list[list[float]]:
    """同族 sketch：共同基底 + 微噪声（|ρ| > 0.9）。"""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(64)
    return [list(0.95 * base + 0.05 * rng.standard_normal(64)) for _ in range(n)]


def _indep_sketch(seed: int = 9) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(rng.standard_normal(64))


def _write_trail_entry(b: Bridge, lane: str | None, round_: int,
                       next_hypothesis: str) -> None:
    b.dispatch("paths.append", {
        **({"lane": lane} if lane is not None else {}),
        "layer": "trail",
        "entry": {"round": round_, "signal": f"sig-{round_}",
                  "attribution": "x", "next_hypothesis": next_hypothesis,
                  "new_information": "y"}})


def _engine_entries(state_root: Path) -> list[dict]:
    p = Path(state_root) / "trail_engine.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


# ---- 1. pending_hypothesis 只回显本线；外线义务不串入 ----

def test_pending_hypothesis_isolated_per_lane(tmp_path):
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "laneA", 1, "A 线假设：量价背离")
    _write_trail_entry(b, "laneB", 1, "B 线假设：隔夜波动")

    loop_a = b.dispatch("status", {"lane": "laneA"})["loop"]
    loop_b = b.dispatch("status", {"lane": "laneB"})["loop"]
    assert loop_a["pending_hypothesis"] == "A 线假设：量价背离"
    assert loop_b["pending_hypothesis"] == "B 线假设：隔夜波动"
    # obligation 点名的是各自线的假设
    assert "量价背离" in loop_a["obligation"]
    assert "隔夜波动" in loop_b["obligation"]
    assert "隔夜波动" not in loop_a["obligation"], "A 线义务被 B 线假设污染"


# ---- 2. n_trials 全局计价；本线计数另报；lane_note 归属呈现 ----

def test_n_trials_global_pricing_with_lane_attribution(tmp_path):
    b = _make_bridge(tmp_path)
    src = "\ndef factor(env):\n    import pandas as pd\n" \
          "    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
    # A 线跑 2 个不同试验，B 线跑 1 个
    b.dispatch("factor.evaluate", {"lane": "laneA", "envId": "primary",
                                   "source": src, "stage": "development"})
    src2 = src.replace("shift(20)", "shift(10)")
    b.dispatch("factor.evaluate", {"lane": "laneA", "envId": "primary",
                                   "source": src2, "stage": "development"})
    src3 = src.replace("shift(20)", "shift(5)")
    b.dispatch("factor.evaluate", {"lane": "laneB", "envId": "primary",
                                   "source": src3, "stage": "development"})

    entries = _engine_entries(tmp_path / "state")
    assert all(e.get("lane") in ("laneA", "laneB") for e in entries), \
        f"engine 条目未打 lane 标: {[e.get('lane') for e in entries]}"

    loop_a = b.dispatch("status", {"lane": "laneA"})["loop"]
    loop_b = b.dispatch("status", {"lane": "laneB"})["loop"]
    # 计价口径全局（A/B 一致）——其它线试验照常抬 bar
    assert loop_a["n_trials"] == loop_b["n_trials"] == 3
    # 本线口径分线
    assert loop_a["n_trials_lane"] == 2
    assert loop_b["n_trials_lane"] == 1
    # 归属呈现：bar 跳变有出处
    assert "其它线" in loop_a.get("lane_note", "")


# ---- 3. 家族链不被外线条目截断（streak 按线） ----

def test_family_streak_survives_foreign_interleaving(tmp_path):
    b = _make_bridge(tmp_path)
    sk = _family_sketches(3)
    entries = []
    # A 线 3 条同族 + B 线 1 条独立族交错写入（全局时间序被打散）
    for i, s in enumerate(sk):
        entries.append({"ts": f"2026-08-31T10:00:{i:02d}", "envId": "primary",
                        "source_hash": f"a{i}", "stage": "development",
                        "horizon": 20, "ic_ir": 0.1, "lane": "laneA",
                        "ic_series_sketch": s})
        if i == 1:
            entries.append({"ts": "2026-08-31T10:00:50", "envId": "primary",
                            "source_hash": "b0", "stage": "development",
                            "horizon": 20, "ic_ir": 0.2, "lane": "laneB",
                            "ic_series_sketch": _indep_sketch()})
    (tmp_path / "state" / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    loop_a = b.dispatch("status", {"lane": "laneA"})["loop"]
    loop_b = b.dispatch("status", {"lane": "laneB"})["loop"]
    # A 线链长 3：B 线条目插在中间不截断 A 线链（旧行为：全局回走被
    # b0 截断 → streak=1，族收敛/边际全在算混合族）
    assert loop_a["family_streak"] == 3, \
        f"A 线族链被外线条目截断: {loop_a['family_streak']}"
    assert loop_b["family_streak"] == 1


# ---- 4. arc 方向预算按线：A 线轮次不消耗 B 线；A 线断链不重置 B 线 ----

def test_arc_budget_per_lane(tmp_path):
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "laneA", 1, "A1")
    _write_trail_entry(b, "laneA", 2, "A2")
    _write_trail_entry(b, "laneB", 1, "B1")

    st = read_mining_state(str(tmp_path / "state"))
    assert lane_arc_rounds(st, "laneA") == 2
    assert lane_arc_rounds(st, "laneB") == 1

    # loop.round 报本线
    loop_a = b.dispatch("status", {"lane": "laneA"})["loop"]
    loop_b = b.dispatch("status", {"lane": "laneB"})["loop"]
    assert loop_a["round"] == 2
    assert loop_b["round"] == 1

    # A 线机械换向（断链归零）不重置 B 线
    arc_rounds_bump(str(tmp_path / "state"), reset=True, lane="laneA")
    st = read_mining_state(str(tmp_path / "state"))
    assert lane_arc_rounds(st, "laneA") == 0
    assert lane_arc_rounds(st, "laneB") == 1


# ---- 5. 断链检测只对本线尾部判定（交错写入不触发 arc 归零风暴） ----

def test_chain_break_detection_ignores_foreign_tail(tmp_path):
    b = _make_bridge(tmp_path)
    sk = _family_sketches(2)
    state = tmp_path / "state"
    # A 线 1 条同族条目打底
    (state / "trail_engine.json").write_text(json.dumps([{
        "ts": "2026-08-31T11:00:00", "envId": "primary",
        "source_hash": "a0", "stage": "development", "horizon": 20,
        "ic_ir": 0.1, "lane": "laneA", "ic_series_sketch": sk[0]}],
        ensure_ascii=False), encoding="utf-8")
    _write_trail_entry(b, "laneA", 1, "A1")
    assert lane_arc_rounds(read_mining_state(str(state)), "laneA") == 1

    # B 线写入一条异族条目（全局尾部变成 B 线异族——旧行为会把它当
    # A 线的断链证据，触发 arc 归零风暴）
    tok = set_request_lane("laneB")
    try:
        b._append_engine_trail_batch("primary", [(
            "b0", "development",
            {"horizon": 20, "ic_ir": 0.2,
             "ic_series_train": _indep_sketch(11)}, None)])
    finally:
        reset_request_lane(tok)

    # A 线预算不受影响：B 线条目不是 A 线的断链证据
    assert lane_arc_rounds(read_mining_state(str(state)), "laneA") == 1

    # A 线自己写异族条目 → A 线断链归零（正常换向语义保留）
    tok = set_request_lane("laneA")
    try:
        b._append_engine_trail_batch("primary", [(
            "a1", "development",
            {"horizon": 20, "ic_ir": 0.1,
             "ic_series_train": _indep_sketch(22)}, None)])
    finally:
        reset_request_lane(tok)
    assert lane_arc_rounds(read_mining_state(str(state)), "laneA") == 0


# ---- 6. 历史无标记条目归 default 线（数据不迁移） ----

def test_legacy_entries_belong_to_default_lane(tmp_path):
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    # legacy 叙事条目（无 lane 字段）
    b.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 1, "signal": "legacy", "attribution": "x",
        "next_hypothesis": "legacy 假设", "new_information": "y"}})

    loop_default = b.dispatch("status", {})["loop"]
    assert loop_default["pending_hypothesis"] == "legacy 假设"
    assert loop_default["lane"] == DEFAULT_LANE

    # 新线 B 看不到 legacy 的 pending（不接管旧叙述者的义务）
    loop_b = b.dispatch("status", {"lane": "laneB"})["loop"]
    assert loop_b["pending_hypothesis"] is None

    # legacy mining_state 顶层 arc_rounds：读侧顶层回退 + 首写迁移
    (state / "mining_state.json").write_text(json.dumps(
        {"round": 3, "arc_rounds": 5, "candidate_pool": [], "finalized": False},
        ensure_ascii=False), encoding="utf-8")
    st = read_mining_state(str(state))
    assert lane_arc_rounds(st, DEFAULT_LANE) == 5, "读侧顶层回退失效"
    arc_rounds_bump(str(state), lane=DEFAULT_LANE)
    st = read_mining_state(str(state))
    assert lane_arc_rounds(st, DEFAULT_LANE) == 6, "迁移后 default 桶计数"
    assert "arc_rounds" not in st, "顶层遗留键未清除（双计风险）"


# ---- 7. trail_summary：本线 latest5 + 分线计数 ----

def test_trail_summary_lane_views(tmp_path):
    b = _make_bridge(tmp_path)
    sk = _family_sketches(3)
    entries = [
        {"ts": f"2026-08-31T12:00:{i:02d}", "envId": "primary",
         "source_hash": f"a{i}", "stage": "development", "horizon": 20,
         "ic_ir": 0.1, "lane": "laneA", "ic_series_sketch": s,
         "verdict": "needs_review"}
        for i, s in enumerate(sk)]
    entries.append({"ts": "2026-08-31T12:00:99", "envId": "primary",
                    "source_hash": "b0", "stage": "development",
                    "horizon": 20, "ic_ir": 0.2, "lane": "laneB",
                    "ic_series_sketch": _indep_sketch(),
                    "verdict": "pass"})
    (tmp_path / "state" / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")

    s_a = b.dispatch("state.trail_summary", {"lane": "laneA"})
    hashes_a = [e["source_hash"] for e in s_a["last_engine_trail"]]
    assert hashes_a == ["a0", "a1", "a2"], f"latest5 混入外线: {hashes_a}"
    assert s_a["evaluations"]["lane"] == 3
    assert s_a["evaluations"]["foreign"] == 1
    assert s_a["evaluations"]["total"] == 4  # 全局计数不丢

    # verdict_counts 已撤出 trail_summary（名词化修复，2026-10-14 任务书
    # A3：通过/拒绝计数不进模型滚动视图）——事实层计数仍保留 lane/
    # foreign/total
    assert "verdict_counts" not in s_a["evaluations"]


# ---- 8. 模型自填的 lane 字段被基础设施身份覆盖 ----

def test_model_supplied_lane_is_overridden(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("paths.append", {"lane": "realLane", "layer": "trail",
                                "entry": {"round": 1, "signal": "s",
                                          "attribution": "x",
                                          "next_hypothesis": "h",
                                          "new_information": "y",
                                          "lane": "spoofedLane"}})
    trail = json.loads((tmp_path / "state" / "trail.json")
                       .read_text(encoding="utf-8"))
    assert trail[-1]["lane"] == "realLane", "模型声明的 lane 覆盖了会话身份"
