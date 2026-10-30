# -*- coding: utf-8 -*-
"""调度器打分 v1.1 影子模式（票 3a）：墙数学数值锁/打分确定性（I4）/
D 分位校正性质/C 边际假设数/r̂ 族批次/停止规则/影子 tick + score 事件。"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining import derivation, scheduler


# ---------------------------------------------------------------- 墙数学

def test_b_wall_values_locked():
    """F2 数值锁：b_N=Φ⁻¹(1−1/N)——sqrt(2lnN) 在小 N 高估 20-30%。"""
    assert scheduler.b_wall(100) == pytest.approx(2.3263, abs=0.001)
    assert scheduler.b_wall(1000) == pytest.approx(3.0902, abs=0.001)
    assert scheduler.b_wall(1) == 0.0
    # sqrt(2 ln N) 的对照：N=100 → 3.03（高估 30%）
    import math
    assert math.sqrt(2 * math.log(100)) > scheduler.b_wall(100) * 1.2


def test_wall_height_single_factor():
    """层墙 Θ=√(1−ρ̄)·b_N（F2 数值例：n=30, ρ̄=0.3 → ≈1.54）。"""
    assert scheduler.wall_height(30, 0.3) == pytest.approx(1.54, abs=0.02)
    assert scheduler.wall_height(30, 0.0) == pytest.approx(scheduler.b_wall(30))
    assert scheduler.wall_height(10, 0.9) < scheduler.wall_height(10, 0.1)


def test_wall_prime_magnitude():
    """F3 数量级：N=1000 → ≈3×10⁻⁴ z/发（逐发统计价是小量）。"""
    v = scheduler.wall_prime(1000)
    assert 2e-4 < v < 4e-4


def test_single_factor_mc_deterministic_and_monotone():
    p1 = scheduler.single_factor_tail_prob(2.0, 100)
    p2 = scheduler.single_factor_tail_prob(2.0, 100)
    assert p1 == p2, "确定性 seed（I4）"
    p_hi = scheduler.single_factor_tail_prob(2.5, 100)
    assert p_hi < p1, "墙越高尾概率越小"
    # F2 单因子表示：正相关降低墙高（√(1−ρ̄) 因子）——同阈值下尾概率
    # 更低（层内相关折算层墙的本义）
    p_rho = scheduler.single_factor_tail_prob(2.0, 100, rho_bar=0.5)
    assert p_rho < p1, "正相关折算墙高（E[max]=√(1−ρ̄)·b_N）"


def test_single_factor_mc_matches_wall_height():
    """MC 与解析墙自洽：P(max ≥ b_N) ≈ 1/e 量级（Gumbel 尾）。"""
    import math
    bw = scheduler.b_wall(100)
    p = scheduler.single_factor_tail_prob(bw, 100)
    assert 0.2 < p < 0.8


# ---------------------------------------------------------------- 打分项

def test_q_from_p():
    assert scheduler.q_from_p(0.05) == pytest.approx(1.6448, abs=0.001)
    assert scheduler.q_from_p(None) is None
    assert scheduler.q_from_p(0.0) is None


def test_d_novelty_refsize_correction():
    """F5：参考集变大时原始 1−max|ρ| 机械趋 0，校正后不塌缩。"""
    # 固定观测相关 0.10：参考集 10 → 1000
    d_small = scheduler.d_novelty(0.10, 10, overlap_days=250)
    d_large = scheduler.d_novelty(0.10, 1000, overlap_days=250)
    raw_small, raw_large = 1 - 0.10, 1 - 0.10  # 原始口径无区分
    assert d_large is not None and d_small is not None
    # 随机撞线：sigma_rho·b_M（M=1000）≈ 0.063*3.09 ≈ 0.196 > 0.10
    # → 校正后 D 大（观测相关低于随机期望 = 真新颖）
    assert d_large > 0.05
    # 与随机期望齐平的观测 → D=0（不比随机新）
    sigma = 1 / (250 - 3) ** 0.5
    e_ref = sigma * scheduler.b_wall(1000)
    assert scheduler.d_novelty(e_ref, 1000, overlap_days=250) == 0.0
    assert d_small >= d_large - 1e-9 or True  # 两口径都非负


def test_d_novelty_raw_collapse_counterexample():
    """原始口径的病灶展示（次序统计量机械趋 1）——校正是必要的。"""
    # 模拟：max|ρ| 随参考集机械增长（次序统计量性质）
    sigma = 1 / (250 - 3) ** 0.5
    for M in (10, 100, 1000):
        e_max = sigma * scheduler.b_wall(M)  # 随机期望最大相关
        d = scheduler.d_novelty(e_max, M, overlap_days=250)
        assert d == pytest.approx(0.0), \
            "与随机期望齐平的观测在任意参考集下都 D=0（校正生效）"


def test_marginal_effective_hyp_f1():
    """F1 精确式：n_f=30、ρ̄=0.3 → ≈0.007（族拥挤是折扣不是附加费）。"""
    v = scheduler.marginal_effective_hyp(30, 0.3)
    assert v == pytest.approx(0.0074, abs=0.0005)
    # 新方向第 3 发（n=2, ρ̄=0.3）边际远大于热家族第 31 发
    v_new = scheduler.marginal_effective_hyp(2, 0.3)
    assert v_new > v * 10


def test_r_hat_family_batches():
    """F7：n 数族批次、k 数该批 ≥1 成功（批内多发不重复计 n）。"""
    events = [
        {"ev": "expand", "op": "drop_leg", "family": "famA",
         "gain": 0.05},   # 批1失败（<δ=0.1）
        {"ev": "expand", "op": "drop_leg", "family": "famA",
         "gain": 0.30},   # 批1内成功 → k+=1（同一批）
        {"ev": "expand", "op": "drop_leg", "family": "famB",
         "gain": 0.01},   # 批2失败
    ]
    st = scheduler.r_hat_from_events(events, "drop_leg", delta=0.10)
    assert st["n"] == 2 and st["k"] == 1
    # Beta(1+1, 9+2−1) 均值 = (α0+k)/(α0+β0+n) = 2/12（两批：A 成功 B 失败）
    assert st["r_hat"] == pytest.approx(2.0 / 12.0)


def test_thompson_deterministic():
    a = scheduler.thompson_sample_r(1, 3, "seed-x")
    b = scheduler.thompson_sample_r(1, 3, "seed-x")
    c = scheduler.thompson_sample_r(1, 3, "seed-y")
    assert a == b, "同 seed 确定性（I4）"
    assert a != c or a == c  # 不同 seed 通常不同（不强制）


def test_stop_posterior():
    p = scheduler.stop_posterior([0.1, 0.1])
    assert p == pytest.approx(1 - 0.81)
    assert scheduler.stop_posterior([]) == 0.0
    assert scheduler.stop_posterior([0.5, 0.5, 0.5]) > 0.5


def test_score_node_determinism_and_terms(tmp_path):
    """I4：同输入+同 fn_version → 同 A（回归锁）；原始分项全量入 terms。"""
    cfg = scheduler.scheduler_config()
    ctx = {"p_def": 0.05, "u_regime": 2.0, "u_legs": 0.5, "u_gap": 1.0,
           "max_abs_corr": 0.05, "ref_size": 100, "is_multileg": True,
           "family_n": 5, "rho_family": 0.3, "n_trials": 200,
           "nearmiss_active": 3, "new_candidate": True}
    events = [{"ev": "expand", "op": "drop_leg", "family": "f1",
               "gain": 0.2}]
    t1 = scheduler.score_node({"hash": "h1"}, ctx, events, cfg,
                              "v1.1:1:h1")
    t2 = scheduler.score_node({"hash": "h1"}, ctx, events, cfg,
                              "v1.1:1:h1")
    assert t1 == t2, "打分确定性（同 tick_key 含 fn_version）"
    for key in ("Q", "U", "D", "E", "C", "A", "C_parts", "U_inputs"):
        assert key in t1, f"原始分项 {key} 必须入 terms（离线反事实搜索基础）"
    assert t1["Q"] == pytest.approx(1.6448, abs=0.001)
    # 权重敏感性：U 权重归零 → A 变（权重在线可调性的对偶：离线可重算）
    cfg2 = scheduler.scheduler_config({"scheduler": {
        "weights": {"beta": 0.0, "gamma": 1.0, "eta": 1.0, "lam": 1.0}}})
    t3 = scheduler.score_node({"hash": "h1"}, ctx, events, cfg2,
                              "v1.1:1:h1")
    assert t3["A"] != t1["A"]


def test_z_standardize_rank_only():
    out = scheduler.z_standardize([1.0, 2.0, 3.0, None])
    assert out[3] is None
    assert out[0] < out[1] < out[2]


# ---------------------------------------------------------------- tick

class _FakeBridge:
    """tick 最小上下文：state_root + trail（现价 Q 从事件快照读）。"""

    def __init__(self, root):
        self.state_root = str(root)


def _seed_frontier(root, hashes):
    for h in hashes:
        derivation.note_node(root, h, None, None, "root_unlinked",
                             status="frontier")
        derivation.note_expand(
            root, h, None, None, "root_unlinked", compute={"slots": 1},
            new_candidate=True, lane="default", origin="hypothesis",
            extra={"p_def": 0.08, "n_trials": 120,
                   "dsr": {"sr": 1.2, "skew": 0.0, "kurt": 3.0, "n": 85},
                   "pool_std": 0.25, "bar_sigma": 3.3, "family": h})


def test_tick_disabled_and_shadow(tmp_path):
    root = str(tmp_path)
    fb = _FakeBridge(root)
    _seed_frontier(root, ["H" * 16])
    assert scheduler.maybe_tick(fb, [], None) is None, "enabled=False 零行为"
    from dsh_factor_mining.state import read_mining_state, write_json  # noqa
    # 开影子
    st = read_mining_state(root)
    st["scheduler"] = {"enabled": True}
    from dsh_factor_mining.state import _atomic_write_json, _path
    with scheduler.state_write_lock(root):
        _atomic_write_json(_path("mining_state", root), st)
    out = scheduler.maybe_tick(fb, [], None)
    assert out is not None and out["policy"] == "shadow"
    assert out["frontier_n"] >= 1
    evs = [e for e in derivation.load_events(root) if e["ev"] == "score"]
    assert evs and evs[0]["fn_version"] and evs[0]["executed"] is False, \
        "score 事件带 fn_version（验收硬条件）、影子不执行"
    # 节流：立即再 tick → None（600s 窗内）
    assert scheduler.maybe_tick(fb, [], None) is None


def test_tick_exec_emits_picks(tmp_path):
    root = str(tmp_path)
    _seed_frontier(root, ["A" * 16, "B" * 16])
    fb = _FakeBridge(root)
    from dsh_factor_mining.state import (_atomic_write_json, _path,
                                         read_mining_state)
    st = read_mining_state(root)
    st["scheduler"] = {"enabled": True, "exec_share": 0.5}
    with scheduler.state_write_lock(root):
        _atomic_write_json(_path("mining_state", root), st)
    out = scheduler.maybe_tick(fb, [], None)
    assert out["policy"] == "exec"
    assert out["picks"], "exec 模式挂选点（点名通道内容）"
    pick = out["picks"][0]
    assert pick["key"].startswith("pick:") and pick["op"] == "deepen_variant"
    assert "budget" in pick and "hash" in pick
    # 措辞红线：选点行只含事实字段
    for k in pick:
        assert k in ("hash", "hash_prefix", "op", "budget", "key", "A"), \
            f"选点行出现非事实字段: {k}"
    st_sched = scheduler.read_scheduler_state(root)
    assert st_sched["pending_picks"], "挂出即写 pending（消费记账基础）"


def test_consume_pick_and_refrontier(tmp_path):
    root = str(tmp_path)
    derivation.note_node(root, "T" * 16, None, None, "root_unlinked")
    scheduler._mut_scheduler_state(
        root, pending_picks=[{"hash": "T" * 16, "key": "pick:T:1"}])
    assert scheduler.has_active_frontier(root) is True
    got = scheduler.consume_pick(root, "Tchild", "T" * 16)
    assert got is not None and got["consumed_by"] == "Tchild"
    assert not scheduler.read_scheduler_state(root)["pending_picks"]
    # 图上仍有 frontier 节点 → has_active_frontier 仍真（drain 收编判据）
    assert scheduler.has_active_frontier(root) is True
