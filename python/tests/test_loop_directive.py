# coding=utf-8
"""自主性停走接线回归（2026-08-21；v3 停点收紧 2026-08-24）。

事故背景（trail.json round 8/9 实证）：agent 在里程碑时刻主动停笔
（「本次探索结束」「等待用户决定是否清 trail 重置后重新注册」），
或写了 next_hypothesis 不执行——用户被迫用固定话术手动推动。

v2 修复（2026-08-21）：引擎响应注入 loop 指令（state=running/may_stop +
obligation + escalation 分级升级 + submit 拒绝菜单）。

v2 失效（session.jsonl 全轨迹诊断，2026-08-24）：导入的 stateRoot 自带
8 个入册因子 → has_value 从第一刻为真 + 候选池满条件灌 8>=3 → 86 条
指令 83 条 may_stop、0 条 running，agent 33 turn 全停（中位 11 分钟），
用户手动 push 31 次；12 次穷尽宣告全假（每次 push 后同池子都挖出新族）。

v3 修复（用户决策）：合法停点 = 仅引擎机械判据（轮次上限 / 试验上限 /
IC_IR 改善收敛 / finalize）；候选池满与 accepted 因子不再是停点；
穷尽宣告写入拒收、回显降级。本文件锁定 v3 行为。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

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
    return b


def _write_engine_trail(state_root: Path, sketches: list[list[float]]) -> None:
    """直写 trail_engine.json（家族 streak 测试用——sketch 同族即链长）。"""
    entries = []
    for i, s in enumerate(sketches):
        entries.append({"ts": f"2026-08-21T20:00:{i:02d}", "envId": "primary",
                        "source_hash": f"hash{i:03d}", "stage": "development",
                        "horizon": 20, "ic_ir": 0.1,
                        "ic_series_sketch": s})
    (state_root / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _family_sketches(n: int, seed: int = 7) -> list[list[float]]:
    """同族 sketch：共同基底 + 微噪声（|ρ| > 0.9）。"""
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(64)
    return [list(0.95 * base + 0.05 * rng.standard_normal(64)) for _ in range(n)]


def _indep_sketches(n: int, seed: int = 9) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    return [list(rng.standard_normal(64)) for _ in range(n)]


# ---- 1. loop 字段存在性与冷启动 running ----

def test_loop_on_status_and_evaluate(tmp_path):
    b = _make_bridge(tmp_path)
    st = b.dispatch("status", {})
    assert "loop" in st, "status 缺 loop 指令"
    assert st["loop"]["state"] == "running"
    assert st["loop"]["obligation"] is not None  # 冷启动：继续内循环

    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": BASELINE_SOURCE,
                       "stage": "development"})
    assert "loop" in diag, "evaluate 响应缺 loop 指令"
    assert diag["loop"]["state"] in ("running", "may_stop", "must_rotate")
    assert diag["loop"]["n_trials"] >= 1  # 本次评估已入 trail


# ---- 2. pending obligation：上一轮 next_hypothesis 原样回显 ----

def test_loop_pending_obligation(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 1, "signal": "momentum",
        "attribution": "IC_IR 弱",
        "next_hypothesis": "换高阶矩维度（skew/kurtosis）",
        "new_information": "无"}})
    loop = b._loop_directive()
    assert loop["state"] == "running"
    assert loop["pending_hypothesis"] == "换高阶矩维度（skew/kurtosis）"
    assert "换高阶矩维度" in loop["obligation"], loop["obligation"]
    assert "不得静默放弃" in loop["obligation"]


# ---- 3. 家族边际收益升级（v6：不看链长，只看边际） ----

def test_family_marginal_escalation(tmp_path):
    """族内 5+ 条同族且边际递减 → 升级信息含实际数字；
    边际为正（仍在改善）→ 不升级；独立试验 → 不升级。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    # 8 条同族：前 3 条高（0.70-0.72 = 族最佳），后 5 条低（0.42-0.50）
    # W=5 → "前"窗口 = 前 3 条最佳 0.72，"最近"窗口 = 后 5 条最佳 0.50
    # → 边际 -0.22 → 严重枯竭 → 升级
    fam_sk = _family_sketches(8)
    entries = []
    for i, s in enumerate(fam_sk):
        ir = 0.70 + 0.01 * i if i < 3 else 0.50 - 0.02 * (i - 2)
        entries.append({"ic_ir": ir, "ic_series_sketch": s})
    _write_engine_trail_full(state, entries)
    loop = b._loop_directive()
    assert loop["family_streak"] == 8
    # 边际 < -0.05 → 升级
    assert loop["escalation"] is not None, loop
    # 独立试验 → 链断 → 族小 → 不升级
    _write_engine_trail(state, _indep_sketches(9))
    loop2 = b._loop_directive()
    assert loop2["escalation"] is None or loop2["family_streak"] < 5, loop2


# ---- 4. accepted 入册不是停点（v3：反转 v2 行为） ----

def test_accepted_is_not_a_stop(tmp_path):
    """v2 事故根因：has_value=accepted>0 让 may_stop 永久打开（导入的
    stateRoot 自带 8 入册因子）。v3：accepted 只是里程碑，继续挖。"""
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "strong_a", "signal": "momentum",
        "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is True, sub
    assert sub["loop"]["state"] == "running", sub["loop"]
    assert sub["loop"]["stop_reason"] is None
    assert sub["loop"]["obligation"] is not None  # 交了价值也必须继续


# ---- 4b. 候选池满不是停点 ----

def test_pool_full_never_stops(tmp_path):
    """v2 事故第二根因：候选池满（8>=3）灌进 check_termination 永久停。"""
    from dsh_factor_mining.state import check_termination
    # 直接断言 state 层：池满 → 不停
    term = check_termination({"round": 1, "global_fail_streak": 0,
                              "n_trials": 2,
                              "candidate_pool": [{}, {}, {}, {}, {}, {}, {}, {}]})
    assert term["stop"] is False, term
    # bridge 层：写入满池 mining_state → loop 仍 running
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "mining_state.json").write_text(json.dumps({
        "round": 1, "global_fail_streak": 0,
        "candidate_pool": [{"signal": f"c{i}"} for i in range(8)],
        "finalized": False}), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["state"] == "running", loop


# ---- 4c. 机械停点 1：簇试验上限（2026-08-24 用户修正：按当前簇计，
# 不按终身计——trail 只增不减，终身上限=一次到顶永久停机） ----

def test_cluster_trials_cap_stops(tmp_path):
    """同族链长达到 max_cluster_trials → must_rotate（v7：方向预算停点
    不再是静默终态——强制挂 rotate 策略，注入器照常推进）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 3, "fam_conv_window": 0,  # 关闭收敛判据
        "finalized": False}), encoding="utf-8")
    _write_engine_trail(state, _family_sketches(3))  # 3 连同族 → streak 3
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    assert loop["stop_kind"] == "direction_budget", loop
    assert "簇试验上限" in loop["stop_reason"], loop["stop_reason"]
    assert loop["cluster_trials"] == 3
    # 方向预算停点：强制 rotate（族小无边际数据也不允许原地续推）
    assert loop["strategy"]["type"] == "rotate", loop["strategy"]
    assert "不同源" in loop["strategy"]["directive"]


def test_cluster_cap_resets_on_rotation(tmp_path):
    """换方向断链重置：3 同族 + 1 异族 → streak=1 → 不停。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 3, "fam_conv_window": 0,
        "finalized": False}), encoding="utf-8")
    _write_engine_trail(state, _family_sketches(3) + _indep_sketches(1, seed=21))
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["cluster_trials"] == 1, loop


def test_lifetime_trials_never_stop(tmp_path):
    """终身 n_trials 超任意值都不停——只用于 deflation 计价，不做停点。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 3, "fam_conv_window": 0,
        "finalized": False}), encoding="utf-8")
    _write_engine_trail(state, _indep_sketches(8, seed=23))  # 8 试验全异族
    loop = b._loop_directive()
    assert loop["n_trials"] == 8
    assert loop["state"] == "running", loop


# ---- 4d. 机械停点 2：IC_IR 改善收敛 ----

def _write_engine_trail_irs(state_root: Path, irs: list[float]) -> None:
    entries = [{"ts": f"2026-08-24T10:00:{i:02d}", "envId": "primary",
                "source_hash": f"hash{i:03d}", "stage": "development",
                "horizon": 20, "ic_ir": v}
               for i, v in enumerate(irs)]
    (state_root / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _write_engine_trail_full(state_root: Path, entries: list[dict]) -> None:
    """直写完整 engine trail 条目（策略测试需要 ic_ir + sketch + verdict 组合）。"""
    norm = []
    for i, e in enumerate(entries):
        n = {"ts": f"2026-08-24T11:00:{i:02d}", "envId": "primary",
             "source_hash": f"hash{i:03d}", "stage": "development",
             "horizon": 20}
        n.update(e)
        norm.append(n)
    (state_root / "trail_engine.json").write_text(
        json.dumps(norm, ensure_ascii=False), encoding="utf-8")


# ---- 4g. 策略指令选择器（v6：边际收益驱动，删 streak 绝对阈值） ----

def test_strategy_continue_default(tmp_path):
    """无异常信号 + 具体可执行 pending → 惯性延续。"""
    b = _make_bridge(tmp_path)
    b.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 1, "signal": "amihud", "attribution": "IC_IR 0.2",
        "next_hypothesis": "测试 amihud 与 close_pos 的交互构造",
        "new_information": "无"}})
    loop = b._loop_directive()
    assert loop["state"] == "running"
    assert loop["strategy"]["type"] == "continue", loop["strategy"]
    assert loop["strategy"]["key"].startswith("continue:")


def test_strategy_rotate_on_rejected_pending(tmp_path):
    """停笔宣言被拒收 → 方向在 agent 心智上已死 → rotate。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "trail.json").write_text(json.dumps([{
        "round": 1, "signal": "x", "attribution": "y",
        "next_hypothesis": "本轮探索完成，建议换池子",
        "new_information": "无"}], ensure_ascii=False), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["pending_rejected"] is not None
    assert loop["strategy"]["type"] == "rotate", loop["strategy"]


def test_strategy_rotate_on_frozen_pending(tmp_path):
    """同一句 pending 冻结 ≥2 轮（r=13 停摆模式）→ rotate。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "trail.json").write_text(json.dumps([
        {"round": 1, "signal": "x", "attribution": "y",
         "next_hypothesis": "测试微观结构维度", "new_information": "无"},
        {"round": 2, "signal": "x", "attribution": "y",
         "next_hypothesis": "测试微观结构维度", "new_information": "无"},
    ], ensure_ascii=False), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["pending_hypothesis"] is not None
    assert loop["strategy"]["type"] == "rotate", loop["strategy"]


def test_strategy_marginal_literature(tmp_path):
    """族内边际严重枯竭（marginal < -0.10）→ literature。"""
    from dsh_factor_mining.bridge import _pick_strategy
    base = dict(stop_kind=None, streak=20, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base,
                       family_marginal={"enough_data": True,
                                        "marginal": -0.15,
                                        "family_best": 0.8,
                                        "recent_best": 0.65})
    assert s["type"] == "literature", s
    assert "严重枯竭" in s["why"]


def test_strategy_marginal_rotate(tmp_path):
    """族内边际枯竭（marginal < -0.05）→ rotate。"""
    from dsh_factor_mining.bridge import _pick_strategy
    base = dict(stop_kind=None, streak=10, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base,
                       family_marginal={"enough_data": True,
                                        "marginal": -0.07,
                                        "family_best": 0.8,
                                        "recent_best": 0.73})
    assert s["type"] == "rotate", s


def test_strategy_marginal_positive_no_rotation(tmp_path):
    """族内仍在改善（marginal >= 0）→ 不催换向（哪怕 streak=50）。"""
    from dsh_factor_mining.bridge import _pick_strategy
    base = dict(stop_kind=None, streak=50, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base,
                       family_marginal={"enough_data": True,
                                        "marginal": 0.02,
                                        "family_best": 0.82,
                                        "recent_best": 0.84})
    assert s["type"] not in ("rotate", "literature"), s


def test_strategy_small_family_no_pressure(tmp_path):
    """族内样本不足（<5）→ 不判边际 → 不催。"""
    from dsh_factor_mining.bridge import _pick_strategy
    base = dict(stop_kind=None, streak=3, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base, family_marginal={"enough_data": False})
    assert s["type"] == "continue", s


def test_strategy_refine_on_streak_plateau(tmp_path):
    """streak>=3 + 族内全局 plateau → refine（边际优先级更高）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    fam = _family_sketches(3)
    indep = _indep_sketches(47, seed=11)
    entries = ([{"ic_ir": 0.5 + 0.1 * (i % 2), "ic_series_sketch": s}
                for i, s in enumerate(indep)]
               + [{"ic_ir": 0.4, "ic_series_sketch": s} for s in fam])
    entries[1] = {**entries[1], "ic_ir": 0.6}
    _write_engine_trail_full(state, entries)
    loop = b._loop_directive()
    assert loop["family_streak"] == 3, loop
    assert loop["strategy"]["type"] in ("refine", "continue"), loop["strategy"]


def test_strategy_compose_when_unadmitted_passes(tmp_path):
    """pass 未入册 >=3 → compose。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    entries = [{"ic_ir": 0.3 + 0.01 * i, "verdict": "pass",
                "ic_series_sketch": s}
               for i, s in enumerate(_indep_sketches(4, seed=13))]
    _write_engine_trail_full(state, entries)
    loop = b._loop_directive()
    assert loop["strategy"]["type"] == "compose", loop["strategy"]


def test_strategy_query_on_accepted_milestone(tmp_path):
    """accepted=3 → query。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "registry.json").write_text(json.dumps([
        {"name": f"f{i}", "accepted": True} for i in range(3)
    ], ensure_ascii=False), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["strategy"]["type"] == "query", loop["strategy"]
    # 2026-09-18 死锁修复:query key 并入 agent_rounds(accepted 冻结时仍保新鲜)
    assert loop["strategy"]["key"].startswith("query:3:"), loop["strategy"]


def test_strategy_none_only_on_silent_terminals(tmp_path):
    """v8 分流：静默终态（finalize/fail_streak——convergence 已退役，
    2026-08-26 族收敛纯 must_rotate）strategy=None、注入器静默；
    方向预算停点（arc/簇/族收敛）strategy 强制 rotate/literature。"""
    from dsh_factor_mining.bridge import _pick_strategy
    base = dict(streak=3, pending_rejected=None, frozen=False,
                plateau=False, pass_unadmitted=0, accepted_n=0,
                agent_rounds=1, n_trials=1)
    # 静默终态 → None
    for kind in ("finalize", "fail_streak"):
        assert _pick_strategy(**base, stop_kind=kind) is None, kind
    # 方向预算 → 强制 rotate（族小无边际数据）或 literature（边际枯竭）
    s = _pick_strategy(**base, stop_kind="direction_budget")
    assert s is not None and s["type"] == "rotate", s
    s2 = _pick_strategy(**base, stop_kind="direction_budget",
                        family_marginal={"enough_data": True, "marginal": -0.15,
                                         "family_best": 0.8, "recent_best": 0.65})
    assert s2["type"] == "literature", s2
    # running → 常规优先级（无异常 → continue）
    s3 = _pick_strategy(**base, stop_kind=None)
    assert s3["type"] == "continue", s3


# ---- 4h. arc 方向段轮次（2026-08-25：轮次上限按方向段计，断链归零） ----

def test_arc_rounds_increment_on_trail_append(tmp_path):
    """一条叙事 trail = 当前方向段一轮；终身 rounds_total 分开计。"""
    b = _make_bridge(tmp_path)
    loop0 = b._loop_directive()
    assert loop0["round"] == 0 and loop0["rounds_total"] == 0
    for i in range(2):
        b.dispatch("paths.append", {"layer": "trail", "entry": {
            "round": i + 1, "signal": "x", "attribution": "y",
            "next_hypothesis": "测试维度A的构造", "new_information": "无"}})
    loop = b._loop_directive()
    assert loop["round"] == 2, loop
    assert loop["rounds_total"] == 2, loop
    assert loop["state"] == "running", loop


def test_arc_cap_is_direction_budget(tmp_path):
    """arc_rounds >= max_rounds → must_rotate（rotate 策略，非静默）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_rounds": 2, "arc_rounds": 2, "max_cluster_trials": 999,
        "fam_conv_window": 0, "finalized": False}), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    assert loop["stop_kind"] == "direction_budget", loop
    assert "方向段轮次上限" in loop["stop_reason"], loop["stop_reason"]
    assert loop["strategy"]["type"] == "rotate", loop["strategy"]
    assert loop["round"] == 2 and loop["rounds_total"] == 0, loop


def test_arc_resets_on_family_break(tmp_path):
    """家族链断（engine trail 追加不接尾链）→ arc_rounds 归零——
    与 cluster_trials 同一事件双归零（同一谓词 _entries_linked）。"""
    b = _make_bridge(tmp_path)
    fam = _family_sketches(2, seed=31)
    indep = _indep_sketches(1, seed=33)[0]
    b._append_engine_trail("primary", "h1", "development",
                           {"ic_ir_train": 0.2, "horizon": 20,
                            "ic_series_train": fam[0]}, None)
    b._append_engine_trail("primary", "h2", "development",
                           {"ic_ir_train": 0.2, "horizon": 20,
                            "ic_series_train": fam[1]}, None)  # 同族 → 不断
    for i in range(2):
        b.dispatch("paths.append", {"layer": "trail", "entry": {
            "round": i + 1, "signal": "x", "attribution": "y",
            "next_hypothesis": "测试族A变体", "new_information": "无"}})
    assert b._loop_directive()["round"] == 2
    # 异族试验追加 → 断链 → arc 归零（簇计数 streak 读时同样自然断）
    b._append_engine_trail("primary", "h3", "development",
                           {"ic_ir_train": 0.2, "horizon": 20,
                            "ic_series_train": indep}, None)
    loop = b._loop_directive()
    assert loop["round"] == 0, loop
    assert loop["rounds_total"] == 2, loop     # 终身叙事数不归零
    assert loop["cluster_trials"] == 1, loop   # 簇计数同步断链
    assert loop["state"] == "running", loop


def test_arc_cap_released_by_rotation(tmp_path):
    """arc 触顶 → must_rotate → 断链后回到 running（无人工干预）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    fam = _family_sketches(1, seed=41)
    indep = _indep_sketches(1, seed=43)[0]
    b._append_engine_trail("primary", "h1", "development",
                           {"ic_ir_train": 0.2, "horizon": 20,
                            "ic_series_train": fam[0]}, None)
    (state / "mining_state.json").write_text(json.dumps({
        "max_rounds": 2, "arc_rounds": 2, "max_cluster_trials": 999,
        "fam_conv_window": 0, "finalized": False}), encoding="utf-8")
    assert b._loop_directive()["state"] == "must_rotate"
    b._append_engine_trail("primary", "h2", "development",
                           {"ic_ir_train": 0.2, "horizon": 20,
                            "ic_series_train": indep}, None)  # 换向断链
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["round"] == 0, loop


def test_family_convergence_stops(tmp_path):
    """族内收敛（2026-08-26 v8：滑窗对滑窗搬进族内 + 纯 must_rotate）：
    同族前窗最佳 0.7，最近窗最佳 0.5 → 改善 −0.2 < 0.05 → 收敛。
    触发的是换向（direction_budget/must_rotate），不再是静默终态。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 4,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    sketches = _family_sketches(8)
    _write_engine_trail_full(state, [
        {"ic_ir": v, "ic_series_sketch": s}
        for v, s in zip([0.7, 0.7, 0.7, 0.7, 0.5, 0.5, 0.5, 0.5], sketches)])
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    assert loop["stop_kind"] == "direction_budget", loop
    assert "族内 IC 收敛" in loop["stop_reason"], loop["stop_reason"]
    assert "窗口最佳" in loop["stop_reason"]
    # 纯 must_rotate：有策略（rotate/literature）、注入器照常推进
    assert loop["strategy"] is not None, loop
    assert loop["strategy"]["type"] in ("rotate", "literature"), loop["strategy"]
    assert "换向" in loop["obligation"], loop["obligation"]
    # 遥测在场（PASS-FAIL：多维报告）
    fc = loop["family_convergence"]
    assert fc["enough_data"] and fc["family_size"] == 8, fc
    assert fc["recent_best"] == 0.5 and fc["prev_best"] == 0.7, fc


def test_family_convergence_not_fired_when_improving(tmp_path):
    """族内前窗 0.3，最近窗 0.7 → 改善 0.4 ≥ 0.05 → 未收敛，继续。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 4,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    sketches = _family_sketches(8)
    _write_engine_trail_full(state, [
        {"ic_ir": v, "ic_series_sketch": s}
        for v, s in zip([0.3, 0.3, 0.3, 0.3, 0.7, 0.3, 0.3, 0.3], sketches)])
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["obligation"] is not None


def test_family_convergence_no_lifetime_ratchet(tmp_path):
    """棘轮修正（同族内）：族历史最高 0.9 在更早位置，但前窗仅 0.4、
    最近窗 0.55 → 相对前窗改善 0.15 ≥ 0.05 → 不收敛。
    对照全历史最佳会被 0.9 棘轮永久压死。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 4,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    irs = [0.9, 0.1, 0.1, 0.1, 0.1, 0.4, 0.4, 0.4, 0.4,
           0.55, 0.5, 0.5, 0.5]
    sketches = _family_sketches(len(irs))
    _write_engine_trail_full(state, [
        {"ic_ir": v, "ic_series_sketch": s} for v, s in zip(irs, sketches)])
    loop = b._loop_directive()
    assert loop["state"] == "running", loop


# ---- 4e. 穷尽宣告拒收：写入口 ----

def test_surrender_hypothesis_rejected_on_write(tmp_path):
    b = _make_bridge(tmp_path)
    for nh in ("此 ETF 池的因子空间已完全穷尽",
               "Kyle 方向已完成，总结汇报",
               "要继续需要换池子或换频率",
               "9 个已入册因子代表 alpha 天花板",
               "等待用户决定是否重置"):
        with pytest.raises(BridgeError, match="被拒收"):
            b.dispatch("paths.append", {"layer": "trail", "entry": {
                "round": 1, "signal": "x", "attribution": "y",
                "next_hypothesis": nh, "new_information": "无"}})
    # 合法假设照常入账
    res = b.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 1, "signal": "x", "attribution": "y",
        "next_hypothesis": "测试 skew 维度与 amihud 的交互构造",
        "new_information": "sketch 维度"}})
    assert res["kind"] == "trail"


# ---- 4f. 穷尽宣告拒收：回显口（遗留脏 trail 不背书） ----

def test_surrender_pending_not_echoed(tmp_path):
    """session.jsonl 实测：停笔宣言混进 trail 后被引擎逐字回显，洗成
    引擎认可状态。v3：降级 pending_rejected，obligation 点名要求重写。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "trail.json").write_text(json.dumps([{
        "round": 1, "signal": "x", "attribution": "y",
        "next_hypothesis": "本轮探索完成。ETF 日频池因子空间已基本穷尽",
        "new_information": "无"}], ensure_ascii=False), encoding="utf-8")
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["pending_hypothesis"] is None, loop
    assert loop["pending_rejected"] is not None, loop
    assert "穷尽" in loop["pending_rejected"]["text"]
    assert loop["pending_rejected"]["reason"]
    assert "停笔宣言" in loop["obligation"], loop["obligation"]


# ---- 5. submit 拒绝路径：战略菜单 + running（不停下来问用户） ----

def test_submit_rejection_menu(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    # 弱因子：deflated p 必然拒绝
    sub = b.dispatch("registry.submit", {
        "name": "weak_probe", "signal": "weak",
        "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is False
    assert "loop" in sub and sub["loop"]["state"] == "running"
    nm = sub.get("next_moves")
    assert nm is not None, "拒绝响应缺 next_moves 菜单"
    assert any("结构性新假设" in o for o in nm["options"]), nm
    assert any("正交组合" in o for o in nm["options"]), nm
    assert any("假门" in f for f in nm["forbidden"]), nm
    assert any("停下来问用户" in f for f in nm["forbidden"]), nm
    # v3：穷尽宣告进 forbidden；数据轮换定位为用户操作（上报而非停笔）
    assert any("穷尽宣告" in f for f in nm["forbidden"]), nm
    assert any("用户操作" in f for f in nm["forbidden"]), nm


# ---- 6. batch / trail_summary 响应带 loop ----

def test_loop_on_batch_and_summary(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    res = b.dispatch("factor.evaluate_batch", {
        "envId": "primary", "horizon": 20,
        "sources": {
            "v1": BASELINE_SOURCE,
            "v2": BASELINE_SOURCE.replace("shift(20)", "shift(21)")}})
    assert "loop" in res, "batch 响应缺 loop 指令"
    # 近邻窗口变体（shift 20 vs 21）：IC 序列高相关 = 真实同族 → streak ≥ 2
    assert res["loop"]["family_streak"] >= 2, res["loop"]

    summary = b.dispatch("state.trail_summary", {})
    assert "loop" in summary, "trail_summary 缺 loop 指令"
    assert summary["loop"]["state"] == "running"


# ---- 2026-08-25 review 修复回归 ----

def test_family_convergence_dedups(tmp_path):
    """族内收敛窗口按 (source_hash, horizon) 去重（与 n_trials/bar_sigma
    同键）——跨 stage 重复条目不得灌窗口计数触发假收敛。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "fam_conv_window": 3, "fam_conv_delta": 0.05}), encoding="utf-8")
    sketches = _family_sketches(8)
    dup = [{"source_hash": "a", "horizon": 5, "ic_ir": 0.5,
            "ic_series_sketch": s} for s in sketches]
    fired, reason, diag = b._family_convergence(dup, {"fam_conv_window": 3,
                                                      "fam_conv_delta": 0.05})
    assert fired is False and diag["family_size"] == 1, diag  # 去重后 1 < 2W
    uniq = [{"source_hash": f"s{i}", "horizon": 5, "ic_ir": 0.5,
             "ic_series_sketch": s} for i, s in enumerate(sketches)]
    fired, reason, diag = b._family_convergence(uniq, {"fam_conv_window": 3,
                                                       "fam_conv_delta": 0.05})
    assert fired is True and reason is not None, (fired, reason, diag)


# ---- v8 族内收敛新增（2026-08-26 规划书） ----

def test_new_family_not_suppressed_by_old_peak(tmp_path):
    """核心动机：旧族峰值 0.9 不压新族——新族在改善中（族内 0.3→0.5）
    → 族收敛不触发（running）。全局收敛在此场景必触发（recent 0.5
    vs 前窗 max=0.9）——这正是被替换的行为。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 5,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    old_fam = _family_sketches(5, seed=21)          # 旧族：峰值族
    new_fam = _family_sketches(10, seed=33)         # 新族：与旧族独立
    entries = ([{"ic_ir": 0.9, "ic_series_sketch": s} for s in old_fam]
               + [{"ic_ir": v, "ic_series_sketch": s}
                  for v, s in zip([0.3] * 5 + [0.5] * 5, new_fam)])
    _write_engine_trail_full(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    fc = loop["family_convergence"]
    assert fc["family_size"] == 10 and fc["recent_best"] == 0.5, fc
    assert fc["prev_best"] == 0.3, fc               # 参考系 = 新族前一窗


def test_new_family_plateau_rotates(tmp_path):
    """对照：新族自身平台（族内 0.45→0.42）→ 族收敛触发 must_rotate
    （族内确实没改善，换向合理——不是被旧峰值压死）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 5,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    old_fam = _family_sketches(5, seed=21)
    new_fam = _family_sketches(10, seed=33)
    entries = ([{"ic_ir": 0.9, "ic_series_sketch": s} for s in old_fam]
               + [{"ic_ir": v, "ic_series_sketch": s}
                  for v, s in zip([0.45] * 5 + [0.42] * 5, new_fam)])
    _write_engine_trail_full(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    assert "族内 IC 收敛" in loop["stop_reason"], loop["stop_reason"]


def test_family_convergence_small_family_silent(tmp_path):
    """enough_data 门槛：族 < 2·Wf 条不判（小族由 marginal 软信号兜底），
    也不会因族小触发假收敛。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 20,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    sketches = _family_sketches(22)                 # 22 < 2×20（生产当前族规模）
    _write_engine_trail_full(state, [
        {"ic_ir": 0.4 if i < 11 else 0.1, "ic_series_sketch": s}
        for i, s in enumerate(sketches)])
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    fc = loop["family_convergence"]
    assert fc["enough_data"] is False and fc["family_size"] == 22, fc


def test_family_convergence_disabled_by_zero_window(tmp_path):
    """fam_conv_window ≤ 0 = 关闭（同旧全局版约定）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 0,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    sketches = _family_sketches(8)
    _write_engine_trail_full(state, [
        {"ic_ir": v, "ic_series_sketch": s}
        for v, s in zip([0.7] * 4 + [0.1] * 4, sketches)])
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["family_convergence"].get("disabled") is True


def test_family_convergence_released_by_chain_break(tmp_path):
    """断链解除：族收敛触发 must_rotate 后，新试验换源（独立 sketch）
    断链 → 新族从头累计 → 回 running。同族续推则门持续压着。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "mining_state.json").write_text(json.dumps({
        "max_cluster_trials": 999, "fam_conv_window": 4,
        "fam_conv_delta": 0.05, "finalized": False}), encoding="utf-8")
    fam = _family_sketches(8)
    _write_engine_trail_full(state, [
        {"ic_ir": v, "ic_series_sketch": s}
        for v, s in zip([0.7] * 4 + [0.5] * 4, fam)])
    assert b._loop_directive()["state"] == "must_rotate"
    # 换向：独立 sketch 的新试验断链（尾部族换成新单例；走真实追加路径
    # ——断链检测/arc 归零随之发生）
    b._append_engine_trail("primary", "newdir", "development",
                           {"ic_ir_train": 0.3, "horizon": 20,
                            "ic_series_train": _indep_sketches(1, seed=77)[0]},
                           None)
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["family_convergence"]["family_size"] == 1, loop


def test_trail_summary_cluster_counted(tmp_path):
    """trail_summary 的 termination 注入读时计算的 cluster_trials——
    此前直传盘上 mining（该键从不在盘上）恒显示「簇试验 0/200」，
    与 loop 指令口径不一致。"""
    b = _make_bridge(tmp_path)
    _write_engine_trail(tmp_path / "state", _family_sketches(3))
    s = b.dispatch("state.trail_summary", {})
    assert s["termination"]["state"]["cluster_trials"] == 3, s["termination"]
