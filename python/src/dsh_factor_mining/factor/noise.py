# coding=utf-8
"""随机噪声世界生成 + 因子噪声硬门（2026-08-24 用户设计决策；
2026-08-25 registry 全量审计后两处修正）。

过拟合层的缺口实审结论：column-perm 测的是单因子样本内截面关联显著性，
deflation 是解析式选择运气校正——过拟合层没有任何蒙特卡洛检验。
用户拍板的硬门：**直接看因子在随机噪声上的表现**——真 alpha 按构造
不可能预测噪声；噪声上仍显著 = 因子公式在拟合评价 artifact
（管道偏差/重叠窗口构造/幸存结构），无条件拒收。

噪声世界定义（结构梯子的 R4 级，保留结构最少）：
- 价格 = 高斯随机游走（随机游走保证价格为正、无任何横截面/时序
  可预测结构）
- **截面可交换性（2026-08-25 修正）**：σ 与量能分布全池统一，
  不做逐资产匹配——首版审计实证（registry 18 因子 10 个假阳性）：
  逐资产 σᵢ/μᵢ 异质性本身是截面结构，σ-单调因子（amihud∝1/σ）
  与 fwd 的异方差秩结构产生系统性 IC。门要回答「无任何截面结构的
  世界里因子还能否提取信号」，null 必须截面可交换
- open = 前收 × 缺口噪声；high/low = max/min(o,c) × 日内振幅噪声
  （保证 h≥max(o,c)、l≤min(o,c) 的合法 OHLC 关系）
- volume = 统一对数正态；amount = v × c（自洽）
- PIT 掩码、日历、symbols、calibration 原样保留（可交易性结构
  与真实宇宙一致——掩码本身不是可挖掘的 alpha 源）

效率（2026-08-25 修正）：IC 用向量化秩相关（numpy argsort 双趟 +
行标准化内积），不再走引擎按日 pandas rank 循环——生产 env
（2822×1752）实测前者毫秒级、后者每世界秒级；50 世界 × 18 因子
从 >10 分钟降到 <2 分钟。

种子纪律：世界 m 的 rng = SeedSequence(base_seed, spawn_key=(m,))——
base_seed 默认从环境指纹派生（同环境可复现，跨环境不同），可显式传。
"""
from __future__ import annotations

import numpy as np

from .env import FactorEnv

# ---- 噪声门结构常量（2026-08-26 规划书 W2）----
# submit 噪声门至少重跑 NOISE_MIN_WORLDS 个世界、预算 NOISE_BUDGET_SECS
# （给 worker 墙 300s 留头部）——单次 factor(env) 计算 × 下限 > 预算的因子
# submit 必然事务中止。evaluate 阶段用 factor_perf 提前暴露这个天花板。
NOISE_MIN_WORLDS = 10
NOISE_BUDGET_SECS = 240.0


def factor_perf(factor_runtime_s: float) -> dict:
    """单次 factor(env) CPU 计时 → submit 噪声门可行性预警（W2；P4 起
    CPU 口径——并行会话下墙钟被挤占失真，CPU 是实现的诚实成本）。

    引擎知道 submit 的结构性上限但 agent 不知道：噪声门 ≥10 世界 ×
    单次计算 > 预算 240s（worker 墙 300s 内跑不完）→ submit 必然
    事务中止。此字段附在 evaluate 诊断上（经 _wrap_diagnosis 自然并入），
    agent 第一次 evaluate 就看到天花板，不必等到 submit 烧几分钟。
    阈值读模块常量于调用时（测试可 monkeypatch 校准）。"""
    est = NOISE_MIN_WORLDS * float(factor_runtime_s)
    if est > NOISE_BUDGET_SECS:
        verdict = "blocked"
        note = (f"submit 噪声门至少重跑 {NOISE_MIN_WORLDS} 个世界 ≈ {est:.0f}s > "
                f"预算 {NOISE_BUDGET_SECS:.0f}s，submit 必然事务中止——先向量化"
                "（df.groupby(\"symbol\") 的 shift/rolling、unstack 到宽表做矩阵"
                "运算，替代 per-symbol Python 循环）")
    elif est > NOISE_BUDGET_SECS / 2:
        verdict = "warn"
        note = (f"submit 噪声门估算 ≈ {est:.0f}s（已超预算 "
                f"{NOISE_BUDGET_SECS:.0f}s 的一半）——慢实现有触顶风险，建议向量化")
    else:
        verdict = "ok"
        note = "单次计算在噪声门预算内"
    return {
        "factor_runtime_s": round(float(factor_runtime_s), 4),
        "basis": "cpu_s",
        "submit_noise_gate_estimate_s": round(est, 4),
        "verdict": verdict,
        "note": note,
    }


def _pool_vol(c: np.ndarray, listed: np.ndarray) -> float:
    """全池统一日收益 std（所有已上市 (t,j) 样本的合并估计）。"""
    rets = c[1:] / c[:-1] - 1.0
    x = rets[listed[1:] & np.isfinite(rets)]
    if len(x) >= 200:
        v = float(np.nanstd(x))
        if v > 0:
            return v
    return 0.02


def _pool_logvol(v: np.ndarray, listed: np.ndarray) -> tuple[float, float]:
    """全池统一 log-volume 均值/std。"""
    lv = np.log(np.maximum(v, 1e-9))
    x = lv[listed & np.isfinite(lv)]
    if len(x) >= 200:
        mu = float(np.nanmean(x))
        sd = float(np.nanstd(x))
        if np.isfinite(mu) and np.isfinite(sd) and sd > 0:
            return mu, sd
    return 10.0, 0.5


def generate_noise_world(env: FactorEnv, rng: np.random.Generator) -> FactorEnv:
    """从真实 env 派生一个截面可交换的 IID 噪声世界（见模块 docstring）。"""
    T, N = env.c.shape
    listed = env.listed
    sigma = _pool_vol(env.c, listed)
    vmu, vsd = _pool_logvol(env.v, listed)

    # 价格随机游走（全池统一 σ；掩码外置 NaN——可得性结构与真实一致，
    # 掩码本身不含信息）
    rets = rng.normal(0.0, sigma, size=(T, N))
    p0 = np.where(listed[0], env.c[0],
                  np.nanmedian(env.c[0][listed[0]]) if listed[0].any() else 10.0)
    p0 = np.where(np.isfinite(p0), p0, 10.0)
    c = p0[None, :] * np.cumprod(1.0 + rets, axis=0)
    c = np.where(listed, c, np.nan)

    # open：前收 × 缺口噪声（t=0 用 p0）
    gap = np.exp(rng.normal(0.0, sigma / np.sqrt(2.0)))
    prev_c = np.vstack([p0[None, :], c[:-1]])
    o = prev_c * gap
    # high/low：合法 OHLC（h ≥ max(o,c)，l ≤ min(o,c)）
    spread = np.exp(np.abs(rng.normal(0.0, sigma / 2.0)))
    hi = np.maximum(o, c) * spread
    lo = np.minimum(o, c) / spread
    o = np.where(listed, o, np.nan)
    hi = np.where(listed, hi, np.nan)
    lo = np.where(listed, lo, np.nan)

    # 量能：全池统一对数正态（不逐资产匹配——见 docstring 可交换性修正）
    v = np.exp(rng.normal(vmu, vsd, size=(T, N)))
    v = np.where(listed, v, np.nan)
    amount = v * c if env.amount is not None else None

    return FactorEnv(o, hi, lo, c, v, env.dates, env.symbols,
                     listed=listed, amount=amount,
                     calibration=env.calibration)


def fast_rank_ic(F: np.ndarray, fwd: np.ndarray, pit: np.ndarray,
                 sample_step: int) -> np.ndarray:
    """向量化秩 IC 序列（与 evaluate._cross_sectional_ic sig_only 语义
    对齐：采样日、PIT、双方 finite、跳过退化截面；ties 用平均秩的
    pandas 版差异仅在大量并列时出现，连续因子下可忽略）。

    返回 IC 数组（每个采样日一个）。"""
    T = F.shape[0]
    rows = np.arange(0, T, max(int(sample_step), 1))
    out = []
    for t in rows:
        m = pit[t] & np.isfinite(F[t]) & np.isfinite(fwd[t])
        n = int(m.sum())
        if n < 2:
            continue
        x = F[t][m]
        y = fwd[t][m]
        # 双趟 argsort → 秩（0-based → pct）
        rx = np.empty(n)
        rx[np.argsort(x, kind="stable")] = np.arange(n)
        ry = np.empty(n)
        ry[np.argsort(y, kind="stable")] = np.arange(n)
        rx = (rx + 1) / (n + 1)
        ry = (ry + 1) / (n + 1)
        rx = (rx - rx.mean()) / (rx.std() + 1e-12)
        ry = (ry - ry.mean()) / (ry.std() + 1e-12)
        out.append(float(np.mean(rx * ry)))
    return np.array(out)


def noise_world_ic_ir(fn, env: FactorEnv, rng: np.random.Generator,
                      statistic=None) -> float | None:
    """在单个噪声世界上跑因子并返回统计量（默认 IC_IR；尾部线传
    spread 统计量——Phase 5 双轨各自的噪声门同源不同统计量）。"""
    from .evaluate import _forward_returns, _pit_mask

    world = generate_noise_world(env, rng)
    F = fn(world)
    fwd = _forward_returns(world)
    pit = _pit_mask(world)
    if statistic is not None:
        # 统计量签名与 permute.day_permutation_test 一致：
        # (F, fwd, pit, sample_step) → float | None（尾部线 spread 版）
        return statistic(F, fwd, pit, world.calibration.sample_step)
    ic = fast_rank_ic(F, fwd, pit, world.calibration.sample_step)
    if len(ic) < 2:
        return None
    std = ic.std(ddof=1)
    return float(ic.mean() / std) if std > 0 else None


def noise_test(fn, env: FactorEnv, m: int, base_seed: int,
               budget_secs: float = NOISE_BUDGET_SECS,
               statistic=None) -> dict:
    """M 个噪声世界的统计量分布 + 硬门判定（worker 内单进程循环）。

    statistic=None → IC_IR（IC 线）；尾部线传 spread 统计量（sign-scaled:
    spread 版按观测符号取绝对值语义不变——z 判 |z|≥3）。

    门统计：z = mean(world 统计量) / (std/sqrt(M))——无 artifact 的因子
    每个世界的统计量围绕 0 波动，跨世界均值应显著为 0；artifact 因子
    在每个世界系统性产生同向统计量 → z 爆表。|z| ≥ 3 = 评价 artifact。
    返回完整分布（PASS-FAIL 教训：多维诊断，不坍缩成二元）。

    预算自适应（2026-08-25 pw15_compD_5050 事故）：慢因子（复合因子
    单次 ~7s）× m=50 > worker 300s 超时 → submit 事务中止。跑满 3 个
    世界后按实测均时判断：跑不起下一个世界（将超 budget_secs，默认
    240s 给 worker 300s 留头部）即截断——下限 10 个世界保证 z 仍可判
    （病理级慢因子超时是可接受结局：raise 语义下不烧名，直接可重试）。
    快因子不受影响（50 世界全跑）。"""
    import time as _time

    irs = []
    t0 = _time.monotonic()
    m_used = m
    budget_hit = False
    for i in range(m):
        if len(irs) >= 3:
            elapsed = _time.monotonic() - t0
            per = elapsed / len(irs)
            if elapsed + per > budget_secs and len(irs) >= NOISE_MIN_WORLDS:
                m_used = len(irs)
                budget_hit = True
                break
        rng = np.random.default_rng(np.random.SeedSequence(base_seed, spawn_key=(i,)))
        try:
            # 条件传参：statistic=None 走三参旧签名（monkeypatch/外部
            # 替换 noise_world_ic_ir 的兼容面不破坏）
            if statistic is not None:
                ir = noise_world_ic_ir(fn, env, rng, statistic=statistic)
            else:
                ir = noise_world_ic_ir(fn, env, rng)
        except Exception:
            ir = None
        if ir is not None and np.isfinite(ir):
            irs.append(float(ir))
    arr = np.array(irs) if irs else np.array([np.nan])
    n_valid = len(irs)
    if n_valid < 2 or np.nanstd(arr) == 0:
        return {"m": m_used, "n_valid": n_valid, "z": np.nan,
                "mean": float(np.nanmean(arr)) if n_valid else np.nan,
                "std": float(np.nanstd(arr)) if n_valid else np.nan,
                "artifact": None,
                "note": "有效世界不足，无法判定" if n_valid < 2 else "零离散"}
    mean = float(arr.mean())
    std = float(arr.std(ddof=1))
    z = mean / (std / np.sqrt(n_valid))
    q = {f"q{p}": float(np.quantile(arr, p / 100))
         for p in (5, 50, 95)}
    out = {
        "m": m_used, "requested_m": m, "n_valid": n_valid,
        "mean": mean, "std": std,
        "z": float(z),
        **q,
        "max_abs": float(np.max(np.abs(arr))),
        # 硬门（用户决策 2026-08-24）：噪声上仍显著 = 评价 artifact
        "artifact": bool(abs(z) >= 3.0),
    }
    if budget_hit:
        per = (_time.monotonic() - t0) / max(m_used, 1)
        out["note"] = (f"预算自适应截断（{m}→{m_used} 世界，因子单次 "
                       f"{per:.1f}s）——{m_used} 世界下 z 仍可判"
                       f"（|z|≥3 阈值不变）")
    return out
