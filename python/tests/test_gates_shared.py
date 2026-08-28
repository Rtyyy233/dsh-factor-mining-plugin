# coding=utf-8
"""P0 共享面提炼锁（2026-08-27 策略层规划）：gates.py 是唯一实现，
evaluate / tailgate 留同名别名——本测试锁死两件事：
1. 别名即同一函数对象（不存在第二份实现悄悄漂移）；
2. 数值口径跨进程可复现（MinHash 系数、B-LP 公式、DSR 算术的
   硬编码 stamp——改动 gates 数学必须带着这里一起改，且那是
   跨包破坏性改动：两侧账本的 N_eff/bar 不可比）。
"""
import pytest

from dsh_factor_mining.factor import evaluate as ev
from dsh_factor_mining.factor import gates, tailgate as tg


def test_aliases_are_same_objects():
    assert ev._blp_sigma is gates.blp_sigma
    assert ev._dsr_sr0 is gates.dsr_sr0
    assert ev._dsr_p_from_stats is gates.dsr_p_from_stats
    assert ev._norm_ppf is gates.norm_ppf
    assert tg._blp_sigma is gates.blp_sigma
    assert tg.selection_minhash is gates.selection_minhash
    assert tg.selection_similarity is gates.selection_similarity
    assert tg._MH_N == gates.MH_N == 32


@pytest.mark.parametrize("n,expected", [
    (1, 0.0),
    (2, 0.5197553442805939),
    (10, 1.57459830134575),
    # 规划书 §G3′ 引用值：N_eff=835 量级时 E[max]≈3.2，z≥3 恰不够
    (835, 3.203485154090944),
    (1000, 3.255121513652723),
])
def test_blp_sigma_stamps(n, expected):
    assert gates.blp_sigma(n) == pytest.approx(expected, rel=1e-12)


def test_selection_minhash_stamp_and_similarity():
    sig = gates.selection_minhash([(0, 1), (0, 2), (5, 3), (5, 7)])
    assert sig[:6] == [1004692108, 246973519, 215685678,
                       860995920, 1227056638, 418413256]
    a = gates.selection_minhash([(0, i) for i in range(10)])
    b = gates.selection_minhash([(0, i) for i in range(10)])
    c = gates.selection_minhash([(0, i) for i in range(10, 20)])
    assert gates.selection_similarity(a, b) == 1.0
    assert gates.selection_similarity(a, c) < 0.2
    # 空集与异长签名：保守 0
    assert gates.selection_minhash([]) == [0] * gates.MH_N
    assert gates.selection_similarity(a, [1, 2]) == 0.0


def test_dsr_p_from_stats_stamp():
    p = gates.dsr_p_from_stats(0.5, 0.0, 3.0, 60, 1.75, 0.15)
    assert p == pytest.approx(0.04272165207275803, rel=1e-12)
    # 有折减无 pool_std → 拒给（None），样本不足 → None
    assert gates.dsr_p_from_stats(0.5, 0.0, 3.0, 60, 2.0, None) is None
    assert gates.dsr_p_from_stats(0.5, 0.0, 3.0, 3, 1.0, 0.1) is None
