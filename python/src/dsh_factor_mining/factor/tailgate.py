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
3. **独立 deflation**：bar = E[max X]（B-LP 单侧，N = 名单聚类 N_eff）×
   s0，s0 优先 null 校准 spread 段经验 std（WS1），降级 1/√采样日数。
   准入是单侧规则（spread_ir ≥ bar）——用单侧 E[max]，不用双侧
   E[max|X|]（双侧数值更大，会无谓抬门）。WS4（2026-08-25）：spread
   序列偏度/峰度入账本（spread_moments，只存矩不存序列），判定改
   t 分母修正（与 IC 线 _dsr_p_from_stats 同构）；缺矩 legacy =
   现行公式。
4. **跨轨 Šidák**：双通道准入 = 两次通过机会；α_track =
   1−(1−α_global)^(1/2)，尾部轨的 z 门槛用 α_track 分位数。

准入链（admit_basis="tail"）：
  G1 placebo_z ≥ 3（top-N 净超额 vs 随机选股 null——引擎侧已算）
  G2 spread 噪声门 |z| < 3（spread 版合成世界检验——管道 artifact 拒；
  z 不可判定（有效世界不足/零离散）= 不判过——fail-closed，与 IC 轨
  噪声门「无法判定 = 事务中止」同一纪律，不做静默放行）
  G3 deflation：spread_ir ≥ E[max X](N_eff)×s0 + Φ⁻¹(1−α_track)×s0
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


#: 账本收录的 stage（WS3 防线 a）：test 不是试验——它是消耗品的最终
#: 消费，不进 deflation 计价（selection 区本就不写尾块，列出仅为显式）
TAIL_LEDGER_STAGES = {"development", "batch", "composite"}


def tail_ledger(trail_engine: list) -> list[dict]:
    """从 trail_engine 派生尾部账本：键 (source_hash, horizon, k_frac)
    去重（重评同键 = 更新不新增）；每条带 spread_ir / n_days(用
    placebo_periods 近似) / selection_mh / ic_ir（跨轨对照用）。

    stage 过滤（WS3 2026-08-25）：只收 development/batch/composite——
    test 区尾块（region="test"）绝不进 deflation 计价（test 是一次性
    消耗品，把它的数字当试验基数 = 泄漏进准入门）。"""
    seen: dict[tuple, dict] = {}
    for e in trail_engine:
        if not isinstance(e, dict):
            continue
        if e.get("stage") not in TAIL_LEDGER_STAGES:
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
    """B-LP 单侧期望最大值 E[max X]（σ 单位）——与 evaluate._blp_sigma
    同式本地复制（避免跨模块私有导入的循环依赖；两处同步维护）。
    单侧：准入规则是 spread_ir ≥ bar（单侧），不用双侧 E[max|X|]。"""
    from statistics import NormalDist

    if n <= 1:
        return 0.0
    gamma = 0.5772156649015329
    z = NormalDist()
    return float((1 - gamma) * z.inv_cdf(1 - 1 / n)
                 + gamma * z.inv_cdf(1 - 1 / (n * np.e)))


def tail_deflation_bar(n_eff: int, n_days: int,
                       alpha_track: float = 0.0253,
                       s0_emp: float | None = None,
                       spread_ir: float | None = None,
                       moments: dict | None = None) -> dict:
    """尾部线 deflation 门参数（WS4 2026-08-25：高阶矩修正版）。

    s0 = null 下 spread_ir 的标准差——s0_emp（本面板随机因子 spread_ir
    经验分布 std，WS1 null 校准 spread 段）有效则用经验值
    （s0_source="empirical"），否则解析式 1/√采样日数（"analytic"）。
    sr0 = E[max X](N_eff)·s0（B-LP 单侧正态——与 IC 线 _LuckSampler
    同一高斯假设，对齐水平不超纲）。

    G3 判定（与 evaluate._dsr_p_from_stats 完全同构：sr→spread_ir，
    sr0→emax·s0，n→采样日数）：
      有 spread_moments（corrected 模式）：
        t_adj = (spread_ir − sr0)·√(n−1)
                / (1 − g3·spread_ir + (g4−1)/4·spread_ir²)^0.5 ≥ z_α
        （denom clip 下限 1e-8 同 IC 线；判定走 t_adj，required 经
        二分反解仅为展示）
      无 moments（legacy 模式）：现行公式 spread_ir ≥ sr0 + z_α·s0
        （denom=1 语义；t_adj 按 denom=1 计算，仅诊断）

    降级不拒绝给门（与 pool_std 的拒绝语义不同）：解析式有理论依据
    （null 下 spread_ir ~ N(0, 1/√n)）且 G3 有 G1 前置门，经验值是
    校准增强；pool_std 缺失时 p 完全不可算才拒绝。

    α_track = 1−(1−0.05)^(1/2) ≈ 0.0253（跨轨 Šidák 二分，用户批准）。"""
    import math as _math
    from statistics import NormalDist

    if n_days is None or n_days < 10 or n_eff < 1:
        return {"ok": False, "reason": "样本不足（n_days<10 或 N_eff<1）"}
    if (isinstance(s0_emp, (int, float)) and float(s0_emp) > 0
            and np.isfinite(float(s0_emp))):
        s0 = float(s0_emp)
        s0_source = "empirical"
    else:
        s0 = 1.0 / np.sqrt(float(n_days))
        s0_source = "analytic"
    z_req = NormalDist().inv_cdf(1 - alpha_track)
    emax = _blp_sigma(float(n_eff))
    out = {"ok": True, "s0": round(s0, 6), "s0_source": s0_source,
           "n_eff": n_eff,
           "alpha_track": alpha_track,
           "emax_sigma": round(emax, 4),
           "bar": round(emax * s0, 4)}
    mom_ok = (isinstance(moments, dict)
              and isinstance(moments.get("g3"), (int, float))
              and isinstance(moments.get("g4"), (int, float))
              and isinstance(moments.get("n"), (int, float))
              and int(moments["n"]) >= 5
              and np.isfinite(float(moments["g3"]))
              and np.isfinite(float(moments["g4"])))
    if not mom_ok:
        # legacy：旧尾块无 spread_moments → denom=1（即现行公式），
        # 判定不因缺矩而翻转（统一校准对照后再定稿新旧差异）
        out.update({"gate_kind": "legacy", "moments": "legacy",
                    "denom": 1.0, "g3": None, "g4": None,
                    "required_spread_ir": round(emax * s0 + z_req * s0, 4),
                    "z_req": round(z_req, 4)})
        if isinstance(spread_ir, (int, float)) and np.isfinite(spread_ir):
            out["t_adj"] = round((float(spread_ir) - emax * s0)
                                 * _math.sqrt(max(int(n_days) - 1, 1)), 4)
            out["pass_g3"] = bool(float(spread_ir)
                                  >= emax * s0 + z_req * s0)
        return out
    g3, g4 = float(moments["g3"]), float(moments["g4"])
    n_m = int(moments["n"])
    sr = float(spread_ir) if (isinstance(spread_ir, (int, float))
                              and np.isfinite(spread_ir)) else 0.0

    def _t_adj(sr_):
        denom = max(1.0 - g3 * sr_ + (g4 - 1.0) / 4.0 * sr_ * sr_, 1e-8)
        return (sr_ - emax * s0) * _math.sqrt(n_m - 1) / _math.sqrt(denom), denom

    t_val, denom = _t_adj(sr)
    out.update({"gate_kind": "corrected", "moments": "corrected",
                "denom": round(denom, 6), "g3": g3, "g4": g4,
                "t_adj": round(t_val, 4), "z_req": round(z_req, 4),
                "pass_g3": bool(t_val >= z_req)})
    # required 反解（二分，展示用；判定以 t_adj 为准）
    hi = max(10.0, sr * 2 + emax * s0 * 2 + 5.0)
    if _t_adj(hi)[0] < z_req:
        out["required_spread_ir"] = None   # 门在扫描范围外——t_adj 数字为准
    elif _t_adj(0.0)[0] >= z_req:
        out["required_spread_ir"] = 0.0
    else:
        lo = 0.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if _t_adj(mid)[0] >= z_req:
                hi = mid
            else:
                lo = mid
        out["required_spread_ir"] = round(hi, 4)
    return out


def tail_admission(tail_block: dict, ledger: list[dict],
                   spread_noise_z: float | None,
                   s0_emp: float | None = None,
                   placebo_z_gate: float | None = None,
                   placebo_m: int | None = None) -> dict:
    """尾部轨准入判定（纯函数；IC 轨照旧走 _passes_acceptance）。

    s0_emp（WS1）：landscape spread 段的经验 s0，bridge 负责读取与指纹
    校验后传入；纯函数性质不变。
    placebo_z_gate（WS2）：submit 侧重跑的权威 placebo z（样本加厚
    m≥60）；None → 兜底 tail_block 的 evaluate 轻量值并标
    placebo_degraded（σ 相对误差 ~24% 的弱证据——轻 null 不可作硬判据，
    门的重判定必须在 submit 用足样本）。

    返回 (accepted, reason, 诊断)；诊断含 N_eff/bar（带 s0_source）/
    placebo（gate 值 + 实际 draws + degraded 标注）——PASS-FAIL 教训：
    多维报告不坍缩。"""
    if not isinstance(tail_block, dict) or tail_block.get("error"):
        return {"accepted": False,
                "reason": "tail 块缺失或计算失败——尾部轨准入需要 evaluate 自动尾部诊断"}
    spread_ir = tail_block.get("spread_ir")
    if not isinstance(spread_ir, (int, float)):
        return {"accepted": False,
                "reason": "spread_ir 不可计算（截面样本不足）——尾部轨无从判定"}
    topn = tail_block.get("topn") or {}
    placebo_light = topn.get("placebo_z")
    gate_ok = (isinstance(placebo_z_gate, (int, float))
               and np.isfinite(float(placebo_z_gate)))
    placebo_z = float(placebo_z_gate) if gate_ok else placebo_light
    placebo_degraded = not gate_ok
    m_used = placebo_m if (gate_ok and placebo_m is not None) \
        else topn.get("placebo_draws")
    n_days = topn.get("periods")
    n_eff, n_total = tail_n_eff(ledger)
    # G3 deflation（跨轨 Šidák α_track；WS4：spread_moments 在场走
    # t 分母偏度/峰度修正，缺矩 legacy = 现行公式）
    bar = tail_deflation_bar(max(n_eff, 1), int(n_days or 0), s0_emp=s0_emp,
                             spread_ir=spread_ir,
                             moments=tail_block.get("spread_moments"))
    diag = {"n_eff": n_eff, "n_trials_tail": n_total, "bar": bar,
            "placebo_z": placebo_z, "placebo_draws": m_used,
            "placebo_m": placebo_m if gate_ok else None,
            "placebo_degraded": placebo_degraded,
            "spread_noise_z": spread_noise_z}
    # G1 placebo：top-N 净超额 vs 随机选股（权威值 = submit 重算；
    # 轻量值兜底时明确标注 degraded——弱证据不冒充门判据）
    _g1_note = (f"，m={m_used}（submit 权威重跑）"
                if gate_ok else
                (f"，degraded：m={m_used}（evaluate 轻量值兜底——"
                 "submit 重算不可用，σ 估计误差 ~24% 的弱证据）"
                 if placebo_z is not None else ""))
    if not isinstance(placebo_z, (int, float)) or placebo_z < 3.0:
        return {"accepted": False,
                "reason": (f"G1 拒收：top-N placebo z={placebo_z}（需 ≥3"
                           f"{_g1_note}）——"
                           "净超额未超出随机选股 null，尾部组差不成立"),
                "diag": diag}
    # G2 spread 噪声门：合成世界上 spread 显著 = 管道 artifact。
    # z 不可判定（None/NaN——有效世界不足或零离散）= 不判过（fail-closed）：
    # 与 IC 轨噪声门「无法判定 = 事务中止」同一纪律，静默放行会让 G2
    # 在恰好算不出的因子上永久失效
    if spread_noise_z is None:
        return {"accepted": False,
                "reason": ("G2 无法判定：spread 噪声门 z 不可计算（有效世界"
                           "不足/零离散）——尾轨不据此判过；tail 轨准入路径"
                           "上 bridge 会以事务中止处理"),
                "diag": diag}
    if abs(spread_noise_z) >= 3.0:
        return {"accepted": False,
                "reason": (f"G2 拒收：spread 噪声门 |z|={abs(spread_noise_z):.2f}"
                           "≥3——尾部组差在拟合评价管道 artifact"),
                "diag": diag}
    # G3 deflation（跨轨 Šidák α_track）
    if not bar.get("ok"):
        return {"accepted": False,
                "reason": f"G3 无法判定：{bar.get('reason')}", "diag": diag}
    if not bar.get("pass_g3", False):
        if bar.get("gate_kind") == "corrected":
            reason = (f"G3 拒收：spread_ir={spread_ir:.3f}（t_adj="
                      f"{bar['t_adj']:.2f} < z_α={bar['z_req']:.2f}，"
                      f"denom={bar['denom']:.3f}，g3={bar['g3']:+.2f}/"
                      f"g4={bar['g4']:.2f} 修正，sr0={bar['bar']:.3f}，"
                      f"N_eff={n_eff}，跨轨 Šidák α_track=0.0253）——"
                      "尾部线多重检验未过")
        else:
            reason = (f"G3 拒收：spread_ir={spread_ir:.3f} < 门 "
                      f"{bar['required_spread_ir']:.3f}"
                      f"（E[max|X|]={bar['emax_sigma']:.2f}σ×s0，"
                      f"N_eff={n_eff}，跨轨 Šidák α_track=0.0253）——"
                      "尾部线多重检验未过")
        return {"accepted": False, "reason": reason, "diag": diag}
    _g3_num = (f"t_adj={bar['t_adj']:.2f} ≥ z_α={bar['z_req']:.2f}"
               f"（g3={bar['g3']:+.2f}/g4={bar['g4']:.2f} 修正）"
               if bar.get("gate_kind") == "corrected"
               else f"门 {bar['required_spread_ir']:.3f}")
    return {"accepted": True,
            "reason": (f"尾部轨通过：placebo z={placebo_z:.1f}"
                       f"（{('m=' + str(m_used) + ' 权威重跑') if gate_ok else 'degraded 轻量值'}），"
                       f"spread_ir={spread_ir:.3f} 过 G3：{_g3_num}，"
                       f"N_eff={n_eff}，s0={bar.get('s0_source')}）"),
            "diag": diag}
