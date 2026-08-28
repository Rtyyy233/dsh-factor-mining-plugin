# coding=utf-8
"""门链（submit 事务化：全过才写 registry；判定汇总在此，输入来自
audit/placebo/increment/robustness 各测量模块）。

- G0 审计链：确定性 + 截断不变性 + 延迟退化（audit.audit_full）。
- G1′ placebo：z ≥ 3（matched-turnover，轻/权威双档）。
- G2′ artifact：随机游走面板成本后不得显著为正（默认 z ≥ 3 即拒；
  z 不可判定 = 不判过——fail-closed，口径 C4 校准期定档）。
- G3′ deflation：z_placebo ≥ E[max X](N_eff) + Φ⁻¹(1−α)（z 已归一化，
  s0=1）；N_eff = trade_minhash 交易清单聚类；**与 z≥3 取大**——N_eff
  小时 G1′ 保底、大时 deflation 接管（835 量级 E[max]≈3.2，z≥3 恰
  不够）。只数策略试验（因子已入 registry，跨层封口，不做三层 Šidák）。
"""
from __future__ import annotations

import numpy as np

from dsh_factor_mining.factor.gates import (
    blp_sigma,
    norm_ppf,
    selection_minhash,
    selection_similarity,
)

#: 交易清单血缘阈（沿用尾线名单链判定 0.25）
TRADE_LINEAGE = 0.25


def trade_minhash(trades: list[dict]) -> list[int]:
    """交易清单 (day, asset, side) 的 32 哈希 MinHash（side 编进 pair 串：
    同日同资产不同向 = 不同交易）。"""
    pairs = [(tr.get("t"), f"{tr.get('asset')}:{tr.get('side')}")
             for tr in (trades or [])]
    return selection_minhash(pairs)


def trade_n_eff(trials: list) -> tuple[int, int]:
    """交易清单 Jaccard 贪心聚类 → (N_eff, 总试验数)。

    trials = 账本条目 list（取 entry["trade_mh"]）；无签名的旧条目按
    独立试验计（保守方向）。与尾线 tailgate.tail_n_eff 同构同阈。"""
    n = len(trials)
    if n == 0:
        return 0, 0
    sigs = [t.get("trade_mh") if isinstance(t, dict) else None
            for t in trials]
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
            if selection_similarity(sigs[i], sigs[j]) >= TRADE_LINEAGE:
                parent[find(j)] = find(i)
    return len({find(i) for i in range(n)}), n


def deflation_bar(n_eff: int, alpha: float = 0.05,
                  g1_floor: float = 3.0) -> dict:
    """G3′ z 单位门：max(G1′ 保底 3, E[max X](N_eff) + Φ⁻¹(1−α))。

    z_placebo 已按 null std 归一（s0=1）——deflation 在 z 单位下一条
    公式（S4 拍板：要，但便宜）。"""
    emax = blp_sigma(float(max(int(n_eff), 1)))
    z_alpha = norm_ppf(1.0 - alpha)
    bar = max(g1_floor, emax + z_alpha)
    return {"n_eff": int(n_eff), "emax_sigma": round(emax, 4),
            "z_alpha": round(z_alpha, 4), "alpha": alpha,
            "g1_floor": g1_floor, "required_z": round(bar, 4)}


def g1_placebo(placebo_out: dict) -> tuple[bool, str]:
    z = (placebo_out or {}).get("z")
    if z is None or not np.isfinite(float(z)):
        return False, ("G1′ 无法判定：matched-turnover null z 不可算"
                       "（Sharpe 缺失/有效 draw 不足/零离散）")
    if float(z) < 3.0:
        m = (placebo_out or {}).get("m")
        return False, (f"G1′ 拒收：placebo z={float(z):.2f} < 3"
                       f"（m={m}，同换手随机信号 null 未被超越）——"
                       "策略收益不超出路径依赖运气")
    return True, f"G1′ 通过：placebo z={float(z):.2f} ≥ 3"


def g2_artifact(rw_out: dict, z_threshold: float = 3.0) -> tuple[bool, str]:
    """随机游走面板 artifact 门（C4 默认口径 z ≥ 3；无法判定不判过）。"""
    z = (rw_out or {}).get("z")
    if z is None or not np.isfinite(float(z)):
        return False, ("G2′ 无法判定：随机游走面板 z 不可算——不判过"
                       "（fail-closed，与噪声门同一纪律）")
    if float(z) >= z_threshold:
        return False, (f"G2′ 拒收：随机游走面板成本后显著为正"
                       f"（z={float(z):.2f} ≥ {z_threshold}）——"
                       "策略在剥削模拟器/管道 artifact，非真 alpha")
    return True, f"G2′ 通过：随机游走面板 z={float(z):.2f} < {z_threshold}"


def g3_deflation(z_placebo, n_eff: int, alpha: float = 0.05) -> tuple[bool, str, dict]:
    bar = deflation_bar(n_eff, alpha=alpha)
    if z_placebo is None or not np.isfinite(float(z_placebo)):
        return False, "G3′ 无法判定：z_placebo 缺失", bar
    ok = float(z_placebo) >= bar["required_z"]
    return (ok,
            (f"G3′ {'通过' if ok else '拒收'}：z={float(z_placebo):.2f} "
             f"{'≥' if ok else '<'} 门 {bar['required_z']:.2f}"
             f"（E[max X]={bar['emax_sigma']:.2f}σ + "
             f"Φ⁻¹(1−{bar['alpha']})={bar['z_alpha']:.2f}，N_eff={n_eff}）"),
            bar)
