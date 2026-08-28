# coding=utf-8
"""P4 门链测试（规划 §8.3-8.6）：placebo null 校准 / trade_minhash 聚类与
deflation bar 单调 / 增量 CI 符号 / artifact 门 fail-closed / robustness 报告。"""
import numpy as np
import pytest

from dsh_strategy_lab.audit import run_pipeline
from dsh_strategy_lab.contract import compile_strategy
from dsh_strategy_lab.gates import (
    TRADE_LINEAGE,
    deflation_bar,
    g1_placebo,
    g2_artifact,
    g3_deflation,
    trade_minhash,
    trade_n_eff,
)
from dsh_strategy_lab.increment import (
    g4_increment,
    increment_report,
    passive_topk_path,
)
from dsh_strategy_lab.placebo import matched_turnover_placebo, null_path_for_draw
from dsh_strategy_lab.robustness import param_flatness, startup_perturbation
from dsh_strategy_lab.simulator import simulate
from _strategy_fixtures import make_env

CAUSAL_MR = """
import numpy as np

def apply(state, env):
    out = []
    for t in range(env.T):
        w = {}
        if t >= 1:
            rets = env.c[t] / env.c[t - 1] - 1.0
            ok = np.isfinite(rets) & (env.c[t - 1] > 0)
            if ok.any():
                j = int(np.nanargmin(np.where(ok, rets, np.inf)))
                w[env.symbols[j]] = 1.0
        out.append(w)
    return out
"""

AR_KW = dict(T=80, N=6, seed=21, ar=-0.55)


def test_placebo_null_calibration_and_real_edge():
    """真信号 z 大、null 策略 z ≈ 0（合成面板上 z 已按 null std 归一）。"""
    env = make_env(**AR_KW)
    ns = compile_strategy(CAUSAL_MR)
    path = run_pipeline(ns, env, seed=42)
    out = matched_turnover_placebo(env, path, m=30, seed=7)
    assert out["z"] is not None and out["z"] > 3.0
    # null 校准：null 策略（本来就随机挑资产）的 z 应 ≈ 0（|z| < 3）
    RANDOM = """
import numpy as np

def apply(state, env):
    return [{env.symbols[np.random.randint(env.N)]: 1.0}
            for _ in range(env.T)]
"""
    ns_r = compile_strategy(RANDOM)
    path_r = run_pipeline(ns_r, env, seed=42)
    out_r = matched_turnover_placebo(env, path_r, m=30, seed=7)
    assert out_r["z"] is not None and abs(out_r["z"]) < 3.0


def test_null_path_matches_calendar_and_magnitudes():
    env = make_env(T=20, N=6, seed=3)
    base = [({"E0": 0.5, "E1": 0.3} if t % 4 == 0 else {})
            for t in range(env.T)]
    rng = np.random.default_rng(0)
    null = null_path_for_draw(base, list(env.symbols), rng)
    for t, (b, n) in enumerate(zip(base, null)):
        assert (not b) == (not n)                 # 同再平衡日历
        if b:
            assert sorted(b.values()) == sorted(n.values())  # 同幅值
            assert set(n) <= set(env.symbols)


def test_trade_minhash_clustering():
    """同交易聚类 / 异交易不聚（§8.4）。"""
    t1 = [{"t": d, "asset": f"E{d % 3}", "side": "buy"} for d in range(40)]
    t2 = [{"t": d, "asset": f"E{d % 3}", "side": "buy"} for d in range(40)]
    t3 = [{"t": d, "asset": f"E{(d + 1) % 3}", "side": "sell"} for d in range(40)]
    s1, s2, s3 = trade_minhash(t1), trade_minhash(t2), trade_minhash(t3)
    from dsh_factor_mining.factor.gates import selection_similarity
    assert selection_similarity(s1, s2) > 0.9     # 同交易
    assert selection_similarity(s1, s3) < TRADE_LINEAGE   # 异交易不聚
    n_eff, n = trade_n_eff([{"trade_mh": s1}, {"trade_mh": s2},
                            {"trade_mh": s3}])
    assert (n_eff, n) == (2, 3)
    # 无签名的旧条目按独立计（保守）
    n_eff2, _ = trade_n_eff([{"trade_mh": s1}, {}, {"trade_mh": s2}])
    assert n_eff2 == 2


def test_deflation_bar_monotonic_and_g1_floor():
    bars = [deflation_bar(n)["required_z"] for n in (1, 10, 100, 835)]
    assert bars == sorted(bars)                    # bar 对 N_eff 单调
    assert deflation_bar(1)["required_z"] == 3.0   # N_eff=1 → bar < 3 →
    # G1′ 保底生效（E[max](1)=0 + Φ⁻¹(0.95)=1.645 < 3）
    b835 = deflation_bar(835)
    assert b835["emax_sigma"] == pytest.approx(3.2035, abs=1e-3)
    assert b835["required_z"] > 3.0                # 大 N_eff 时 deflation 接管
    ok, msg, bar = g3_deflation(3.5, n_eff=1)
    assert ok and "保底" not in msg                # 3.5 ≥ 3 过（G1′ 保底）
    ok2, _, _ = g3_deflation(3.5, n_eff=835)
    assert not ok2                                 # 3.5 < 4.85 拒


def test_increment_known_sign_and_zero_overlay():
    """已知正 delta 的合成策略 CI 符号正确；零 overlay（策略=基线）
    delta ≈ 0 且不过门（§8.5）。基线用**反号信号**（动量）在均值回复
    面板上构造已知劣于策略的被动组合——增量方向由构造保证。"""
    env = make_env(**AR_KW)
    ns = compile_strategy(CAUSAL_MR)
    strat = run_pipeline(ns, env, seed=42)
    F_bad = np.diff(env.c, axis=0, prepend=env.c[:1])  # 昨日涨幅（动量 =
    base = passive_topk_path(F_bad, env, top_k=2,      # 面板上的反号信号
                             rebalance_every=1)
    inc = increment_report(env, strat, base)
    ok, msg = g4_increment(inc)
    assert ok, msg                                  # 真信号 vs 反号基线有增量
    assert inc["delta_sharpe"] > 0
    # 零 overlay：策略路径 == 基线路径 → delta≈0，不判过
    inc0 = increment_report(env, base, base)
    ok0, _ = g4_increment(inc0)
    assert not ok0
    assert abs(inc0["delta_sharpe"] or 0) < 1e-12
    assert inc0["strategy"]["sharpe"] == pytest.approx(inc0["baseline"]["sharpe"])


def test_g2_artifact_fail_closed():
    """随机游走门：z≥3 拒；不可判定不判过（§8.6 的门机制半边——真
    exploit 策略待 C4 校准期构造，v1 模拟器无已知成交漏洞）。"""
    ok, _ = g2_artifact({"z": 0.5})
    assert ok
    ok2, msg2 = g2_artifact({"z": 3.4})
    assert not ok2 and "G2′" in msg2
    ok3, msg3 = g2_artifact({"z": None})
    assert not ok3 and "不判过" in msg3
    # 集成半边：真策略在随机游走面板上 z < 3（P2 已测分布，这里测门链）


def test_g1_placebo_verdicts():
    ok, _ = g1_placebo({"z": 3.2, "m": 60})
    assert ok
    ok2, msg2 = g1_placebo({"z": 2.1, "m": 60})
    assert not ok2 and "G1′" in msg2
    ok3, msg3 = g1_placebo({"z": None})
    assert not ok3 and "无法判定" in msg3


def test_robustness_startup_and_flatness():
    env = make_env(T=90, N=5, seed=11, ar=-0.4)
    ns = compile_strategy(CAUSAL_MR)
    rob = startup_perturbation(ns, env, seed=42)
    assert rob["n_points"] >= 8
    assert rob["verdict"] == "reported"            # v1 软门
    assert "delta_median" in rob and "positive_frac" in rob
    # 平缓性：中心 + 邻居变体；坏邻居 skipped ≠ 失败
    variants = {"center": CAUSAL_MR,
                "v_k2": CAUSAL_MR,                 # 同源邻居（平）
                "broken": "def not_apply(x):\n    pass"}
    flat = param_flatness(variants, env, seed=42)
    assert flat["n_valid"] == 2
    assert "skipped" in flat["variants"]["broken"]
    assert flat["verdict"] == "reported"
