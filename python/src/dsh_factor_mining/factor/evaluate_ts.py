# -*- coding: utf-8 -*-
"""单品种时序因子评估（factor-mining-futures 线，2026-11-07 任务书 §四）。

IC 序列定义（任务书 §四，ts 线诊断同形的承重墙）：

    ic_t = z(信号_t) × 前向收益_{t→t+h}    （每 h 日一发，不重叠采样）

- z(信号)：**point-in-time 滚动标准化**（过去 TS_Z_WINDOW bar 的均值/
  标准差，最少 TS_Z_MIN bar；常数属管线定义，非因子可调参数）。IR 与
  t 统计量对正仿射缩放不变——标准化不影响门判定，只保证跨因子的池分布
  （pool_std）在同一量纲下比较。**禁止全窗口标准化**（commissioning
  实证 2026-11-07：全窗 z 与非重叠采样行相乘时，未来采样行的信号窗
  包含当前行的前向收益，减窗均值诱导机械负相关——纯随机游走上 120 日
  动量 IR 系统性 −0.04σ、噪声门 z=−4 误杀动量族；PIT 滚动 z 无此构造）。
- 前向收益：与截面线同执行语义（t1=收盘信号→次开入场→t+h 收盘出场；
  t0=信号收盘即入场）。fwd 不做 rank——单序列无截面。
- G-limit：信号日前向窗 (t, t+h] 含涨跌停 bar 的观测从序列剔除，
  计数 execution_masked_n（任务书 §三.2；完整执行模拟挂账二期）。

诊断同形：evaluate_ts 产出的键集与截面 evaluate() 对齐——现役消费面
（discipline 红牌/deflated 充分统计量/submit 重算/trail sketch/CRN 墙）
全部按原键工作：
- column_perm_train 键位由 ts 结构显著检验（ts_null：循环块置换，
  块长=h 保信号自相关）占用——discipline 的 |z|<3 fail 语义原样成立。
- beta_exposure=None（无截面 beta 概念）；topn 键位由 position_sim
  （sign(F) 仓位模拟毛/净 IR+换手，标注不门）占用。

红线：本模块只做纯计算——一切数字面向引擎内部与人读面（诊断对象），
模型面零样本外数字；walk_forward t1 钳制 ≤ dev_end。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .env import TsEnv

# 停板近似：|日收益| ≥ limit_pct×0.95 视为涨跌停 bar（v1 日收盘口径，
# 任务书 §三.3——结算价/一字板精细判定挂账）
LIMIT_TOL = 0.95
# z(信号) 的 PIT 滚动窗（bar 数）：均值/标准差只用过去（含当日）——
# 见模块 docstring 的全窗口标准化机械偏实证。常数=管线定义。
TS_Z_WINDOW = 250
TS_Z_MIN = 60


def _pit_z(F: np.ndarray) -> np.ndarray:
    """信号 point-in-time 滚动 z（过去 TS_Z_WINDOW bar，最少 TS_Z_MIN）。

    输出 NaN 的行=历史不足（暖机期），由调用方按有限性剔除。"""
    s = pd.Series(F)
    mu = s.rolling(TS_Z_WINDOW, min_periods=TS_Z_MIN).mean()
    sd = s.rolling(TS_Z_WINDOW, min_periods=TS_Z_MIN).std()
    return ((s - mu) / sd).values


# ---------------------------------------------------------------------------
# 基础原语：(T,) 版 forward/pit/limit
# ---------------------------------------------------------------------------

def ts_forward_returns(env: TsEnv, horizon: int | None = None) -> np.ndarray:
    """(T,) 前向收益，语义与截面 _forward_returns 逐条对齐：
    t1（默认）: close[t+h]/open[t+1] - 1；t0: close[t+h]/close[t] - 1。
    尾部 h 根不可得 → NaN。horizon 缺省=calibration.horizon。"""
    cal = env.calibration
    h = int(horizon) if horizon else int(cal.horizon)
    c = pd.Series(env.c)
    if cal.execution == "t0":
        return (c.shift(-h) / c - 1.0).values
    return (c.shift(-h) / pd.Series(env.o).shift(-1) - 1.0).values


def ts_pit_mask(env: TsEnv) -> np.ndarray:
    """(T,) 可交易掩码：价格 finite（t1 另要求次开 finite）。"""
    m = np.isfinite(env.c) & np.isfinite(env.o) & np.isfinite(env.v)
    if env.calibration.execution != "t0":
        m &= np.isfinite(pd.Series(env.o).shift(-1).values)
    return m


def limit_hit_bars(env: TsEnv) -> np.ndarray:
    """(T,) 涨跌停 bar 近似：|c_t/c_{t-1} - 1| ≥ limit_pct×LIMIT_TOL。

    增量复权序列的日收益在合约段内与原始一致（乘性常数在比值中消去），
    换月日收益被复权抹平不会误触发——日收盘口径下该近似可用。"""
    c = env.c
    prev = np.full_like(c, np.nan)
    prev[1:] = c[:-1]
    r = c / prev - 1.0
    th = float(env.limit_pct) * LIMIT_TOL
    return np.isfinite(r) & (np.abs(r) >= th)


def _sample_step(env: TsEnv, horizon: int | None) -> int:
    """不重叠采样步长（与截面 sample_step 语义同款：max(ise, h)）。"""
    h = int(horizon) if horizon else int(env.calibration.horizon)
    ise = int(env.calibration.ic_sample_every or 0)
    return max(ise, h) if ise > 0 else h


# ---------------------------------------------------------------------------
# IC 序列（诊断与段验证共用定义）
# ---------------------------------------------------------------------------

def ts_product_series(F, env: TsEnv, horizon: int | None = None,
                      end_date: str | None = None,
                      start_date: str | None = None) -> pd.Series:
    """IC 序列：z(信号) × 前向收益，每 h 日一发（不重叠），G-limit 剔除。

    - horizon：per-call horizon（None=主 horizon）；采样步长=max(ise,h)
    - end_date（闭）：右界（诊断传 dev_end；段验证传 None=全面板）
    - start_date（闭）：左界（段验证分段用；缺省=起点）

    信号暖机期（滚动 z 历史不足）与滚动零离散的行 → NaN 剔除；
    全空 → 空序列（调用方按样本不足处理）。
    """
    h = int(horizon) if horizon else int(env.calibration.horizon)
    if h <= 0:
        raise ValueError(f"horizon 必须 > 0（收到 {h}）")
    F = np.asarray(F, dtype=np.float64)
    if F.ndim != 1 or F.shape[0] != env.T:
        raise ValueError(f"ts 因子输出必须 (T,)={env.T}（收到 {F.shape}）")
    step = _sample_step(env, horizon)
    fwd = ts_forward_returns(env, h)
    limit = limit_hit_bars(env)
    hit_any = np.cumsum(limit, dtype=np.int64)
    dates = pd.DatetimeIndex(env.dates)
    lo = 0 if start_date is None else int(np.searchsorted(
        dates, pd.Timestamp(start_date), side="left"))
    hi = env.T if end_date is None else int(np.searchsorted(
        dates, pd.Timestamp(end_date), side="right"))
    Fz = _pit_z(F)          # PIT 滚动 z（全窗标准化的机械偏见模块 docstring）
    # 向量化行选择（h=1 时采样行≈T，逐行 Python 循环会拖慢置换 null×200 遍）
    rows = np.arange(lo, hi, step)
    rows = rows[rows + h < env.T]
    ok = (np.isfinite(Fz[rows]) & np.isfinite(fwd[rows])
          & ((hit_any[rows + h] - hit_any[rows]) == 0))   # G-limit 剔除
    pos = rows[ok]
    if pos.size == 0:
        return pd.Series(dtype=np.float64)
    idx = dates[pos]
    return pd.Series(Fz[pos] * fwd[pos], index=idx)


def ts_product_series_full(F, env: TsEnv, horizon: int | None = None,
                           end_date: str | None = None,
                           start_date: str | None = None) -> tuple[pd.Series, dict]:
    """ts_product_series + 执行遥测（n_candidates/execution_masked_n）。"""
    h = int(horizon) if horizon else int(env.calibration.horizon)
    step = _sample_step(env, horizon)
    limit = limit_hit_bars(env)
    hit_any = np.cumsum(limit, dtype=np.int64)
    dates = pd.DatetimeIndex(env.dates)
    lo = 0 if start_date is None else int(np.searchsorted(
        dates, pd.Timestamp(start_date), side="left"))
    hi = env.T if end_date is None else int(np.searchsorted(
        dates, pd.Timestamp(end_date), side="right"))
    rows = np.arange(lo, hi, step)
    rows = rows[rows + h < env.T]
    n_cand = int(rows.size)
    masked = int(((hit_any[rows + h] - hit_any[rows]) > 0).sum())
    s = ts_product_series(F, env, horizon=horizon, end_date=end_date,
                          start_date=start_date)
    telem = {"n_candidates": n_cand, "execution_masked_n": masked,
             "n_valid": int(len(s)), "sample_step": step}
    return s, telem


def series_ir(s: pd.Series) -> float:
    """序列 IR（mean/std, ddof=1）；<2 观测或零离散 → NaN。"""
    if len(s) < 2:
        return float("nan")
    sd = float(s.std(ddof=1))
    if not (sd > 0):
        return float("nan")
    return float(s.mean()) / sd


# ---------------------------------------------------------------------------
# ts 结构显著（ts_null：循环块置换，块长=h）+ date-shift placebo
# ---------------------------------------------------------------------------

def ts_block_perm_null(F, env: TsEnv, horizon: int | None = None,
                       n_perm: int = 200, seed: int = 42,
                       end_date: str | None = None,
                       real_series: pd.Series | None = None) -> dict:
    """循环块置换 null（任务书 §四：column_perm_train 键位的 ts 占用者）。

    null 机制：把信号切成块长 h 的整块并随机重排块序（circular block
    permutation——保块内自相关结构，破坏跨块时间对齐），与真实前向收益
    重配；PIT 滚动 z 随置换重算。检验「信号时间对齐是否携带超出自相关
    结构的增量」。

    实现注记（commissioning 2026-11-07）：曾误实现为整序列循环轮转——
    慢因子（周期 ≫ h）轮转后近似自身，null≈real、检验力坍塌（真信号
    z 仅 ~1-2.6）。块重排才是「保块内、破块间」的正确语义；采样步长
    ≥ h 使每块至多一个采样行，块重排经滚动 z 的跨块混合仍非平凡。
    （段内置换=统计恒等的教训不适用：这里跨块打破对齐，不是段内标签。）

    返回与截面 column-perm 同形：{z, p, null_mean, method}——
    discipline verdict 的 |z|<3 fail 语义原样消费。"""
    h = int(horizon) if horizon else int(env.calibration.horizon)
    end = end_date or env.calibration.dev_end
    real = (real_series if real_series is not None
            else ts_product_series(F, env, horizon=horizon, end_date=end))
    if len(real) < 5 or not (float(real.std(ddof=1) or 0.0) > 0):
        return {"z": float("nan"), "p": float("nan"), "null_mean": None,
                "method": "ts_block_perm", "n_perm": 0,
                "note": "样本不足或零离散"}
    Fv = np.asarray(F, dtype=np.float64)
    T = env.T
    n_blocks = int(math.ceil(T / h))
    # 自然块网格（非环绕）：块 i = 行 [i·h, min((i+1)·h, T))——尾块短块
    # 作为整体参与重排，拼接后长度恰为 T（环绕拼接会重复行、膨胀长度）
    blocks = [np.arange(i * h, min((i + 1) * h, T)) for i in range(n_blocks)]
    rng = np.random.default_rng(seed)
    null_irs = []
    for _ in range(n_perm):
        order = rng.permutation(n_blocks)
        Fp = np.concatenate([Fv[blocks[int(i)]] for i in order])
        sp = ts_product_series(Fp, env, horizon=horizon, end_date=end)
        ir = series_ir(sp)
        if np.isfinite(ir):
            null_irs.append(ir)
    if len(null_irs) < 20 or not (float(np.std(null_irs, ddof=1) or 0.0) > 0):
        return {"z": float("nan"), "p": float("nan"),
                "null_mean": (float(np.mean(null_irs))
                              if null_irs else None),
                "method": "ts_block_perm", "n_perm": len(null_irs),
                "note": "null 样本不足"}
    arr = np.asarray(null_irs)
    obs = series_ir(real)
    z = (obs - float(arr.mean())) / float(arr.std(ddof=1))
    p = math.erfc(abs(z) / math.sqrt(2))
    return {"z": float(z), "p": float(p), "null_mean": float(arr.mean()),
            "null_std": float(arr.std(ddof=1)), "method": "ts_block_perm",
            "n_perm": len(null_irs), "n_obs": int(len(real))}


def ts_date_shift_placebo(F, env: TsEnv, horizon: int | None = None,
                          shifts=(5, 10, 21)) -> dict:
    """date-shift placebo（附诊断，非门）：信号整体后移 k bar——真因子
    的 IR 应随 shift 崩向 0；shift 后仍显著=拟合慢变量巧合。"""
    h = int(horizon) if horizon else int(env.calibration.horizon)
    end = env.calibration.dev_end
    Fv = np.asarray(F, dtype=np.float64)
    base = series_ir(ts_product_series(Fv, env, horizon=h, end_date=end))
    out = {}
    for k in shifts:
        if k >= env.T:
            continue
        Fs = np.full_like(Fv, np.nan)
        Fs[k:] = Fv[:-k]
        ir = series_ir(ts_product_series(Fs, env, horizon=h, end_date=end))
        out[str(k)] = round(ir, 4) if np.isfinite(ir) else None
    return {"unshifted_ir": (round(base, 4) if np.isfinite(base) else None),
            "shifted_ir": out,
            "note": "IR 口径=ts 乘积序列（z(信号)×前向收益）"}


# ---------------------------------------------------------------------------
# position_sim（topn 键位占用：标注不门）
# ---------------------------------------------------------------------------

def position_sim(F, env: TsEnv, horizon: int | None = None,
                 cost_bps: float | None = None,
                 end_date: str | None = None) -> dict | None:
    """sign(F) 仓位模拟（任务书 §四：topn 键位，标注不门）。

    持仓 = sign(信号)（多/空/平），每 h 日再平衡（与 IC 采样同节奏）；
    毛 IR=仓位×前向收益序列 IR；净=扣换手成本（cost_bps 默认 2bp/边，
    期货单品种口径——calibration.cost_bps 显式配置时从之）。"""
    cal = env.calibration
    h = int(horizon) if horizon else int(cal.horizon)
    cost = (float(cost_bps) if cost_bps is not None
            else float(cal.cost_bps if cal.cost_bps else 2.0))
    end = end_date or cal.dev_end
    Fv = np.asarray(F, dtype=np.float64)
    if Fv.ndim != 1 or Fv.shape[0] != env.T:
        return None
    dates = pd.DatetimeIndex(env.dates)
    hi = (env.T if end is None else int(np.searchsorted(
        dates, pd.Timestamp(end), side="right")))
    fwd = ts_forward_returns(env, h)
    rows = np.arange(0, hi, h)
    pos_prev = 0.0
    gross, net, turns = [], [], []
    for t in rows:
        t = int(t)
        if t + h >= env.T or not (np.isfinite(Fv[t]) and np.isfinite(fwd[t])):
            continue
        pos = float(np.sign(Fv[t]))
        turn = abs(pos - pos_prev)
        gross.append(pos * float(fwd[t]))
        net.append(pos * float(fwd[t]) - turn * cost / 1e4)
        turns.append(turn)
        pos_prev = pos
    if len(gross) < 5:
        return None
    g = np.asarray(gross)
    n = np.asarray(net)

    def _ir(x: np.ndarray) -> float | None:
        sd = float(x.std(ddof=1))
        return (float(x.mean() / sd) if sd > 0 else None)

    g_ir, n_ir = _ir(g), _ir(n)
    return {"gross_ir": (round(g_ir, 4) if g_ir is not None else None),
            "net_ir": (round(n_ir, 4) if n_ir is not None else None),
            "turn_avg": round(float(np.mean(turns)), 4),
            "n_obs": int(len(gross)),
            "cost_bps_per_side": cost,
            "note": ("sign(F) 仓位模拟（标注不门；完整执行模拟挂账二期；"
                     "毛口径未含滑点/保证金/移仓成本）")}


# ---------------------------------------------------------------------------
# 评估主入口（诊断同形）
# ---------------------------------------------------------------------------

def evaluate_ts(F, env: TsEnv, n_trials: float = 1,
                pool_std: float | None = None, horizon=None,
                bar_sigma: float | None = None,
                wall_mode: str | None = None) -> dict:
    """单品种时序因子 dev 评估——键集与截面 evaluate() 同形（模块 docstring）。

    与截面的差异只在计算器：IC 序列=乘积序列；结构检验=循环块置换；
    beta=None；topn=position_sim。消费面（discipline/deflated/submit
    重算/trail sketch/CRN 墙）全部按原键原语义工作。"""
    from .evaluate import (_decay_diagnostics, _deflated_sharpe_p,
                           _env_horizon_view, _ic_stats,
                           _rolling_ic_stability, _train_sensitivity,
                           _yearly_sign_consistency)
    env = _env_horizon_view(env, horizon)
    F = np.asarray(F, dtype=np.float64)
    if F.ndim != 1 or F.shape[0] != env.T:
        raise ValueError(f"ts 因子输出必须 (T,)={env.T}（收到 {F.shape}）")
    if env.calibration.dev_end is None:
        raise ValueError("三区分界未设置：calibration.dev_end/sel_end 为空")

    train_end = env.calibration.dev_end
    series, telem = ts_product_series_full(F, env, horizon=None,
                                           end_date=train_end)
    if len(series) == 0:
        return {"error": "无有效观测（PIT/G-limit 剔除后序列为空）",
                "line": "ts_single", "instrument": env.instrument,
                "execution_masked": telem}
    stats = _ic_stats(series)

    result = {
        "signal": None,
        "line": "ts_single",
        "instrument": env.instrument,
        "horizon": env.calibration.horizon,
        "ic_mean_train": stats["mean"],
        "ic_ir_train": stats["ir"],
        "ic_n_train": stats["n"],
        "execution_masked": telem,
        "yearly_consistency_train": _yearly_sign_consistency(series),
        # ts 结构显著（ts_null）占用 column_perm_train 键位——
        # discipline |z|<3 fail 与 submit 快检原语义成立
        "column_perm_train": ts_block_perm_null(F, env, horizon=None,
                                                real_series=series),
        "rolling_ic_stability_train": _rolling_ic_stability(series),
        "decay_train": _decay_diagnostics(series),
        "date_shift": ts_date_shift_placebo(F, env),
        "beta_exposure": None,
        "topn": position_sim(F, env),
    }
    # 雕刻统计（描述字段，非门——已退役语义沿用）
    try:
        from .hardening import carving_from_series
        result["carving"] = carving_from_series(series)
    except Exception as _ce:  # noqa: BLE001 — 描述字段失败不判定
        result["carving"] = {"tripped": False,
                             "note": f"计算失败（不判定）: {type(_ce).__name__}"}
    from ..discipline import red_flags_and_verdict
    result["deflated_train"] = _deflated_sharpe_p(
        series, bar_sigma=bar_sigma, pool_std=pool_std,
        n_trials=n_trials, wall_mode=wall_mode)
    result["ic_series_train"] = [round(float(x), 4) for x in series.tolist()]
    result["train_sensitivity"] = _train_sensitivity(series, env)
    gv = red_flags_and_verdict(result, region="train")
    result["verdict"] = gv["verdict"]
    result["red_flags"] = gv["red_flags"]
    if gv.get("warnings"):
        result["warnings"] = gv["warnings"]
    return result


# ---------------------------------------------------------------------------
# walk-forward（t1 ≤ dev_end 钳制——样本外评估已内部化，红线 4）
# ---------------------------------------------------------------------------

def evaluate_walk_forward_ts(F, env: TsEnv, n_folds: int = 5,
                             t0_date=None, t1_date=None) -> dict:
    """ts 版分块稳健性：乘积序列按折切分（t0 缺省=dev_end 起）。

    模型面 t1 ≤ dev_end 钳制在 bridge 工具层执行（与面板同构——本函数
    被 book_audit 引擎内调时走 dev_end 之后的纯前向带）。"""
    from .evaluate import _ic_stats
    F = np.asarray(F, dtype=np.float64)
    if F.ndim != 1 or F.shape[0] != env.T:
        raise ValueError(f"ts 因子输出必须 (T,)={env.T}（收到 {F.shape}）")
    t0_date = env.calibration.dev_end if t0_date is None else t0_date
    series = ts_product_series(
        F, env, horizon=None,
        start_date=str(pd.Timestamp(t0_date).date()),
        end_date=(str(pd.Timestamp(t1_date).date()) if t1_date else None))
    series = series.dropna()
    n = len(series)
    if n < n_folds * 2:
        return {"error": f"区间样本不足（n={n} < {n_folds * 2}）"}
    edges = np.linspace(0, n, n_folds + 1).astype(int)
    overall_mean = float(series.mean())
    sign = int(np.sign(overall_mean))
    per_fold = []
    same = 0
    for i in range(n_folds):
        seg = series.iloc[edges[i]:edges[i + 1]]
        st = _ic_stats(seg)
        per_fold.append({"t0": str(seg.index[0].date()),
                         "t1": str(seg.index[-1].date()),
                         "ic_mean": st["mean"], "ic_ir": st["ir"],
                         "n": st["n"]})
        if np.sign(st["mean"]) == sign:
            same += 1
    return {"n_folds": n_folds, "per_fold": per_fold,
            "per_fold_ic_ir": [f["ic_ir"] for f in per_fold],
            "per_fold_ic_mean": [f["ic_mean"] for f in per_fold],
            "fold_consistency": same / n_folds, "sign": sign,
            "overall_ic_mean": overall_mean,
            "note": "ts 线：乘积序列分块；模型面 t1 钳制由 bridge 工具层执行"}
