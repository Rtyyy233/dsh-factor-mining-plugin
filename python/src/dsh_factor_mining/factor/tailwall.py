# coding=utf-8
"""尾部墙 v2（2026-10-07）：名单重叠即相关 + 单侧 CRN E[max X]。

v1（tail_n_eff 并查集连通分量计数）的三重病——链式（相关性不可传递但
传递闭包强加传递：巨连通内 86% 的对低于边阈值）、巨连通（etf 99% 试验
并入单分量 → N_eff=18/2797，出身审计见 NOTE-bar-estimators-three-
methods-2026-10-07.md）、定义敏感（并查集 vs 贪心代表制族数差 +1244%）
——根源都是把依赖结构压缩成标量计数。v2 原则：名单信息一路带到相关
矩阵，不做任何离散化。

核心闭式（null 下 top-K 等权对池等权价差统计量，iid 特异分量；推导
DESIGN-tail-wall-v2-2026-10-07.md §1）：

    rho_ij = (Omega_ij - c) / (1 - c),   c = K/N = k_frac

    Omega_ij = 时间平均名单重叠率；(day,asset) shingle 的 MinHash
    Jaccard 与其单调换算（等基数近似）：Omega = 2J / (1 + J)

边界：同名单 → rho=1；随机重叠（Omega=c）→ rho=0（**随机基线自动
校正**——朴素 Jaccard 相关会系统性高估）；不相交 → -c/(1-c) 微负
互补 → clip 0（负结构对 E[max X] 二阶小、伤 PSD，保守丢弃）。
环境修正 (a, b)：a=斜率（理论 1）、b=ambient（同市场同日共享收益
实现的残余相关，理论 ≈0）——null-calibration 经验拟合覆盖（P2），
缺省理论值（闭式在 iid 特异下精确，缺校准不是 fail-closed）。

单调性：E[max X] 经 _LuckSampler 单侧子类采样，CRN 下加试验只多一个
max 候选 → 估计量对台账规模单调不减（耦合定理）——v1"灌同族变体洗
墙"的后门在数学上关死。桥接攻击回归锚：test_tailwall.py。
"""
from __future__ import annotations

import numpy as np

from .evaluate import _LuckSampler
from .gates import selection_similarity


def jaccard_to_overlap(j):
    """MinHash Jaccard → 名单重叠率 Omega（等基数近似：Omega=2J/(1+J)）。

    由 |∩| = J·|∪| = J(|S_i|+|S_j|−|∩|) 解出；|S_i|=|S_j| 时精确，
    失配为二阶（影子重放 P4 对照精确式）。j 需在 [0,1]。"""
    j = np.clip(np.asarray(j, dtype=np.float64), 0.0, 1.0)
    return 2.0 * j / (1.0 + j)


def overlap_rho(omega, c, a: float = 1.0, b: float = 0.0):
    """重叠率 → null 相关系数：rho = clip(a·(Omega−c)/(1−c) + b, 0, 1)。

    c 可为标量或与 omega 同形的成对值（sqrt(c_i·c_j) 由调用方合成）。
    下 clip 0：负互补结构（两名单合起来对池做差）对 E[max X] 贡献
    二阶小且大面积负值伤 PSD——保守丢弃（DESIGN §2）。"""
    omega = np.asarray(omega, dtype=np.float64)
    c = np.asarray(c, dtype=np.float64)
    rho = a * (omega - c) / (1.0 - np.clip(c, 0.0, 0.999)) + b
    return np.clip(rho, 0.0, 1.0)


def jaccard_matrix(sigs, block: int = 256) -> np.ndarray:
    """(n, MH_N) 签名阵 → n×n 两两 Jaccard（一致率，分块省内存）。"""
    sigs = np.asarray(sigs, dtype=np.uint32)
    n = sigs.shape[0]
    jmat = np.empty((n, n), dtype=np.float64)
    for s in range(0, n, block):
        e = min(n, s + block)
        jmat[s:e] = (sigs[s:e, None, :] == sigs[None, :, :]).mean(axis=2)
    np.fill_diagonal(jmat, 1.0)
    return jmat


def psd_project(R: np.ndarray) -> np.ndarray:
    """对称化 + 特征值 clip + **对角重标定到 1**（相关矩阵重构）。

    2026-10-07 事故修正：仅把负特征值 clip 到 +1e-8 是**正向注入方差**
    ——重构对角 = ΣλᵏVᵢₖ² 会 >1（负特征值被正数替换），列方差 >1 直接
    虚抬 E[max X]（影子实测 +0.24σ，超过 iid 精确上界=数学不可能）。
    修正：clip 到 0 后按 sqrt(dᵢdⱼ) 重标定（congruence 缩放保 PSD，
    对角精确回到 1）。IC 线采样器的同名兜底无此问题（其 R 一次 Gram
    PSD by construction，clip 分支理论不可达）。"""
    r = (R + R.T) / 2.0
    w, V = np.linalg.eigh(r)
    w = np.clip(w, 0.0, None)
    r = (V * w) @ V.T
    d = np.sqrt(np.clip(np.diag(r), 1e-12, None))
    r = r / np.outer(d, d)
    np.fill_diagonal(r, 1.0)
    return r


def overlap_R(sigs, c: float | list, a: float = 1.0, b: float = 0.0) -> np.ndarray:
    """名单签名阵 → v2 相关矩阵（对角 1、PSD 兜底）。

    c：标量（全池同 k_frac）或 per-trial 列表——成对取几何平均
    sqrt(c_i·c_j)（等 K 式的成对推广；精确一般式见 DESIGN §1，
    k_frac 实测集中一两档，几何平均与精确式差异二阶，影子重放对照）。"""
    jmat = jaccard_matrix(sigs)
    omega = jaccard_to_overlap(jmat)
    if np.isscalar(c):
        cmat = np.full_like(omega, float(c))
    else:
        cv = np.asarray(c, dtype=np.float64)
        cmat = np.sqrt(np.outer(cv, cv))
    rho = overlap_rho(omega, cmat, a=a, b=b)
    np.fill_diagonal(rho, 1.0)
    return psd_project(rho)


class OneSidedMaxSampler(_LuckSampler):
    """单侧 E[max X]（尾部墙 v2）：准入规则 spread_ir ≥ bar 本就单侧。

    _abs_max=False 切换 max 统计量取侧（父类三处 max 源已参数化）；
    load_R 直接装载外部相关矩阵（v2 的 R 来自名单重叠闭式，不经
    父类的 sketch 尾对齐路径）。CRN 单调性与父类逐字相同。"""

    _abs_max = False

    def load_R(self, R: np.ndarray) -> None:
        """从外部相关矩阵全量构造（PSD 兜底由 overlap_R 负责，此处再
        投影一次防调用方绕过）。K 列种子与父类同一 SEED=42 序列。

        单调性约定（2026-10-07 桥接攻击实测修正）：**一次装载、前缀取
        值**——emax(m)=mean(max over 前 m 列)。若改为"逐前缀全量重建"
        （每步对前缀矩阵重新 eigh），特征基不嵌套 → CRN 耦合被破坏，
        桥接试验会实测降墙（test_monotone_and_bridge_attack 首版即抓到：
        0.2167→0.2115）。前缀列集取值的单调性逐路径精确（max over 更大
        列集 ≥ 更小子集）；代价是前缀边际分布用的是全量投影矩阵的对应
        主子块（投影修正二阶小）——单调性优先。"""
        r = psd_project(np.asarray(R, dtype=np.float64))
        m = r.shape[0]
        w, V = np.linalg.eigh(r)
        w = np.clip(w, 1e-8, None)
        amat = V * np.sqrt(w)
        z = np.column_stack([self._z_col(j, self.K) for j in range(m)])
        self._keys = list(range(m))
        self._sig = [None] * m
        self._R = r
        self._Rinv = (V / w) @ V.T
        self._X = z @ amat.T
        self._mx = self._X.max(axis=1)

    def emax_prefix(self, m: int) -> float:
        """前 m 列的单侧 E[max X]——与全量装载同一 CRN 耦合（单调不减）。"""
        if self._X is None or m <= 0:
            return 0.0
        m = min(m, self._X.shape[1])
        return float(self._X[:, :m].max(axis=1).mean())

    def emax(self) -> float:
        """E[max X]（σ 单位）。M=0 → 0；M=1 → E[Z⁺]=1/√(2π)≈0.399
        （单侧底价：冷启动只有正尾是选择）。"""
        if self._mx is None or len(self._mx) == 0:
            return 0.0
        return float(self._mx.mean())


_last_emax: dict = {}   # state_root -> (emax, n)：单调哨兵的跨提交记忆


def check_sentinels(state_root, result: dict) -> list[str]:
    """墙的三只哨兵（2026-10-07 修正版：原实现只比"生效 vs 计算"，
    开关常开时恒等不会响——抓不到 v2 自身漂移）。返回告警列表（空=健康），
    由 bridge 写 stderr；v1-v2 已知系统性差距（≈0.2σ，v1 塌缩证据）
    只记录不告警。三条：
      ①上界哨兵：E[max X] 在 clip 后全非负相关下 ≤ 独立情形（Slepian），
        超界 = R 构造/投影数学 bug（psd 注入方差事故正是这类，影子期
        实测 +0.24σ 抓到）。
      ②单调哨兵：台账只增 → E[max] 法则层面单调不减；估计噪声 se≈0.005，
        容差 0.02σ。降 = 构造回归。
      ③plumbing 哨兵（bridge 内）：生效门值 ≠ v2 计算值 = 接线回归。"""
    warns = []
    if not (isinstance(result, dict) and result.get("ok")):
        return warns
    e, n = result.get("emax_sigma"), result.get("n")
    iid = result.get("iid_sigma")
    if (isinstance(e, (int, float)) and isinstance(iid, (int, float))
            and e > iid):
        warns.append(f"上界哨兵：E[max]={e} > iid 精确上界 {iid}"
                     "（数学不可能——R 构造/投影注入了方差）")
    prev = _last_emax.get(state_root)
    if (isinstance(prev, tuple) and isinstance(e, (int, float))
            and isinstance(n, (int, float)) and n >= prev[1]
            and e < prev[0] - 0.02):
        warns.append(f"单调哨兵：E[max] {e} < 上次 {prev[0]}"
                     f"（台账 {prev[1]}→{n} 未减，墙不应降）")
    if isinstance(e, (int, float)) and isinstance(n, (int, float)):
        _last_emax[state_root] = (float(e), int(n))
    return warns


def tail_wall_v2_bar(sigs, c: float | list, s0: float,
                     a: float = 1.0, b: float = 0.0,
                     alpha_track: float = 0.0253,
                     z_req: float | None = None) -> dict:
    """尾部墙 v2 门参数（纯函数）：R(名单重叠) → 单侧 CRN E[max X]。

    bar = E[max X]·s0 + z_{1−alpha_track}·s0——与 v1 的 tail_deflation_bar
    同一结构，只换 E[max] 项来源（族计数 → 重叠相关矩阵）。s0（WS1
    经验校准）与 WS4 高阶矩 t_adj 判定不变（矩修正分母在调用方组装，
    本函数的 emax 经 emax_override 进入同一公式）。

    返回含 iid_sigma = 单侧 iid 精确模拟上界（K=4000、固定种子；
    E[max] 的数学天花板，上界哨兵数据——blp 渐近式在此量级误差 ±0.05
    不可作哨兵）。其余：ok/emax_sigma/bar/n/a/b/z_req——fail 只在输入
    空台账（n=0）；其余 fail-open 理论兜底（闭式 iid 下精确，见模块头）。"""
    from statistics import NormalDist

    sigs = list(sigs)
    n = len(sigs)
    if n == 0:
        return {"ok": False, "reason": "空台账"}
    if not (isinstance(s0, (int, float)) and float(s0) > 0
            and np.isfinite(float(s0))):
        return {"ok": False, "reason": "s0 无效"}
    smp = OneSidedMaxSampler()
    smp.load_R(overlap_R(sigs, c, a=a, b=b))
    emax = smp.emax()
    # iid 精确上界（分块模拟，固定种子——上界哨兵的参照值）
    rng_iid = np.random.default_rng(246813)
    run_max = np.zeros(4000)
    for s in range(0, n, 400):
        zz = rng_iid.standard_normal((4000, min(400, n - s)))
        run_max = np.maximum(run_max, zz.max(axis=1))
    iid_sigma = float(run_max.mean())
    z = float(z_req) if z_req is not None else NormalDist().inv_cdf(1.0 - alpha_track)
    bar = emax * float(s0)
    return {"ok": True, "wall": "v2", "n": n, "a": float(a), "b": float(b),
            "emax_sigma": round(emax, 4), "bar": round(bar, 4),
            "iid_sigma": round(iid_sigma, 4),
            "s0": round(float(s0), 6), "z_req": round(z, 4),
            "required_add_z": round(z * float(s0), 4)}
