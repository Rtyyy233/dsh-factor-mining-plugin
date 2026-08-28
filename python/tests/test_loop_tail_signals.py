# coding=utf-8
"""loop 尾部遥测 + 族收敛/边际双轨化回归（2026-08-27 规划书 WS-B / D2）。

锁定行为：
- loop.tail 遥测块：n_trials_tail / n_eff（tail_ledger 派生）、bar
  （legacy 近似）、near_misses（spread_ir ∈ [bar−0.10, bar) 最近 ≤3 条）
- 族收敛双轨 AND：IC 平但 spread 在改善 → 不触发 must_rotate，
  escalation 改写分流文案；两线都平 → 触发（文案带双线数字）；
  spread 线条目 < 2·Wf → 只有 IC 线参与判定（回到单轨行为）
- _pick_strategy R1/R2：两线都枯竭才 literature/rotate，单线枯竭不强制
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, _pick_strategy  # noqa: E402
from dsh_factor_mining.factor.tailgate import (  # noqa: E402
    tail_near_misses)

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


def _family_sketches(n: int, seed: int = 7) -> list[list[float]]:
    rng = np.random.default_rng(seed)
    base = rng.standard_normal(64)
    return [list(0.95 * base + 0.05 * rng.standard_normal(64)) for _ in range(n)]


def _entry(i: int, sketch: list[float], *, ic_ir: float, spread_ir: float | None,
           periods: int = 100, ts: str | None = None) -> dict:
    e = {"ts": ts or f"2026-08-27T10:{i // 60:02d}:{i % 60:02d}",
         "envId": "primary", "source_hash": f"hash{i:04d}",
         "stage": "development", "horizon": 20, "ic_ir": ic_ir,
         "verdict": "pass", "red_flags": [],
         "ic_series_sketch": sketch}
    if spread_ir is not None:
        e["tail"] = {"k_frac": 0.2, "spread_ir": spread_ir,
                     "selection_mh": None,
                     "topn": {"periods": periods, "placebo_z": 1.0}}
    return e


def _write_trail(state_root: Path, entries: list[dict]) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "trail_engine.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _mining(state_root: Path, **kw) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    m = {"max_cluster_trials": 999, "fam_conv_window": 4,
         "fam_conv_delta": 0.05, "fam_conv_delta_tail": 0.05,
         "finalized": False}
    m.update(kw)
    (state_root / "mining_state.json").write_text(
        json.dumps(m), encoding="utf-8")


# ---- 1. loop.tail 遥测块 ----

def test_tail_telemetry_counts_and_cold_start(tmp_path):
    """直写带 tail 块的 engine trail → loop.tail.n_trials_tail / n_eff
    正确（无 selection_mh 的条目按独立试验计）；空 trail 冷启动不炸。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    sketches = _family_sketches(6)
    entries = [_entry(i, sketches[i], ic_ir=0.2, spread_ir=0.05 + 0.01 * i)
               for i in range(6)]
    # 两条无 tail 块的条目不计入尾部账本
    entries += [_entry(10 + i, _family_sketches(1, seed=50 + i)[0],
                       ic_ir=0.1, spread_ir=None) for i in range(2)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    tel = loop["tail"]
    assert tel["n_trials_tail"] == 6, tel
    assert tel["n_eff"] == 6, tel  # 无签名 → 保守独立计
    # 冷启动：空 trail → 计数 0 + bar None，不炸
    _write_trail(state, [])
    loop0 = b._loop_directive()
    assert loop0["tail"]["n_trials_tail"] == 0
    assert loop0["tail"]["near_misses"] == []


def test_tail_telemetry_near_misses(tmp_path):
    """spread_ir 落在 [bar−0.10, bar) 的条目 → near_misses 命中且 ≤3 条
    （按 ts 取最近）；窗外（远低于/已过线）不入选。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    sketches = _family_sketches(30, seed=3)
    # 30 条独立尾块（periods=100 → s0 解析式 0.1）定 bar，随后精确构造
    # 窗内/窗外条目
    entries = [_entry(i, sketches[i], ic_ir=0.05, spread_ir=0.0 + 0.005 * i)
               for i in range(30)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    tel = loop["tail"]
    bar = tel["bar"]
    assert bar is not None and isinstance(bar["value"], (int, float)), tel
    bar_v = float(bar["value"])
    # 纯函数对照：同一账本同一 bar 的窗内条目
    from dsh_factor_mining.factor.tailgate import tail_ledger
    expected = tail_near_misses(tail_ledger(entries), bar_v)
    assert len(expected) > 0, "窗内应有候选（0.005 步进 × 30 覆盖窗口）"
    got = tel["near_misses"]
    assert len(got) <= 3
    assert [m["hash_prefix"] for m in got] == \
        [m["hash_prefix"] for m in expected], (got, expected)
    for m in got:
        assert bar_v - 0.10 <= m["spread_ir"] < bar_v, (m, bar_v)
        assert m["gap"] > 0
    assert "优先构造变体" in tel["note"]


def test_tail_telemetry_on_evaluate_response(tmp_path):
    """evaluate 响应的 loop 自动携带 tail 遥测（status/evaluate/batch/
    trail_summary 同一 _loop_directive 源）。"""
    b = _make_bridge(tmp_path)
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": BASELINE_SOURCE,
                       "stage": "development"})
    tel = diag["loop"]["tail"]
    assert "n_trials_tail" in tel and "n_eff" in tel and "bar" in tel, tel
    summary = b.dispatch("state.trail_summary", {})
    assert "tail" in summary["loop"], summary["loop"].keys()


# ---- 2. 族收敛双轨 AND（D2） ----

def test_dual_track_ic_flat_tail_improving_no_rotate(tmp_path):
    """IC 滑窗平但 spread 改善 ≥ δ → 不触发 must_rotate；escalation 分流
    文案含双线数字；diag.ic_converged=True / tail.converged=False。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    _mining(state)
    sketches = _family_sketches(8)
    entries = [_entry(i, sketches[i], ic_ir=0.5,
                      spread_ir=0.2 if i < 4 else 0.6)
               for i in range(8)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    fc = loop["family_convergence"]
    assert fc["ic_converged"] is True, fc
    assert fc["tail"]["enough_data"] is True, fc
    assert fc["tail"]["converged"] is False, fc
    assert fc["tail"]["recent_best"] == 0.6 and fc["tail"]["prev_best"] == 0.2, fc
    assert loop["escalation"] is not None, loop
    assert "尾部线仍在改善" in loop["escalation"], loop["escalation"]
    assert "0.600" in loop["escalation"] and "0.200" in loop["escalation"]


def test_dual_track_both_flat_fires(tmp_path):
    """两线都平 → 触发 must_rotate；stop_reason 带双线数字。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    _mining(state)
    sketches = _family_sketches(8)
    entries = [_entry(i, sketches[i], ic_ir=0.5 if i < 4 else 0.5,
                      spread_ir=0.3)
               for i in range(8)]
    # IC：0.5 → 0.5（平）；spread：0.3 → 0.3（平）→ 双线收敛
    _write_trail(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    assert loop["stop_kind"] == "direction_budget", loop
    assert "双线收敛" in loop["stop_reason"], loop["stop_reason"]
    assert "spread" in loop["stop_reason"], loop["stop_reason"]


def test_dual_track_spread_short_ic_only(tmp_path):
    """spread 线条目 < 2·Wf → 只有 IC 线参与判定（回到现行单轨行为）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    _mining(state)
    sketches = _family_sketches(8)
    # 8 条 IC 全平（IC 收敛），但只有 3 条带 spread 块（3 < 2×4）→ 触发
    entries = [_entry(i, sketches[i], ic_ir=0.5,
                      spread_ir=0.6 if i >= 5 else None)
               for i in range(8)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    fc = loop["family_convergence"]
    assert fc["tail"]["enough_data"] is False, fc
    assert "族内 IC 收敛" in loop["stop_reason"], loop["stop_reason"]


def test_dual_track_delta_tail_zero_disables_tail_line(tmp_path):
    """fam_conv_delta_tail ≤0 = 尾部线退出判定（IC 单轨）。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    _mining(state, fam_conv_delta_tail=0)
    sketches = _family_sketches(8)
    entries = [_entry(i, sketches[i], ic_ir=0.5,
                      spread_ir=0.2 if i < 4 else 0.6)
               for i in range(8)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "must_rotate", loop
    fc = loop["family_convergence"]
    assert fc["tail"]["enough_data"] is False, fc


def test_dual_track_tail_improving_beats_ic_severe_marginal(tmp_path):
    """组合场景：IC 边际严重枯竭 + IC 收敛，但尾部线在改善 → 既不
    must_rotate 也不 literature——单线枯竭只出 escalation，策略分流。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    _mining(state)
    sketches = _family_sketches(8)
    entries = [_entry(i, sketches[i],
                      ic_ir=0.8 if i < 4 else 0.5,
                      spread_ir=0.2 if i < 4 else 0.6)
               for i in range(8)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    assert loop["state"] == "running", loop
    assert loop["strategy"]["type"] != "literature", loop["strategy"]
    esc = loop["escalation"]
    assert esc is not None and "尾部线" in esc, esc


# ---- 3. _family_marginal 双线 + _pick_strategy R1/R2 双轨 ----

def test_family_marginal_reports_tail_line(tmp_path):
    """diag 增 tail_marginal：族内 spread_ir 滑窗对族前史最佳。"""
    b = _make_bridge(tmp_path)
    sketches = _family_sketches(8)
    entries = [_entry(i, sketches[i],
                      ic_ir=0.8 if i < 3 else 0.5,
                      spread_ir=0.7 if i < 3 else 0.2)
               for i in range(8)]
    fm = b._family_marginal(entries)
    assert fm["enough_data"] is True
    tm = fm["tail_marginal"]
    assert tm["enough_data"] is True and tm["family_size"] == 8, tm
    assert tm["family_best_before"] == 0.7 and tm["recent_best"] == 0.2, tm
    assert tm["marginal"] == round(0.2 - 0.7, 4), tm


def test_family_marginal_tail_insufficient(tmp_path):
    """尾部线样本 <5 → tail_marginal.enough_data=False（R1/R2 回 IC 单轨）。"""
    b = _make_bridge(tmp_path)
    sketches = _family_sketches(8)
    entries = [_entry(i, sketches[i], ic_ir=0.5,
                      spread_ir=0.3 if i >= 6 else None)
               for i in range(8)]
    fm = b._family_marginal(entries)
    assert fm["tail_marginal"]["enough_data"] is False, fm


def _fam(m_ic: float, m_tail: float | None):
    d = {"enough_data": True, "marginal": m_ic,
         "family_best": 0.8, "recent_best": 0.8 + m_ic}
    if m_tail is not None:
        d["tail_marginal"] = {"enough_data": True, "marginal": m_tail,
                              "family_best": 0.6,
                              "recent_best": 0.6 + m_tail}
    else:
        d["tail_marginal"] = {"enough_data": False, "family_size": 0}
    return d


def test_strategy_r1_requires_both_lines():
    """IC -0.15 且尾部线无数据 → literature（单轨现行行为）；
    尾部线在场且未枯竭 → 不 literature。"""
    base = dict(stop_kind=None, streak=20, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base, family_marginal=_fam(-0.15, None))
    assert s["type"] == "literature", s
    s2 = _pick_strategy(**base, family_marginal=_fam(-0.15, 0.02))
    assert s2["type"] != "literature", s2
    s3 = _pick_strategy(**base, family_marginal=_fam(-0.15, -0.20))
    assert s3["type"] == "literature", s3
    assert "尾部" in s3["why"], s3["why"]


def test_strategy_r2_requires_both_lines():
    """IC -0.07：尾部线无数据 → rotate（现行）；尾部线 +0.01 → 不 rotate。"""
    base = dict(stop_kind=None, streak=10, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base, family_marginal=_fam(-0.07, None))
    assert s["type"] == "rotate", s
    s2 = _pick_strategy(**base, family_marginal=_fam(-0.07, 0.01))
    assert s2["type"] not in ("rotate", "literature"), s2


def test_strategy_direction_budget_dual_line():
    """direction_budget 分流的换向强度同样双轨化：两线严重枯竭才
    literature，单线（尾部改善）降级 rotate。"""
    base = dict(streak=3, pending_rejected=None, frozen=False,
                plateau=False, pass_unadmitted=0, accepted_n=0,
                agent_rounds=1, n_trials=1)
    s = _pick_strategy(**base, stop_kind="direction_budget",
                       family_marginal=_fam(-0.15, -0.15))
    assert s["type"] == "literature", s
    s2 = _pick_strategy(**base, stop_kind="direction_budget",
                        family_marginal=_fam(-0.15, 0.05))
    assert s2["type"] == "rotate", s2


def test_marginal_escalation_dual_line_text(tmp_path):
    """escalation 文案双线：两线都枯竭 → IC 文案 + 尾部线数字随附；
    单线（IC 枯竭、尾部未枯竭）→ 分流文案不强制换向。"""
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    _mining(state, fam_conv_window=0)  # 关收敛，只测 marginal
    sketches = _family_sketches(8)
    # W=5：IC 族前史最佳 0.8（前 3 条）→ 最近窗 0.5（边际 -0.3 严重枯竭）；
    # spread 族前史 0.7 → 最近窗 0.2（边际 -0.5 同枯竭）→ 双线都枯竭
    entries = [_entry(i, sketches[i],
                      ic_ir=0.8 if i < 3 else 0.5,
                      spread_ir=0.7 if i < 3 else 0.2)
               for i in range(8)]
    _write_trail(state, entries)
    loop = b._loop_directive()
    assert "尾部线边际" in loop["escalation"], loop["escalation"]
    assert loop["strategy"]["type"] == "literature", loop["strategy"]
    # 单线：IC 枯竭、尾部改善 → 分流文案，不强制换向/文献
    entries2 = [_entry(i, sketches[i],
                       ic_ir=0.8 if i < 3 else 0.5,
                       spread_ir=0.2 if i < 3 else 0.5)
                for i in range(8)]
    _write_trail(state, entries2)
    loop2 = b._loop_directive()
    assert "未同枯竭" in loop2["escalation"], loop2["escalation"]
    assert "不强制" in loop2["escalation"], loop2["escalation"]
    assert loop2["strategy"]["type"] != "literature", loop2["strategy"]


if __name__ == "__main__":
    import tempfile
    fns = [test_tail_telemetry_counts_and_cold_start,
           test_tail_telemetry_near_misses,
           test_tail_telemetry_on_evaluate_response,
           test_dual_track_ic_flat_tail_improving_no_rotate,
           test_dual_track_both_flat_fires,
           test_dual_track_spread_short_ic_only,
           test_dual_track_delta_tail_zero_disables_tail_line,
           test_dual_track_tail_improving_beats_ic_severe_marginal,
           test_family_marginal_reports_tail_line,
           test_family_marginal_tail_insufficient,
           test_strategy_r1_requires_both_lines,
           test_strategy_r2_requires_both_lines,
           test_strategy_direction_budget_dual_line,
           test_marginal_escalation_dual_line_text]
    for fn in fns:
        if fn.__code__.co_varnames[:1] == ("tmp_path",):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
        else:
            fn()
        print(f"PASS {fn.__name__}")
    print("LOOP_TAIL_SIGNALS_TESTS PASS")
