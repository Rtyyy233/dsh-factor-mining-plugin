# coding=utf-8
"""日期置换 null（day-perm，2026-08-25 用户决策：真实结构上的时序置换
检验，补「对真实市场结构过拟合」这层缺蒙特卡洛 null 的洞）。

IC 总量 = 时序对齐分量 + 持久截面结构分量；day-perm 与 column-perm
各证其一，正交互补：

- **day-perm（本模块）**：保留真实因子截面 F[t] 与真实收益截面 fwd[t']
  两个边缘分布（一切真实市场结构——截面相关/波动聚集/板块联动——
  原封不动），只随机重排配对 π。检验：因子时间轴与收益时间轴的
  **对齐**是否携带超出持久结构的增量
- **column-perm（evaluate 内已有）**：保留真实收益，打乱因子→资产
  分配。检验：截面**分配**信息

关键语义（实现前推演钉死，防误用为无条件硬门）：
- p_perm 小（observed 在 null 上尾，alignment_dependent=True）=
  对齐依赖。这是**真短周期因子**（如反转：factor[t] 专配 return[t+1]）
  与**时序性前视/路径过拟合**的并集——样本内不可分，拒收方向与阈值
  交给 Phase 6 校准（registry 已入册因子 × 证伪 preset 混淆矩阵），
  当前 submit 接线为 report-only
- p_perm 大（observed ≈ null）= 持久倾斜结构主导（如非流动性溢价：
  倾斜持续性使任意配对都复现 IC）。这是合法的截面 alpha 形态，
  由 column-perm 认证，day-perm 不否决——若做成无条件硬门会误杀
  整个持久溢价类因子

为什么不循环移位而用全随机配对：慢因子退化——mom250 平移 5 天
≈ 自身，小 k 移位后配对几乎不变，null 失效（现有 date-shift
k∈{5,10,21} 的盲区，保留为快速点检）；全随机配对期望重叠 → 1/T，
退化消失。本模块是 date-shift 的完备化：3 个 lag 点换成完整零分布。

统计量参数化：statistic(F, fwd, pit, sample_step) → float | None——
IC_IR 版（本模块默认）与尾部组差 spread 版（tail 线）共用同一台
置换机器。

种子纪律：置换 i 的 rng = SeedSequence(base_seed, spawn_key=(i,))，
与 noise.py 同构（可复现）。
"""
from __future__ import annotations

import numpy as np

from .env import FactorEnv


def ic_ir_statistic(F: np.ndarray, fwd: np.ndarray, pit: np.ndarray,
                    sample_step: int) -> float | None:
    """生产 IC_IR 统计量（与 evaluate/noise 同语义：采样日 rank IC
    序列 → mean/std；截面样本不足 → None）。"""
    from .noise import fast_rank_ic

    ic = fast_rank_ic(F, fwd, pit, sample_step)
    if len(ic) < 2:
        return None
    std = ic.std(ddof=1)
    return float(ic.mean() / std) if std > 0 else None


def day_permutation_test(fn, env: FactorEnv, m: int = 200, base_seed: int = 0,
                         budget_secs: float = 120.0,
                         statistic=None) -> dict:
    """完整日期置换 null：observed vs 随机配对分布（见模块 docstring）。

    返回多维诊断（PASS-FAIL 教训：不坍缩成二元）：
    - observed / null 分位数（q5/q50/q95）/ obs_percentile
    - p_upper（observed ≥ null 上尾）/ p_lower / p_two
    - alignment_dependent（p_two < 0.01）——**标注不拒收**（Phase 6
      校准后定门方向；在此之前 submit 侧只报告）

    预算自适应（沿噪声门纪律）：跑满 20 个置换后按实测均时判断，
    超 budget_secs（默认 120s）且 ≥50 个已跑即截断。向量化秩 IC 下
    单次置换毫秒级，200 次通常秒级完成，预算仅对病态统计量生效。"""
    import time as _time

    from .evaluate import _forward_returns, _pit_mask

    stat = statistic or ic_ir_statistic
    F = fn(env)
    if not isinstance(F, np.ndarray):
        F = np.asarray(F, dtype=np.float64)
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    step = env.calibration.sample_step
    obs = stat(F, fwd, pit, step)
    if obs is None or not np.isfinite(obs):
        return {"m": 0, "n_valid": 0, "observed": None,
                "note": "因子输出无法计算统计量（有效截面样本不足）"}

    T = fwd.shape[0]
    null: list[float] = []
    t0 = _time.monotonic()
    m_used = m
    budget_hit = False
    for i in range(m):
        if len(null) >= 20:
            elapsed = _time.monotonic() - t0
            per = elapsed / len(null)
            if elapsed + per > budget_secs and len(null) >= 50:
                m_used = len(null)
                budget_hit = True
                break
        rng = np.random.default_rng(np.random.SeedSequence(base_seed,
                                                           spawn_key=(i,)))
        perm = rng.permutation(T)
        s = stat(F, fwd[perm], pit, step)
        if s is not None and np.isfinite(s):
            null.append(float(s))
    if not null:
        return {"m": 0, "n_valid": 0, "observed": float(obs),
                "note": "置换样本不足（统计量在所有配对下均无法计算）"}
    arr = np.array(null)
    n = len(null)
    p_upper = (1.0 + float(np.sum(arr >= obs))) / (n + 1.0)
    p_lower = (1.0 + float(np.sum(arr <= obs))) / (n + 1.0)
    p_two = min(1.0, 2.0 * min(p_upper, p_lower))
    # 方向性 p（observed 自身方向的单侧）：alignment_dependent 的判定
    # 统计。用 p_two 判会有置换粒度问题——m=100 时 p_two 下限
    # 2/(m+1)≈0.0198，0.01 阈值永远达不到；单侧下限 1/(m+1)，
    # m>=100 即可达阈值（预算截断到 <100 时该字段不可能为 True，
    # 见 docstring 粒度说明）
    p_align = p_upper if obs >= float(arr.mean()) else p_lower
    out = {
        "m": m_used, "requested_m": m, "n_valid": n,
        "observed": float(obs),
        "null_mean": float(arr.mean()),
        "null_std": float(arr.std(ddof=1)) if n > 1 else None,
        **{f"null_q{p}": float(np.quantile(arr, p / 100))
           for p in (5, 50, 95)},
        "obs_percentile": round(float(np.mean(arr < obs)) * 100.0, 2),
        "p_upper": round(p_upper, 5),
        "p_lower": round(p_lower, 5),
        "p_two": round(p_two, 5),
        "p_align": round(p_align, 5),
        # 标注字段（不拒收）：对齐依赖 = observed 在自身方向上显著超出
        # 随机配对分布——真短周期因子与时序性过拟合的并集，门方向待
        # Phase 6 校准。粒度：需 m>=100 才可能为 True
        "alignment_dependent": bool(p_align < 0.01),
    }
    if budget_hit:
        per = (_time.monotonic() - t0) / max(m_used, 1)
        out["note"] = f"预算自适应截断（{m}→{m_used} 置换，单次 {per:.2f}s）"
    return out
