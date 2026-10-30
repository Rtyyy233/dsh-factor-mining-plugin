# -*- coding: utf-8 -*-
"""周对账（票 3b，F6 配对定义）+ 重放 harness（票 5，F10 反事实池态）
+ 功效回填公式 + 跟随率统计。"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining import derivation, scheduler

NOW = time.strftime("%Y-%m-%dT%H:%M:%S")


def _score(root, hash_, a, rank=None):
    derivation.note_score(root, hash_, {"A": a}, rank=rank,
                          fn_version="v1.1")


def _expand(root, parent, child, gain, new_candidate=True,
            via_pick=False, ts=NOW):
    derivation.note_expand(
        root, child, parent, "variant", "declared", new_candidate=new_candidate,
        compute={"slots": 1}, extra={
            "gain": gain, "new_candidate": new_candidate,
            "dsr": {"sr": 1.5, "skew": 0.0, "kurt": 3.0, "n": 85},
            "pool_std": 0.25, "bar_sigma": 3.3, "n_trials": 120,
            "a_parent": None, "family": parent,
            **({"via_pick": "pick:x:1"} if via_pick else {})})


def test_reconcile_pairing_and_spearman(tmp_path):
    """F6 配对：决策时刻父态 A × 子−父增益；A 与增益正相关 → spearman>0。"""
    root = str(tmp_path)
    # 三个父态：A 高/中/低，增益随之
    for i, (a, g) in enumerate([(3.0, 0.5), (1.0, 0.1), (-1.0, -0.3)]):
        p = f"P{i}" * 8
        _score(root, p, a, rank=0 if a == 3.0 else None)
        _expand(root, p, f"C{i}" * 8, g)
    rep = scheduler.reconcile(root, window_days=7)
    arm = rep["pick_arm"]
    assert arm["n"] == 0, "无 pick 标记 → 全部进自由臂"
    free = rep["free_arm_excluded_from_power"]
    assert free["n"] == 3
    assert free["spearman"] == pytest.approx(1.0), \
        "构造完全单调——秩相关为 1"


def test_reconcile_arm_split_epsilon_excluded(tmp_path):
    """分臂：pick 消费的进主判据臂；自由派发臂（ε 硬配额语义）只对账
    不判效——不进同口径功效判据。"""
    root = str(tmp_path)
    _score(root, "PA" * 8, 2.0)
    _expand(root, "PA" * 8, "CA" * 8, 0.4, via_pick=True)
    _score(root, "PB" * 8, 1.0)
    _expand(root, "PB" * 8, "CB" * 8, 0.1)
    rep = scheduler.reconcile(root)
    assert rep["pick_arm"]["n"] == 1
    assert rep["free_arm_excluded_from_power"]["n"] == 1
    assert "不进同口径" in rep["note_free_arm"]


def test_reconcile_skips_unscored_parents(tmp_path):
    """无决策时刻父态 A（打分器未覆盖）→ 跳过不编造。"""
    root = str(tmp_path)
    _expand(root, "PX" * 8, "CX" * 8, 0.2)
    rep = scheduler.reconcile(root)
    assert rep["pick_arm"]["n"] + \
        rep["free_arm_excluded_from_power"]["n"] == 0


def test_reconcile_calibration_buckets(tmp_path):
    root = str(tmp_path)
    for i in range(10):
        p = f"Q{i}" * 8
        _score(root, p, float(i))
        _expand(root, p, f"D{i}" * 8, float(i) * 0.1)
    rep = scheduler.reconcile(root)
    cal = rep["free_arm_excluded_from_power"]["calibration"]
    assert len(cal) == 5, "五分位校准桶"
    v0 = cal["q0"]
    v4 = cal["q4"]
    assert v4 > v0, "构造单调——高分位实现增益更高（校准方向）"


def test_replay_counterfactual_pool(tmp_path):
    """F10：五策略×2预算；重放的墙=自身累计试验数的解析式；事件流
    自足（expand 快照的 dsr/池参数）。"""
    root = str(tmp_path)
    for i in range(8):
        _score(root, f"R{i}" * 8, float(i % 3))
        _expand(root, f"R{i}" * 8, f"E{i}" * 8, 0.05 * (i % 4))
    out = scheduler.replay(root)
    assert "error" not in out
    for strat in ("uniform", "score_max", "novelty", "near_miss",
                  "marginal_value"):
        assert strat in out["strategies"]
        assert "budget_0.5" in out["strategies"][strat]
    assert "不继承行为策略池态" in out["counterfactual_note"]


def test_replay_empty_stream_honest(tmp_path):
    out = scheduler.replay(str(tmp_path))
    assert "error" in out, "影子期前重放——诚实拒绝不编造"


def test_required_blocks_formula():
    """n ≈ ((z_{0.975}+z_{0.80})·σ/δ)²；σ_diff 用影子期实测回填。"""
    nb = scheduler.required_blocks(sigma_diff=0.10, delta=0.05)
    assert nb == pytest.approx(((1.959964 + 0.841621) * 2.0) ** 2, rel=0.01)
    assert scheduler.required_blocks(0.0, 0.05) == 0.0
    assert scheduler.required_blocks(0.1, 0.0) == float("inf")


def test_follow_rate_statistics(tmp_path):
    """排序先行实验验收：挂出首位（rank=0）后 48h 内被派发 → 跟随。"""
    root = str(tmp_path)
    _score(root, "F1" * 8, 2.0, rank=0)     # 挂出首位 → 被跟随
    _expand(root, None, "F1" * 8, 0.0)       # 该 hash 被派发
    _score(root, "F2" * 8, 1.0, rank=0)      # 挂出首位 → 未跟随
    fr = scheduler.follow_rate(root)
    assert fr["total"] == 2 and fr["followed"] == 1
    assert fr["rate"] == pytest.approx(0.5)
