# coding=utf-8
"""尾部线独立多重检验与准入（2026-08-25 用户批准设计——与 IC 线分账，
不混用：四处结构差异见记忆 factor-mining-tail-dimension-design）。

结构差异的产品化：
1. **独立账本**：从 trail_engine 派生（单一事实源）——键 =
   (source_hash, horizon, k_frac)，凡带 tail 块的条目即一次尾部试验
   （自动计算 = 自动计数，堵可选停时）。不落第二个文件。
2. **家族几何 = top-K 名单 MinHash Jaccard**：IC ρ=0.75 的两因子尾部
   名单可只重叠 0.4（尾部恰是因子分歧最大处）——IC 家族链在此失效。
   选择集 = {(采样日, top-K 资产)} 的 (day, asset) 对，32 哈希 MinHash
   签名（128B/试验），相似率 = Jaccard 无偏估计。
3. **独立 deflation**：bar = E[max|X|]（B-LP，N = 名单聚类 N_eff）×
   s0，s0 = 1/√采样日数（null 下 spread_ir ~ N(0, 1/√n)）。
   B-LP 高阶矩修正省略（spread 序列未入账本——记为已知简化，
   Phase 6 校准时评估影响）。
4. **跨轨 Šidák**：双通道准入 = 两次通过机会；α_track =
   1−(1−α_global)^(1/2)，尾部轨的 z 门槛用 α_track 分位数。

准入链（admit_basis="tail"）：
  G1 placebo_z ≥ 3（top-N 净超额 vs 随机选股 null——引擎侧已算）
  G2 spread 噪声门 |z| < 3（spread 版合成世界检验——管道 artifact 拒）
  G3 deflation：spread_ir ≥ E[max|X|](N_eff)×s0 + Φ⁻¹(1−α_track)×s0
  （spread day-perm 作为诊断随附——方向歧义同 IC 线，Phase 6 校准定门）
"""
from __future__ import annotations

import numpy as np

_MH_N = 32          # MinHash 签名长度（与构造指纹同量级）
_LINEAGE = 0.25     # 名单 Jaccard 链判定阈（对齐 _FP_LINEAGE_THRESH）


def _stable_hash64(s: str) -> int:
    import hashlib

    return int.from_bytes(
        hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def selection_minhash(pairs) -> list[int]:
    """(day_idx, asset_idx) 选择集的 MinHash 签名（确定性，跨进程可复现）。"""
    import random as _random

    rng = _random.Random(20260826)
    coeffs = [(rng.randrange(1, 2 ** 31), rng.randrange(0, 2 ** 31))
              for _ in range(_MH_N)]
    hs = [_stable_hash64(f"{t}:{j}") for t, j in pairs]
    if not hs:
        return [0] * _MH_N
    return [min(((a * h + b) % (2 ** 31 - 1)) & 0xFFFFFFFF for h in hs)
            for a, b in coeffs]


def selection_similarity(mh_a: list, mh_b: list) -> float:
    """签名一致率 = Jaccard 无偏估计（与 _fingerprint_similarity 同式）。"""
    if (not isinstance(mh_a, list) or not isinstance(mh_b, list)
            or len(mh_a) != len(mh_b) or not mh_a):
        return 0.0
    return sum(1 for a, b in zip(mh_a, mh_b) if a == b) / len(mh_a)


def tail_ledger(trail_engine: list) -> list[dict]:
    """从 trail_engine 派生尾部账本：键 (source_hash, horizon, k_frac)
    去重（重评同键 = 更新不新增）；每条带 spread_ir / n_days(用
    placebo_periods 近似) / selection_mh / ic_ir（跨轨对照用）。"""
    seen: dict[tuple, dict] = {}
    for e in trail_engine:
        if not isinstance(e, dict):
            continue
        tail = e.get("tail")
        if not isinstance(tail, dict) or tail.get("error"):
            continue
        key = (e.get("source_hash"), e.get("horizon"), tail.get("k_frac"))
        seen[key] = {
            "source_hash": e.get("source_hash"),
            "horizon": e.get("horizon"),
            "k_frac": tail.get("k_frac"),
            "spread_ir": tail.get("spread_ir"),
            "ic_ir": e.get("ic_ir"),
            "tail_ic": tail.get("tail_ic"),
            "spread_ic_corr": tail.get("spread_ic_corr"),
            "placebo_z": (tail.get("topn") or {}).get("placebo_z"),
            "n_periods": (tail.get("topn") or {}).get("periods"),
            "selection_mh": tail.get("selection_mh"),
        }
    return list(seen.values())


def tail_n_eff(ledger: list[dict]) -> tuple[int, int]:
    """名单 Jaccard 贪心聚类 → (聚类数 = N_eff, 总试验数)。

    无 selection_mh 的旧条目按独立试验计（保守方向：N_eff 只多不少）。"""
    n = len(ledger)
    if n == 0:
        return 0, 0
    sigs = [r.get("selection_mh") for r in ledger]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if find(i) == find(j):
                continue
            if sigs[i] is None or sigs[j] is None:
                continue
            if selection_similarity(sigs[i], sigs[j]) >= _LINEAGE:
                parent[find(j)] = find(i)
    return len({find(i) for i in range(n)}), n


def _blp_sigma(n: float) -> float:
    """B-LP 单侧期望最大值（σ 单位）——与 evaluate._blp_sigma 同式本地
    复制（避免跨模块私有导入的循环依赖；两处同步维护）。"""
    from statistics import NormalDist

    if n <= 1:
        return 0.0
    gamma = 0.5772156649015329
    z = NormalDist()
    return float((1 - gamma) * z.inv_cdf(1 - 1 / n)
                 + gamma * z.inv_cdf(1 - 1 / (n * np.e)))


def tail_deflation_bar(n_eff: int, n_days: int,
                       alpha_track: float = 0.0253) -> dict:
    """尾部线 deflation 门参数：s0 = 1/√采样日数（null 下 spread_ir
    标准差），bar = E[max|X|](N_eff)×s0，准入需
    spread_ir ≥ bar + Φ⁻¹(1−α_track)×s0。

    α_track = 1−(1−0.05)^(1/2) ≈ 0.0253（跨轨 Šidák 二分，用户批准）。"""
    from statistics import NormalDist

    if n_days is None or n_days < 10 or n_eff < 1:
        return {"ok": False, "reason": "样本不足（n_days<10 或 N_eff<1）"}
    s0 = 1.0 / np.sqrt(float(n_days))
    z_req = NormalDist().inv_cdf(1 - alpha_track)
    emax = _blp_sigma(float(n_eff))
    return {"ok": True, "s0": round(s0, 6), "n_eff": n_eff,
            "alpha_track": alpha_track,
            "emax_sigma": round(emax, 4),
            "bar": round(emax * s0, 4),
            "required_spread_ir": round(emax * s0 + z_req * s0, 4)}


def tail_admission(tail_block: dict, ledger: list[dict],
                   spread_noise_z: float | None) -> dict:
    """尾部轨准入判定（纯函数；IC 轨照旧走 _passes_acceptance）。

    返回 (accepted, reason, 诊断)；诊断含 N_eff/bar/placebo——
    PASS-FAIL 教训：多维报告不坍缩。"""
    if not isinstance(tail_block, dict) or tail_block.get("error"):
        return {"accepted": False,
                "reason": "tail 块缺失或计算失败——尾部轨准入需要 evaluate 自动尾部诊断"}
    spread_ir = tail_block.get("spread_ir")
    if not isinstance(spread_ir, (int, float)):
        return {"accepted": False,
                "reason": "spread_ir 不可计算（截面样本不足）——尾部轨无从判定"}
    topn = tail_block.get("topn") or {}
    placebo_z = topn.get("placebo_z")
    n_days = topn.get("periods")
    n_eff, n_total = tail_n_eff(ledger)
    bar = tail_deflation_bar(max(n_eff, 1), int(n_days or 0))
    diag = {"n_eff": n_eff, "n_trials_tail": n_total, "bar": bar,
            "placebo_z": placebo_z, "spread_noise_z": spread_noise_z}
    # G1 placebo：top-N 净超额 vs 随机选股
    if not isinstance(placebo_z, (int, float)) or placebo_z < 3.0:
        return {"accepted": False,
                "reason": (f"G1 拒收：top-N placebo z={placebo_z}（需 ≥3）——"
                           "净超额未超出随机选股 null，尾部组差不成立"),
                "diag": diag}
    # G2 spread 噪声门：合成世界上 spread 显著 = 管道 artifact
    if spread_noise_z is not None and abs(spread_noise_z) >= 3.0:
        return {"accepted": False,
                "reason": (f"G2 拒收：spread 噪声门 |z|={abs(spread_noise_z):.2f}"
                           "≥3——尾部组差在拟合评价管道 artifact"),
                "diag": diag}
    # G3 deflation（跨轨 Šidák α_track）
    if not bar.get("ok"):
        return {"accepted": False,
                "reason": f"G3 无法判定：{bar.get('reason')}", "diag": diag}
    if spread_ir < bar["required_spread_ir"]:
        return {"accepted": False,
                "reason": (f"G3 拒收：spread_ir={spread_ir:.3f} < 门 "
                           f"{bar['required_spread_ir']:.3f}"
                           f"（E[max|X|]={bar['emax_sigma']:.2f}σ×s0，"
                           f"N_eff={n_eff}，跨轨 Šidák α_track=0.0253）——"
                           "尾部线多重检验未过"),
                "diag": diag}
    return {"accepted": True,
            "reason": (f"尾部轨通过：placebo z={placebo_z:.1f}，"
                       f"spread_ir={spread_ir:.3f} ≥ 门 "
                       f"{bar['required_spread_ir']:.3f}（N_eff={n_eff}）"),
            "diag": diag}
