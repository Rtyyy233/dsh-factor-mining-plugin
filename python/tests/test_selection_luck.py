# coding=utf-8
"""v3（2026-08-21）选择运气 E[max|X|] 直算体系专项测试。

三处升级（74 条真实 trail 对照实验）：
  F1 相关实测：尾对齐全对实测（v2 按长度分组只测 10.2% 对，跨长度同族
     被静默置独立 → bar 被低估）
  F2 门量：bar_sigma = E[max|X|]（CRN Monte Carlo 直算），替代
     谱 (Σλ)²/Σλ² → B-LP 链条（有效自由度统计量 ≠ 期望最大值预测器）
  F3 双侧：agent 按 |IC| 挑最优（含符号事后翻转）→ max|X| 统计量

数学性质（CRN 公共随机数 + 条件采样增列）：
  - 单调性（定理，非补丁）：superset 的逐点 max ≥ subset
  - 确定性：Z 列固定种子，同 trials 同 bar
  - 同族计价：ρ≈1 的 M 个变体 bar ≈ E|Z|；独立 M 个 bar ≈ iid E[max|Z|]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.evaluate import (  # noqa: E402
    _LuckSampler,
    _blp_sigma,
    _dsr_p_from_stats,
    _dsr_sr0,
    _tail_aligned_corr,
)

E_ABS_Z = math.sqrt(2.0 / math.pi)  # ≈0.7979（M=1 双侧选运底价）


# ---- 采样器：确定性与单调性 ----

def _iid_sketches(m: int, k: int = 60, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    return {(f"h{i}", 20): list(rng.normal(0, 0.1, k)) for i in range(m)}


def _equicorr_sketches(m: int, rho: float, k: int = 60, seed: int = 11) -> dict:
    """共同因子结构：s_i = ρ·z + √(1-ρ²)·e_i → 两两相关 ≈ ρ。"""
    rng = np.random.default_rng(seed)
    z = rng.normal(0, 0.1, k)
    out = {}
    for i in range(m):
        e = rng.normal(0, 0.1, k)
        out[(f"h{i}", 20)] = list(rho * z + math.sqrt(1 - rho * rho) * e)
    return out


def test_sampler_deterministic_same_trials():
    a, b = _LuckSampler(), _LuckSampler()
    t = _iid_sketches(5)
    a.sync(t)
    b.sync(t)
    assert a.bar_sigma() == b.bar_sigma()  # CRN 固定种子：可复现
    assert a.bar_sigma() > 0


def test_sampler_monotone_under_appending():
    """单调性定理：追加试验（无论相关结构）bar 不减。"""
    s = _LuckSampler()
    trials = _equicorr_sketches(3, 0.9)
    s.sync(trials)
    bar1 = s.bar_sigma()
    # 追加 5 个高相关变体（同族参数扫描——灌水攻击面）
    more = _equicorr_sketches(5, 0.9, seed=13)
    s.sync({**trials, **more})
    assert s.bar_sigma() >= bar1 - 1e-12
    # 追加独立新维度试验
    indep = {(f"new{i}", 20): list(np.random.default_rng(100 + i).normal(0, 0.1, 60))
             for i in range(3)}
    s.sync({**trials, **more, **indep})
    assert s.bar_sigma() >= bar1


def test_family_pricing_identical_vs_independent():
    """同族计价核心：ρ→1 的族只付 E|Z| 底价；独立族付全额 E[max|Z|]。

    v2 幂校正对弥散相关族（ρ̄≈0.9）的失真由此封死：E[max|X|] 由实测
    R 直算，不经过"有效个数"中转。iid E[max|Z|]：M=10 ≈ 1.86、M=8 ≈ 1.75
    （∫[1-(2Φ(z)-1)^M]dz）。"""
    # 完全同族（ρ≈0.999）：M=10 也只比单试验略贵
    twin = _LuckSampler()
    twin.sync(_equicorr_sketches(10, 0.999))
    bar_twin = twin.bar_sigma()
    assert E_ABS_Z - 0.05 <= bar_twin <= 1.05, bar_twin
    # 独立族 M=10：接近 iid E[max|Z|] ≈ 1.86
    indep = _LuckSampler()
    indep.sync(_iid_sketches(10))
    bar_indep = indep.bar_sigma()
    assert bar_indep > 1.75, bar_indep
    # 弥散相关族（ρ=0.9）介于两者之间（v2 幂校正给 N_eff≈1.9 → bar≈B-LP(1.9)，
    # 与直算值的偏差正是 v3 修正的对象——只断直算位于两极之间且高于同族）
    mid = _LuckSampler()
    mid.sync(_equicorr_sketches(10, 0.9))
    bar_mid = mid.bar_sigma()
    assert bar_twin < bar_mid < bar_indep, (bar_twin, bar_mid, bar_indep)


def test_single_trial_floor_is_e_abs_z():
    s = _LuckSampler()
    s.sync({("only", 20): list(np.random.default_rng(3).normal(0, 0.1, 60))})
    assert abs(s.bar_sigma() - E_ABS_Z) < 0.02  # M=1 → E|Z|（符号也是选出来的）


def test_p_fw_bounds():
    s = _LuckSampler()
    s.sync(_iid_sketches(4))
    assert s.p_fw(0.0) == 1.0
    assert s.p_fw(50.0) == 0.0
    assert 0.0 < s.p_fw(2.0) < 0.5


def test_nu_telemetry_twin_vs_independent():
    """ν 遥测：孪生试验互相解释 → ν 低；独立试验 ν≈1。"""
    twin = _LuckSampler()
    twin.sync(_equicorr_sketches(2, 0.999))
    nu_t = twin.nu_telemetry()
    assert nu_t is not None and nu_t["min"] < 0.6  # 孪生：残差方差占比低
    indep = _LuckSampler()
    indep.sync(_iid_sketches(2))
    nu_i = indep.nu_telemetry()
    assert nu_i["min"] > 0.9  # 独立：几乎全部方差独立


# ---- F1：尾对齐相关实测 ----

def test_tail_aligned_corr_crosses_length_boundary():
    """不同长度 sketch 的尾部共享段必须实测（v2 长度分组返回 None → 置独立）。
    v4（2026-09-13 计价审计）：重叠下限 20→60——短重叠段 |r| 零假设期望
    ≈1/√20≈0.22，纯噪声且正向膨胀平均相关（生产事故成分之一）。"""
    rng = np.random.default_rng(5)
    a = list(rng.normal(0, 0.1, 85))              # 85 点
    # b（60 点）= a 尾 60 点的强相关变体：重叠 60 ≥ 下限 → 实测 ≈0.95
    b = [0.95 * x for x in a[-60:]]
    c = _tail_aligned_corr(a, b)
    assert c is not None and c > 0.85, c
    # 重叠不足（<60，v4 前为 20）→ None（保守按独立计——短重叠=噪声源）
    short = [0.9 * x for x in a[-30:]]
    assert _tail_aligned_corr(a, short) is None
    tiny = list(rng.normal(0, 0.1, 10))
    assert _tail_aligned_corr(a, tiny) is None


def test_build_r_incident_regression_psd_and_cluster_discount():
    """计价事故回归（2026-09-13）：混合长度+族间负相关的试验集——

    旧实现(逐对短重叠+abs+PSD 投影)把此类结构虚构成巨型共同因子,
    E[max] 超 iid 上限(生产实测 4.58 > 4.35)。修复后:
    ①R 最小特征值 ≥ 0(单 Gram 构造,无需救投影);
    ②聚簇结构享受折扣:bar 明显低于 iid M 试验值。
    """
    rng = np.random.default_rng(21)
    trials = {}
    n_grp, grp = 8, 5
    for g in range(n_grp):
        base = rng.normal(0, 1, 84)
        if g % 2 == 1:
            base = -base  # 奇数族与偶数族负相关(abs 时代的伤害点)
        for i in range(grp):
            trials[(f"g{g}_{i}", 20)] = list(base + rng.normal(0, 0.3, 84))
    # 混入一个短 sketch(<60)——不进 R,按独立计
    trials[("tiny", 20)] = list(rng.normal(0, 0.1, 30))
    smp = _LuckSampler()
    smp.sync(trials)
    w = np.linalg.eigvalsh(smp._R)
    assert w.min() > -1e-8, ("R 非 PSD", w.min())  # ① 修复:构造即 PSD
    bar = smp.bar_sigma()
    # ② 聚簇折扣:40 试验若全独立 ≈ E[max|Z|]_40 ≈ 2.35;8 族 → ≈2.0±
    iid_41 = _LuckSampler()
    iid_41.sync({(f"x{i}", 20): list(np.random.default_rng(100 + i).normal(0, 0.1, 84))
                 for i in range(41)})
    assert bar < iid_41.bar_sigma() - 0.15, (bar, iid_41.bar_sigma())


def test_sampler_r_uses_tail_alignment_across_lengths():
    """长度不一致的孪生族：R 实测高相关（v2 会被置独立 → bar 高估选运差价）。"""
    rng = np.random.default_rng(9)
    z = rng.normal(0, 0.1, 85)
    s = _LuckSampler()
    s.sync({("a", 20): list(z), ("b", 20): list(z[-60:])})
    assert s._R is not None
    off = abs(float(s._R[0, 1]))
    assert off > 0.85, s._R


# ---- 门函数：sr0 = bar_sigma·pool_std ----

def test_dsr_sr0_gate_semantics():
    assert _dsr_sr0(None, None)[0] == 0.0            # 直调单检验口径
    assert _dsr_sr0(0.0, 0.2)[0] == 0.0
    sr0_none, note = _dsr_sr0(2.0, None)             # 有折减无基线 → 拒给
    assert sr0_none is None and "pool_std" in note
    sr0, _ = _dsr_sr0(2.0, 0.15)
    assert abs(sr0 - 0.30) < 1e-12


def test_dsr_p_monotone_in_bar():
    """固定 sr，选运 bar 越高 → deflated p 越大（门更严）。"""
    ps = [_dsr_p_from_stats(0.5, 0.0, 3.0, 60, bar, 0.15)
          for bar in (0.8, 1.5, 2.5, 3.5)]
    assert all(p is not None for p in ps)
    assert ps == sorted(ps), ps
    # 有折减无 pool_std → None（不给不可信数字）
    assert _dsr_p_from_stats(0.5, 0.0, 3.0, 60, 2.0, None) is None


def test_blp_sigma_legacy_stamp_conversion():
    """v2 旧章 n_eff_at_write → σ 换算（包络跨版本单调）。N≤1 → 0。"""
    assert _blp_sigma(1) == 0.0
    assert _blp_sigma(0.5) == 0.0
    assert 1.0 < _blp_sigma(10) < _blp_sigma(100) < _blp_sigma(1000)


# ---- bridge：_n_eff_from_entries / 全链集成 ----

GOOD = "def factor(env):\n    import pandas as pd\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
VARIANT = "def factor(env):\n    import pandas as pd\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(60) - 1.0).values\n"


def _panel(path: Path, T=800, N=40, seed=4):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}", "open": p * 1.001,
                         "high": p * 1.01, "low": p * 0.99, "close": p,
                         "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(path)


def _setup(root: Path, mode="in_process"):
    data = root / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(root / "state"), execution_mode=mode)
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)}, "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


def _entry(h: str, sketch: list | None, horizon: int = 20, **kw) -> dict:
    e = {"source_hash": h, "stage": "development", "horizon": horizon,
         "ic_series_sketch": sketch}
    e.update(kw)
    return e


def test_n_eff_from_entries_pending_floor_and_gate_ignores_stale_stamps():
    b = Bridge(state_root="unused-state-root-for-unit", execution_mode="in_process")
    # 空 trail + pending 单例 → M=1 底价 E|Z|
    stats = b._n_eff_from_entries([], source_hash="abc", horizon=20)
    assert stats["n_trials"] == 1
    assert abs(stats["bar_sigma"] - E_ABS_Z) < 0.02, stats
    # v4（2026-09-13 计价审计）：包络摘除——伪造/陈旧的 bar_sigma_at_write
    # 章不得回读进门。生产事故:章棘轮把诚实 3.35 锁死到 4.58(超 iid 上限)。
    entries = [_entry("h1", list(np.random.default_rng(1).normal(0, 0.1, 60)),
                      bar_sigma_at_write=5.0)]
    stats2 = b._n_eff_from_entries(entries, source_hash="h2", horizon=20)
    assert stats2["bar_sigma"] < 2.0, stats2  # 两独立试验的诚实价,不是章的 5.0
    # v2 旧章(谱 N_eff)换算同样不进门(双无 sketch=2 次独立试验,诚实≈1.12)
    entries_v2 = [_entry("h1", None, n_eff_at_write=100.0)]
    stats3 = b._n_eff_from_entries(entries_v2, source_hash="h2", horizon=20)
    assert stats3["bar_sigma"] < 1.5 < _blp_sigma(100.0), stats3


def test_n_eff_from_entries_twin_family_cheaper_than_independent():
    b = Bridge(state_root="unused-state-root-for-unit", execution_mode="in_process")
    twins = [_entry(f"h{i}", list(np.random.default_rng(42).normal(0, 0.1, 60)))
             for i in range(8)]  # 同一 sketch → ρ=1 完全同族
    indep = [_entry(f"k{i}", list(np.random.default_rng(50 + i).normal(0, 0.1, 60)))
             for i in range(8)]
    bar_twin = b._n_eff_from_entries(twins)["bar_sigma"]
    bar_indep = b._n_eff_from_entries(indep)["bar_sigma"]
    assert bar_twin < 1.05, bar_twin          # 同族：≈E|Z| 底价
    assert bar_indep > 1.6, bar_indep         # 独立：接近 E[max|Z|]_8≈1.75
    # 同 hash 同 horizon 重评不重复计数（v2 语义保留）
    dup = twins + [twins[0]]
    assert b._n_eff_from_entries(dup)["n_trials"] == 8


def test_evaluate_chain_stamps_bar_sigma_and_rises(tmp_path):
    b = _setup(tmp_path)
    # null 校准建 pool_std 基线（v3：冷启动 M=1 也有 E|Z| 底价 → 需基线）
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    r1 = b.dispatch("factor.evaluate",
                    {"envId": "primary", "source": GOOD, "stage": "development", "async": False})
    d1 = r1["deflated_train"]
    assert d1.get("bar_sigma", 0) >= E_ABS_Z - 0.05, d1  # 冷启动底价在场
    assert d1.get("p") is not None and d1.get("pool_std"), d1
    assert "recomputed_at_trail" in d1
    # 第二个试验入 trail → bar 抬升（单调），nu 遥测在场
    r2 = b.dispatch("factor.evaluate",
                    {"envId": "primary", "source": VARIANT, "stage": "development", "async": False})
    d2 = r2["deflated_train"]
    assert d2["bar_sigma"] >= d1["bar_sigma"] - 1e-9, (d1, d2)
    assert isinstance(d2.get("nu"), dict) or d2.get("nu") is None
    # trail 盖章：bar_sigma_at_write 落盘
    import json
    trail = json.loads((tmp_path / "state" / "trail_engine.json").read_text(encoding="utf-8"))
    assert all("bar_sigma_at_write" in e for e in trail), trail


def test_evaluate_worker_mode_forwards_bar_sigma(tmp_path):
    """worker 子进程路径：bar_sigma 参数转发不丢（JSON 序列化边界）。"""
    b = _setup(tmp_path, "worker")
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    r = b.dispatch("factor.evaluate",
                   {"envId": "primary", "source": GOOD, "stage": "development", "async": False})
    d = r["deflated_train"]
    assert d.get("bar_sigma", 0) >= E_ABS_Z - 0.05, d
    assert d.get("p") is not None, d


def test_batch_recompute_uses_trail_level_bar(tmp_path):
    b = _setup(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    r = b.dispatch("factor.evaluate_batch",
                   {"envId": "primary", "sources": {"f1": GOOD, "f2": VARIANT}})
    for name, diag in r["factors"].items():
        dp = diag["deflated_train"]
        assert "recomputed_at_batch" in dp, (name, dp)
        assert dp["bar_sigma"] > 0 and "n_trials" in dp, dp
    # 批内族口径（evaluate_batch 内部 sampler）也在场
    assert r["batch"]["bar_sigma"] > 0


def test_submit_recompute_carries_bar_sigma(tmp_path):
    b = _setup(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": GOOD, "stage": "development", "async": False})
    res = b.dispatch("registry.submit",
                     {"name": "f_luck", "signal": "momentum20",
                      "source": GOOD, "diagnosis": diag})
    dp = res["entry"]["diagnosis"]["deflated_train"]
    assert "recomputed_at_submit" in dp and dp["bar_sigma"] > 0, dp


def test_cold_start_without_baseline_rejects_p(tmp_path):
    """空 trail + 无 null 基线：M=1 的 E|Z| 底价也要求 pool_std——p=None 拒给。"""
    b = _setup(tmp_path)
    r = b.dispatch("factor.evaluate",
                   {"envId": "primary", "source": GOOD, "stage": "development", "async": False})
    d = r["deflated_train"]
    assert d.get("p") is None
    assert "pool_std" in d.get("note", ""), d


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
