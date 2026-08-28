# coding=utf-8
"""共享门原语（2026-08-27 策略层规划 P0 提炼：因子包行为零变化）。

收编此前复制的门数学，供因子层（evaluate / tailgate / bridge）与策略层
（dsh_strategy_lab 兄弟包 import 本包）共同消费——策略层是第三个
消费者，即提炼时机（原 evaluate / tailgate 注释自认「两处同步维护」）。
原定义处保留同名别名，既有 import 面（bridge / tests）零变化；数值
口径逐位一致（tests/test_gates_shared.py 锁死）。

- blp_sigma：B-LP 单侧期望最大值 E[max X]（σ 单位）
- selection_minhash / selection_similarity：选择集 (day, asset) 对的
  32 哈希 MinHash 签名与 Jaccard 无偏相似率
- dsr_sr0 / dsr_p_from_stats：选择运气 bar（sr0 = E[max|X|]·pool_std）
  与矩修正 deflated p 的充分统计量重算
"""
from __future__ import annotations

import math

#: MinHash 签名长度（跨包契约：策略层 trade_minhash 复用同一签名空间，
#: 长度或系数变更 = 跨包破坏性改动，两侧账本不可比）
MH_N = 32


def norm_ppf(q: float) -> float:
    """标准正态分位数（stdlib 实现，无 scipy 依赖）。"""
    from statistics import NormalDist
    return NormalDist().inv_cdf(q)


def blp_sigma(n: float) -> float:
    """B-LP 单侧期望最大值（σ 单位）：(1-γ)Z(1-1/N)+γZ(1-1/(N·e))。

    在用消费面：
    - 因子 IC 线（evaluate）：v2（2026-08-20）门公式；v3（2026-08-21）
      起门改 E[max|X|] 直算（_LuckSampler）——此函数仅用于旧 trail 条目
      n_eff_at_write 章的 σ 换算（包络跨版本单调）。
    - 尾部线（tailgate）G3 deflation bar：准入规则是单侧
      spread_ir ≥ bar（双侧 E[max|X|] 数值更大，会无谓抬门）；策略层
      G3′ 同式（z 单位下 E[max X](N_eff) + Φ⁻¹(1−α)）。
    保留 v2 数值口径：N≤1 → 0。"""
    if n <= 1:
        return 0.0
    gamma = 0.5772156649015329  # Euler-Mascheroni
    z1 = norm_ppf(1.0 - 1.0 / n)
    z2 = norm_ppf(1.0 - 1.0 / (n * math.e))
    return (1.0 - gamma) * z1 + gamma * z2


def _stable_hash64(s: str) -> int:
    import hashlib

    return int.from_bytes(
        hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def selection_minhash(pairs) -> list[int]:
    """(day_idx, asset_idx) 选择集的 MinHash 签名（确定性，跨进程可复现）。

    pair 元素任意可 str()（策略层 trade_minhash 复用时把 side 编进
    pair 串，如 (day, "asset:buy")——同一哈希空间，签名可互比）。"""
    import random as _random

    rng = _random.Random(20260826)
    coeffs = [(rng.randrange(1, 2 ** 31), rng.randrange(0, 2 ** 31))
              for _ in range(MH_N)]
    hs = [_stable_hash64(f"{t}:{j}") for t, j in pairs]
    if not hs:
        return [0] * MH_N
    return [min(((a * h + b) % (2 ** 31 - 1)) & 0xFFFFFFFF for h in hs)
            for a, b in coeffs]


def selection_similarity(mh_a: list, mh_b: list) -> float:
    """签名一致率 = Jaccard 无偏估计（与 _fingerprint_similarity 同式）。"""
    if (not isinstance(mh_a, list) or not isinstance(mh_b, list)
            or len(mh_a) != len(mh_b) or not mh_a):
        return 0.0
    return sum(1 for a, b in zip(mh_a, mh_b) if a == b) / len(mh_a)


def dsr_sr0(bar_sigma: float | None, pool_std: float | None) -> tuple[float | None, str]:
    """选择运气 bar（v3 2026-08-21）：sr0 = E[max|X|]·pool_std。

    bar_sigma = 选择统计量（agent 按 |IC| 挑最优，含符号事后翻转——trail
    实证：volume_decay_30 以 IC_IR=-0.62 入册）在全局零假设下的期望水平，
    σ 单位，由 _LuckSampler 从试验相关矩阵 R 直算。双侧：max|X|。

    v2 链条（谱 (Σλ)²/Σλ² → B-LP(N_eff) 单侧）退役，三处失真（74 条真实
    trail 对照实验）：按长度分组只实测 10.2% 对（F1）；有效自由度统计量
    ≠ 期望最大值预测器，弥散相关下低估 3.4 倍（F2）；单侧 bar 配 |IC|
    统计量漏计符号选择（F3）。

    bar_sigma=None/0（直调单检验口径）→ 无折减；任何折减（bar_sigma>0）
    都需 pool_std，缺 → (None, 拒绝原因)——不给不可信数字。"""
    if not bar_sigma or bar_sigma <= 0:
        return 0.0, "单检验口径（无选择折减）"
    if pool_std is not None and pool_std > 0:
        sr0 = bar_sigma * pool_std
        return sr0, (f"pool_std={pool_std:.4f}（E[max|X|]={bar_sigma:.3f}σ "
                     "选运 bar 的池分布缩放）")
    return None, ("选择折减需 pool_std（池内 IC_IR 分布尺度）——deflated p 不可信，"
                  "拒绝给出。需 trail_engine 实测 IC_IR 分布或 null 地形校准。")


def dsr_p_from_stats(sr, g3, g4, n, bar_sigma: float | None,
                     pool_std: float | None) -> float | None:
    """充分统计量 → DSR p（A3：registry_submit 提交时重算；v3 bar_sigma 口径）。

    单因子 deflated p 完全由 (sr_hat, skew, kurt, n_obs) 与 (bar_sigma,
    pool_std) 决定——这四项充分统计量都在 deflated_train 里带着，submit
    重算无需 IC 序列/env/重评估（纯算术，去耦合设计不破）。样本不足或
    缺 pool_std（有折减时）→ None。"""
    try:
        sr, g3, g4, n = float(sr), float(g3), float(g4), int(n)
    except (TypeError, ValueError):
        return None
    if n < 5:
        return None
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    denom = max(denom, 1e-8)
    sr0, _ = dsr_sr0(bar_sigma, pool_std)
    if sr0 is None:
        return None
    t_stat = (abs(sr) - sr0) * (n - 1) ** 0.5 / denom ** 0.5
    return 0.5 * math.erfc(t_stat / math.sqrt(2.0))
