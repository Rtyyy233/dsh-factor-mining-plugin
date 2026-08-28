# coding=utf-8
"""P1 模拟器测试（规划 §8.2）：T+1 / 一字板顺延与撤单 / 现金腿计息 /
费用算术 / 同输入重放逐位一致 / 裸空与杠杆 fail-closed。"""
import json
import math

import pytest

from dsh_strategy_lab.contract import StrategyError
from dsh_strategy_lab.simulator import CostModel, simulate
from _strategy_fixtures import const_path, make_env

I = 1_000_000.0


def test_cash_leg_interest_exact_on_all_cash():
    """flat 面板 + 永远空仓：equity[t+1]/equity[t] == 1+r（日计息）。"""
    env = make_env(T=15, N=2, flat=True)
    res = simulate(env, const_path(env, {}), CostModel(cash_rate_annual=0.015))
    r = 0.015 / 252.0
    eq = res["equity"]
    for t in range(len(eq) - 1):
        assert eq[t + 1] == pytest.approx(eq[t] * (1.0 + r), rel=1e-12)
    assert eq[0] == pytest.approx(I * (1.0 + r), rel=1e-12)
    assert res["metrics"]["n_trades"] == 0


def test_fee_arithmetic_full_allocation():
    """flat 面板 + 满仓单一资产：费用感知缩放（buy 降尺度留出佣金）+
    滑点内嵌价格；终值 = I/((1+comm)(1+slip)) 逐位可算（零利率隔离费用）。"""
    env = make_env(T=12, N=2, flat=True)
    cm = CostModel(commission_bps=2.5, slippage_bps=5.0,
                   cash_rate_annual=0.0)
    res = simulate(env, const_path(env, {"E0": 1.0}), cm, initial_capital=I)
    assert res["metrics"]["n_trades"] == 1
    tr = res["trades"][0]
    assert tr["side"] == "buy" and tr["asset"] == "E0" and tr["t"] == 1
    assert tr["scaled"] is True          # 满仓需求 > 现金（费差），按现金缩放
    comm, slip = 2.5e-4, 5e-4
    amt = I / (1.0 + comm)               # 缩放后的成交名义额
    assert tr["notional"] == pytest.approx(amt, rel=1e-12)
    assert tr["price"] == pytest.approx(10.0 * (1.0 + slip), rel=1e-12)
    assert tr["fee"] == pytest.approx(amt * comm, rel=1e-12)
    assert tr["qty"] == pytest.approx(amt / (10.0 * (1.0 + slip)), rel=1e-12)
    # 终值：份额 × flat 收盘价（滑点+佣金即一次性成本）
    assert res["equity"][-1] == pytest.approx(
        I / ((1.0 + comm) * (1.0 + slip)), rel=1e-9)
    assert res["metrics"]["total_fees"] == pytest.approx(amt * comm, rel=1e-9)


def test_limit_lock_defers_then_fills_and_cancels():
    """一字板（h==l）：信号持续 → 锁死日零成交、次一开盘顺延成交；
    信号消失 → 撤单（此后零成交）。"""
    # 顺延：bar1 锁死，目标持续 → bar1 零成交、bar2 开盘成交
    env = make_env(T=8, N=2, seed=3, lock_bars={(1, 0): True})
    res = simulate(env, const_path(env, {"E0": 0.5}))
    trades_e0 = [tr["t"] for tr in res["trades"] if tr["asset"] == "E0"]
    assert 1 not in trades_e0            # 锁死日不成交
    assert trades_e0 and trades_e0[0] == 2   # 首笔顺延到次一开盘
    # 撤单：bar1 锁死，目标只在 close0 出现一次 → 永不成交
    path = [{"E0": 0.5}] + [{} for _ in range(env.T - 1)]
    res2 = simulate(env, path)
    assert res2["metrics"]["n_trades"] == 0


def test_suspension_defers_like_limit_lock():
    """停牌（listed=False + NaN）同顺延语义。"""
    env = make_env(T=8, N=2, seed=5, unlisted_bars={(1, 0): True})
    res = simulate(env, const_path(env, {"E0": 0.5}))
    trades_e0 = [tr["t"] for tr in res["trades"] if tr["asset"] == "E0"]
    assert 1 not in trades_e0
    assert trades_e0 and trades_e0[0] == 2


def test_t1_one_batch_per_bar_no_same_day_round_trip():
    """T+1 结构性满足：每 bar 恰一批成交——同资产同 bar 绝不双向成交；
    昨买今卖（不同交易日）放行。"""
    env = make_env(T=14, N=2, seed=9)
    path = [({"E0": 1.0} if t % 2 == 0 else {}) for t in range(env.T)]
    res = simulate(env, path)
    by_bar = {}
    for tr in res["trades"]:
        by_bar.setdefault((tr["t"], tr["asset"]), set()).add(tr["side"])
    assert all(len(sides) == 1 for sides in by_bar.values())
    # 交替买卖确实发生在相邻开盘（昨买今卖 = T+1 合法）
    t0_side = {tr["t"]: tr["side"] for tr in res["trades"]}
    assert t0_side.get(1) == "buy" and t0_side.get(2) == "sell"


def test_turnover_full_switch_is_one():
    """满仓切换：单日单边换手 ≈ 1。"""
    env = make_env(T=25, N=3, seed=13)
    path = [({"E0": 1.0} if t < 10 else {"E1": 1.0}) for t in range(env.T)]
    res = simulate(env, path)
    assert res["metrics"]["n_trade_days"] >= 1
    sw = [d for d in res["turnover_days"] if d["t"] == 11]
    assert len(sw) == 1
    assert 0.95 < sw[0]["turnover"] <= 1.0 + 1e-9


def test_replay_bitwise_identical():
    env = make_env(T=40, N=4, seed=17)
    path = [({"E0": 0.4, "E2": 0.3} if t % 5 < 3 else {"E1": 0.6})
            for t in range(env.T)]
    a = simulate(env, path)
    b = simulate(env, path)
    assert a == b
    assert json.dumps(a) == json.dumps(b)   # JSON 往返稳定（worker 无损）


def test_policy_scan_short_and_leverage_fail_closed():
    env = make_env(T=10, N=2, flat=True)
    with pytest.raises(StrategyError, match="E0.*权重.*< 0"):
        simulate(env, const_path(env, {"E0": -0.3}))
    with pytest.raises(StrategyError, match="权重和"):
        simulate(env, const_path(env, {"E0": 0.7, "E1": 0.5}))
    # allow_short 放开后负权重可跑（realism 二档预研路径）
    res = simulate(env, const_path(env, {"E0": -0.3}),
                   CostModel(allow_short=True, max_leverage=2.0))
    assert res["metrics"]["n_trades"] >= 1


def test_cost_model_version_changes_with_any_field():
    v = CostModel().version
    assert v != CostModel(commission_bps=3.0).version
    assert v != CostModel(slippage_bps=6.0).version
    assert v != CostModel(cash_rate_annual=0.02).version
    assert v != CostModel(limit_lock_mask=False).version
    assert v == CostModel().version
    assert "v1" in v
