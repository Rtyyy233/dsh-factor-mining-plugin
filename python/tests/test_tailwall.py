# coding=utf-8
"""尾部墙 v2 测试（2026-10-07，PLAN-tail-wall-v2-and-rescan P1c/P5①④）。

覆盖：闭式数学边界 / 双侧 golden（IC 线行为钉死）/ 单调性 + 桥接攻击
（v1 洗墙后门的永久回归锚）/ 充分性合成锚（弥散结构上计数塌缩 vs
CRN）/ 门参数 wall 字段 / tail_admission v2 双报透传。
"""
import numpy as np
import pytest

from dsh_factor_mining.factor.evaluate import _LuckSampler
from dsh_factor_mining.factor.gates import blp_sigma, selection_minhash
from dsh_factor_mining.factor.tailgate import tail_admission, tail_deflation_bar
from dsh_factor_mining.factor.tailwall import (
    OneSidedMaxSampler,
    check_sentinels,
    jaccard_to_overlap,
    overlap_rho,
    overlap_R,
    tail_wall_v2_bar,
)


def _sigs_from_subpool(subpool, n_trials, k=20, days=10, seed=0):
    """subpool 内随机选 k 资产 × days 日 → MinHash 签名列表。"""
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n_trials):
        pairs = []
        for d in range(days):
            chosen = rng.choice(np.asarray(subpool), size=k, replace=False)
            pairs.extend((int(d), int(a)) for a in chosen)
        out.append(selection_minhash(pairs))
    return out


# ---- 闭式数学边界 ----

def test_jaccard_to_overlap_math():
    assert float(jaccard_to_overlap(0.0)) == 0.0
    assert float(jaccard_to_overlap(1.0)) == 1.0
    assert abs(float(jaccard_to_overlap(0.5)) - 2.0 / 3.0) < 1e-12
    v = jaccard_to_overlap(np.array([0.0, 0.5, 1.0]))
    assert v.shape == (3,) and abs(v[2] - 1.0) < 1e-12


def test_overlap_rho_boundaries():
    # 同名单 → 1；随机重叠（Omega=c）→ 0（基线自动校正）；不相交 → clip 0
    assert abs(float(overlap_rho(1.0, 0.2)) - 1.0) < 1e-12
    assert abs(float(overlap_rho(0.2, 0.2))) < 1e-12
    assert float(overlap_rho(0.0, 0.2)) == 0.0
    # 环境修正 b 抬整体，仍 clip 到 [0,1]
    assert abs(float(overlap_rho(0.2, 0.2, b=0.1)) - 0.1) < 1e-12
    assert float(overlap_rho(1.0, 0.2, b=0.3)) == 1.0


# ---- 双侧 golden（IC 线行为钉死）----

def test_two_sided_golden():
    """父类默认双侧不变：独立参照实现（同 Z 列种子、同 PSD 投影）对拍
    _LuckSampler.bar_sigma——任何未来改动破坏双侧语义立即爆。"""
    rng = np.random.default_rng(7)
    sketches = [(rng.standard_normal(70) * 0.3).round(4).tolist()
                for _ in range(5)]
    sketches[2] = (np.asarray(sketches[0]) * 0.8 + 0.01).round(4).tolist()
    trials = {f"k{i}": s for i, s in enumerate(sketches)}

    smp = _LuckSampler()
    smp.sync(dict(trials))
    golden = smp.bar_sigma()
    assert smp._abs_max is True

    # 独立参照：同 z 列公式直接重算
    R = smp._R
    w, V = np.linalg.eigh(R)
    w = np.clip(w, 1e-8, None)
    A = V * np.sqrt(w)
    Z = np.column_stack([_LuckSampler._z_col(j, _LuckSampler.K)
                         for j in range(len(trials))])
    X = Z @ A.T
    ref = float(np.abs(X).max(axis=1).mean())
    assert abs(golden - ref) < 1e-9
    # 单侧口径下严格更小（同 R 同种子）
    one = OneSidedMaxSampler()
    one.load_R(R)
    assert 0.5 * golden < one.emax() < golden


# ---- 单调性 + 桥接攻击（v1 洗墙后门的永久回归锚）----

def test_monotone_and_bridge_attack():
    pool_a = list(range(0, 30))
    pool_b = list(range(40, 70))
    sigs_a = _sigs_from_subpool(pool_a, 12, seed=1)
    sigs_b = _sigs_from_subpool(pool_b, 12, seed=2)
    c = 0.2
    rows = sigs_a + sigs_b

    # 桥接试验：横跨 A0∪B0 两半采样（v1 下合并两簇 → 墙降的攻击形态）
    rng = np.random.default_rng(9)
    bridge_pairs = []
    for d in range(10):
        chosen = np.concatenate([
            rng.choice(np.asarray(pool_a), size=10, replace=False),
            rng.choice(np.asarray(pool_b), size=10, replace=False)])
        bridge_pairs.extend((int(d), int(a)) for a in chosen)
    bridge = selection_minhash(bridge_pairs)

    # 一次装载全量（含桥接）、前缀取值——单调性来自 CRN 列耦合（同一定理：
    # max over 更大列集 ≥ 更小子集），正是 v1 做不到的那条
    full_rows = rows + [bridge]
    R_full = overlap_R(full_rows, c)
    one = OneSidedMaxSampler()
    one.load_R(R_full)
    b0 = one.emax_prefix(24)   # 两簇基线
    b1 = one.emax_prefix(30)   # +6 个同簇 A 试验：墙不降
    b2 = one.emax_prefix(31)   # +桥接试验：墙不降
    assert b1 >= b0 - 1e-12
    assert b2 >= b1 - 1e-12, "桥接试验不得降墙（CRN 单调性定理）"
    full = tail_wall_v2_bar(full_rows, c, s0=0.1377)
    assert abs(one.emax() - full["emax_sigma"]) < 1e-3  # 响应字段 4 位舍入


# ---- 充分性合成锚（弥散结构上计数塌缩 vs CRN）----

def test_dispersion_sufficiency_anchor():
    """弥散相关汤上：v1 连通分量计数塌缩（<<n），v2 CRN emax 高于按塌缩
    计数的 B-LP 值——'有效维数标量不是 E[max] 充分统计量'的固化锚。"""
    n = 120
    sigs = _sigs_from_subpool(list(range(0, 100)), n, k=15, days=8, seed=5)
    R = overlap_R(sigs, 0.15)
    one = OneSidedMaxSampler()
    one.load_R(R)
    emax_v2 = one.emax()
    # v1 口径：并查集连通分量（|J|>=0.25 在弥散汤上几乎无边 → 近似全计数；
    # 这里直接用 tailgate 同构复刻）
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    iu = np.triu_indices(n, 1)
    strong = R[iu] >= 0.25
    for a, b in zip(iu[0][strong], iu[1][strong]):
        ra, rb = find(int(a)), find(int(b))
        if ra != rb:
            parent[ra] = rb
    n_eff_v1 = len({find(i) for i in range(n)})
    assert n_eff_v1 < n * 0.85, "夹具预期：弥散汤上连通分量塌缩"
    assert emax_v2 > blp_sigma(max(n_eff_v1, 1))
    assert emax_v2 >= 0.9 * blp_sigma(n)


# ---- 门参数 wall 字段 ----

def test_tail_deflation_bar_wall_flag():
    b1 = tail_deflation_bar(18, 80, s0_emp=0.1377)
    assert b1["ok"] and b1["wall"] == "v1"
    # out 里是 4 位舍入值，与全精度 blp_sigma 容差对齐
    assert abs(b1["emax_sigma"] - blp_sigma(18.0)) < 1e-3
    b2 = tail_deflation_bar(18, 80, s0_emp=0.1377, emax_override=2.9)
    assert b2["wall"] == "v2"
    assert abs(b2["bar"] - 2.9 * 0.1377) < 1e-3
    # 无效 override → 回 v1（fail-open 方向 = 现行行为）
    b3 = tail_deflation_bar(18, 80, s0_emp=0.1377, emax_override=float("nan"))
    assert b3["wall"] == "v1"


# ---- tail_admission v2 双报透传 ----

def test_tail_admission_v2_passthrough():
    tail_block = {"spread_ir": 0.9,
                  "spread_moments": {"g3": 0.1, "g4": 3.0, "n": 60},
                  "topn": {"periods": 80}}
    v2 = {"ok": True, "wall": "v2", "n": 500, "emax_sigma": 2.9,
          "bar": 0.4}
    d = tail_admission(tail_block, [], spread_noise_z=0.5,
                       s0_emp=0.1377, placebo_z_gate=5.0,
                       placebo_m=120, v2_wall=v2)
    assert d["accepted"] is True
    assert d["diag"]["bar"]["wall"] == "v2"
    assert d["diag"]["bar"]["emax_sigma"] == 2.9
    assert d["diag"]["wall_v2"]["n"] == 500
    # v2 缺席 → v1 行为（同输入同判定路径）
    d1 = tail_admission(tail_block, [], spread_noise_z=0.5,
                        s0_emp=0.1377, placebo_z_gate=5.0,
                        placebo_m=120)
    assert d1["diag"]["bar"]["wall"] == "v1"
    assert d1["diag"]["wall_v2"] is None


# ---- 哨兵（上界/单调；接线哨兵在 bridge 内）----

def test_sentinels():
    root = "test-sentinel-root"
    # ①上界：E[max] 超 iid 精确上界 = 数学不可能，必须告警
    bad = {"ok": True, "emax_sigma": 3.77, "iid_sigma": 3.52, "n": 2797}
    w = check_sentinels(root, bad)
    assert any("上界" in x for x in w)
    # 健康值不告警
    good = {"ok": True, "emax_sigma": 3.31, "iid_sigma": 3.52, "n": 2797}
    w2 = check_sentinels(root + "-2", good)
    assert w2 == []
    # ②单调：台账未减而 E[max] 下降超容差 → 告警（依赖上一步记忆）
    w3 = check_sentinels(root + "-2",
                         {"ok": True, "emax_sigma": 3.20,
                          "iid_sigma": 3.52, "n": 2797})
    assert any("单调" in x for x in w3)
    # 台账增长后同值不告警（法则层面单调，估计噪声容差 0.02）
    w4 = check_sentinels(root + "-2",
                         {"ok": True, "emax_sigma": 3.31,
                          "iid_sigma": 3.60, "n": 2900})
    assert w4 == []
    # ⑤v1-v2 已知系统性差距不属于哨兵（无告警字段——由双报记录）
