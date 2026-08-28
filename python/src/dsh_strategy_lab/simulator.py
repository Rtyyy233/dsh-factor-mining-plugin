# coding=utf-8
"""模拟器 v1（harness 独占——agent 只产 weights，成交/费用/T+1 全在这里）。

成交时点（harness 固定，非 agent 可选项）：t 收盘信号 → t+1 开盘成交。
不可成交（停牌 listed=False / 一字板 h==l 近似）：当日不动。顺延/撤单
不维护显式订单队列，由「每个开盘向**最新收盘目标**移动」等价承载：
信号持续（目标仍在）→ 后续开盘自动重试（顺延）；信号消失（目标≈
当前）→ 无 delta 无交易（撤单）。

费用（C1 默认档，config 可调；首批真实账本后校准定档）：
- 佣金 commission_bps（默认 2.5）按成交名义额计；
- 滑点 slippage_bps（默认 5）：买按 open×(1+s)、卖按 open×(1−s)；
- T+1：开盘批次执行下「当日买、当日卖」不存在（每 bar 恰一批成交），
  结构性满足；跨境 ETF T+0 属二档；
- 现金腿按年化 cash_rate_annual（默认 1.5%）日计（/年化因子，只对
  正现金计息）。

realism 阶梯（cost_model_version 进指纹与账本——跨成本档的结果不可比，
必须可区分）：
- v1（本档）：A 股 ETF 面板、T+1、无裸空/无杠杆（w≥0、Σw≤1，
  fail-closed）、分数份额、固定 bps 成本。
- 二档（未实现，改动 = version 变更）：跨境 ETF T+0、随流动性价差、
  整数手、融券裸空。

确定性：纯算术无 RNG——同输入重放逐位一致（测试锁死）；策略侧的
确定性由 contract.inject_seed + audit 双跑保证。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .contract import StrategyError


@dataclass(frozen=True)
class CostModel:
    """成本/ realism 档（frozen：version 由字段派生，任何字段变更 =
    新 cost_model_version = 新指纹 = 跨档不可比）。"""

    commission_bps: float = 2.5
    slippage_bps: float = 5.0
    cash_rate_annual: float = 0.015
    allow_short: bool = False
    max_leverage: float = 1.0
    limit_lock_mask: bool = True   # 一字板（h==l）不可成交近似

    @property
    def version(self) -> str:
        return (f"v1:comm{self.commission_bps:g}bps:slip{self.slippage_bps:g}"
                f"bps:cash{self.cash_rate_annual:.4f}"
                f":short{int(self.allow_short)}:lev{self.max_leverage:g}"
                f":lock{int(self.limit_lock_mask)}")


DEFAULT_COST_MODEL = CostModel()


def _ffill_close(c: np.ndarray) -> np.ndarray:
    """停牌缺口的前收填充（估值用；未上市首日仍 NaN → 0 兜底——
    未上市即无持仓，0 不进任何成交）。"""
    fc = pd.DataFrame(c).ffill().values
    return np.where(np.isfinite(fc), fc, 0.0)


def _policy_scan(weight_path: list[dict], env, cm: CostModel) -> None:
    """裸空/杠杆的 fail-closed 预检（v1 禁；带定位的可行动错误）。"""
    for t, w in enumerate(weight_path):
        gross = 0.0
        for sym, v in w.items():
            if not cm.allow_short and v < -1e-12:
                raise StrategyError(
                    f"第 {t} 项（{env.dates[t]}）资产 {sym} 权重 {v:.4f} < 0"
                    "——v1 禁裸空（realism 二档）；暴露缩放请用现金腿")
            gross += v
        if gross > cm.max_leverage + 1e-6:
            raise StrategyError(
                f"第 {t} 项（{env.dates[t]}）权重和 {gross:.4f} > "
                f"max_leverage={cm.max_leverage}——v1 无杠杆；余量请留现金腿")


def simulate(env, weight_path: list[dict],
             cost_model: CostModel | None = None,
             initial_capital: float = 1_000_000.0) -> dict:
    """权重路径 → 组合账本（equity 曲线 + 逐笔成交 + 指标）。

    输入 weight_path 已过 contract.validate_weight_path；本函数再过
    政策预检（裸空/杠杆）。输出全 JSON 友好（worker 往返无损）：
    equity / daily_returns / trades / turnover / metrics /
    cost_model_version。"""
    cm = cost_model or DEFAULT_COST_MODEL
    _policy_scan(weight_path, env, cm)
    T, N = env.T, env.N
    sym_idx = {s: j for j, s in enumerate(env.symbols)}
    o, h, l = env.o, env.h, env.l
    listed = env.listed if env.listed is not None else np.ones((T, N), bool)
    close_ff = _ffill_close(env.c)
    af = float(env.calibration.annualization or 252.0)
    comm = cm.commission_bps / 1e4
    slip = cm.slippage_bps / 1e4
    r_cash = cm.cash_rate_annual / af

    cash = float(initial_capital)
    shares = np.zeros(N)
    last_close = close_ff[0].copy()
    equity = np.empty(T)
    trades: list[dict] = []
    turnover_days: list[dict] = []
    total_fees = 0.0
    total_slip_cost = 0.0

    for t in range(T):
        # ---- t 开盘：向昨收目标 weight_path[t-1] 移动 ----
        if t >= 1:
            trades_before = len(trades)
            target = weight_path[t - 1]
            tradable = (listed[t]
                        & np.isfinite(o[t]) & (o[t] > 0)
                        & (np.isfinite(h[t]) & np.isfinite(l[t])))
            if cm.limit_lock_mask:
                tradable &= ~(h[t] == l[t])   # 一字板近似
            val_px = np.where(tradable, o[t], last_close)
            port_val = cash + float(shares @ val_px)
            sells: list[tuple[int, float, float]] = []   # (j, amt, price_exec)
            buys: list[tuple[int, float, float]] = []
            tgt = {sym_idx[s]: w for s, w in target.items()}
            for j in range(N):
                # 契约：不复述上一目标 = 减仓/清仓——持有但不在目标里的
                # 资产按 w=0 处理（缺席即清仓指令，不是「维持现状」）
                if j not in tgt and shares[j] == 0:
                    continue
                delta = tgt.get(j, 0.0) * port_val - shares[j] * val_px[j]
                if abs(delta) <= max(1e-9, 1e-9 * max(port_val, 1.0)):
                    continue                   # 无 delta = 撤单语义
                if not tradable[j]:
                    continue                   # 不可成交：信号持续则明日重试（顺延）
                if delta > 0:
                    buys.append((j, delta, o[t][j] * (1.0 + slip)))
                else:
                    sells.append((j, -delta, o[t][j] * (1.0 - slip)))

            day_notional = 0.0
            # 先卖后买（现金可用性；T+1：单批执行无当日回转）
            for j, amt, px in sells:
                qty = amt / px
                fee = amt * comm
                cash += amt - fee
                shares[j] -= qty
                day_notional += amt
                total_fees += fee
                total_slip_cost += amt * slip / (1.0 - slip)
                trades.append({"t": t, "date": str(env.dates[t]),
                               "asset": env.symbols[j], "side": "sell",
                               "qty": float(qty), "price": float(px),
                               "notional": float(amt), "fee": float(fee),
                               "scaled": False})
            if buys:
                need = sum(amt * (1.0 + comm) for _, amt, _ in buys)
                scale = min(1.0, cash / need) if need > 0 else 1.0
                for j, amt, px in buys:
                    amt_s = amt * scale
                    if amt_s <= 0:
                        continue
                    qty = amt_s / px
                    fee = amt_s * comm
                    cash -= amt_s + fee
                    shares[j] += qty
                    day_notional += amt_s
                    total_fees += fee
                    total_slip_cost += amt_s * slip / (1.0 + slip)
                    trades.append({"t": t, "date": str(env.dates[t]),
                                   "asset": env.symbols[j], "side": "buy",
                                   "qty": float(qty), "price": float(px),
                                   "notional": float(amt_s), "fee": float(fee),
                                   "scaled": bool(scale < 1.0)})
            if day_notional > 0 and port_val > 0:
                turnover_days.append({
                    "t": int(t), "date": str(env.dates[t]),
                    "turnover": float(day_notional / port_val / 2.0),
                    "n_trades": int(len(trades) - trades_before),
                })
        # ---- 现金腿计息（收盘；只对正现金）----
        if cash > 0 and r_cash != 0.0:
            cash += cash * r_cash
        # ---- 收盘估值 ----
        known = np.isfinite(env.c[t]) & (env.c[t] > 0)
        last_close = np.where(known, env.c[t], last_close)
        equity[t] = cash + float(shares @ last_close)

    # ---- 指标（多维报告，不坍缩——PASS-FAIL 教训）----
    eq = equity.tolist()
    rets = np.diff(equity) / equity[:-1] if T > 1 else np.array([])
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    mean_r = float(rets.mean()) if len(rets) else 0.0
    run_max = np.maximum.accumulate(equity)
    mdd = float(np.max(1.0 - equity / run_max)) if T else 0.0
    yrs = (T - 1) / af if T > 1 else 0.0
    ann_ret = ((equity[-1] / equity[0]) ** (1.0 / yrs) - 1.0
               if yrs > 0 and equity[0] > 0 else 0.0)
    turn_arr = np.array([d["turnover"] for d in turnover_days]) \
        if turnover_days else np.array([0.0])
    return {
        "equity": eq,
        "daily_returns": [float(x) for x in rets],
        "trades": trades,
        "turnover_days": turnover_days,
        "metrics": {
            "final_equity": float(equity[-1]),
            "total_return": float(equity[-1] / initial_capital - 1.0),
            "ann_return": float(ann_ret),
            "ann_vol": float(sd * math.sqrt(af)) if sd > 0 else 0.0,
            "sharpe": float(mean_r / sd * math.sqrt(af)) if sd > 0 else None,
            "max_drawdown": mdd,
            "n_trades": len(trades),
            "n_trade_days": len(turnover_days),
            "avg_turnover_per_trade_day": float(turn_arr.mean()),
            "total_fees": float(total_fees),
            "est_slippage_cost": float(total_slip_cost),
        },
        "cost_model_version": cm.version,
        "initial_capital": float(initial_capital),
    }
