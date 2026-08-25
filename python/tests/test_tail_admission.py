# coding=utf-8
"""尾部线独立多重检验 + 准入回归（2026-08-25 用户批准设计——与 IC 线
分账：独立账本 / 名单 Jaccard 家族几何 / 独立 deflation / 跨轨 Šidák）。

锁定：
1. 账本派生：trail_engine 带尾块条目 → (source_hash, horizon, k_frac)
   键去重
2. 名单 Jaccard 家族：同名单试验聚类 → N_eff < 总数；名单不重叠 →
   N_eff = 总数（IC ρ 高但名单不同的场景）
3. deflation bar：N_eff 越大门越高；跨轨 α_track 语义
4. 准入链：tilt 因子过 G1（placebo）+ G3（spread_ir 超 bar）；弱因子
   G1 拒；admit_basis=tail 的 submit 全链（含 IC 轨结果留存 ic_track）
5. 默认 admit_basis=ic 行为不变（回归保护）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402
from dsh_factor_mining.factor.tailgate import (  # noqa: E402
    selection_minhash, selection_similarity, tail_admission, tail_deflation_bar,
    tail_ledger, tail_n_eff)

TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
    return np.broadcast_to(tilt, env.c.shape).astype(float)
"""

# 名单与 tilt 完全不重叠的因子（后 20 资产顶部）
TILT_FLIP_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    tilt = np.where(np.arange(n) < 20, -1.0, 1.0)
    return np.broadcast_to(tilt, env.c.shape).astype(float)
"""


def _make_drift_bridge(root: Path) -> Bridge:
    rng = np.random.default_rng(11)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    mu = np.where(np.arange(N) < 20, 0.004, -0.004)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(mu[i], 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high",
                                "low": "low", "close": "close",
                                "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


# ---- 1. 账本派生 + 名单 Jaccard 家族 ----

def test_ledger_and_neff(tmp_path):
    b = _make_drift_bridge(tmp_path)
    # 三个因子：tilt、tilt 的同族变体（同名单）、flip（名单完全不重叠）
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    diag2 = b.dispatch("factor.evaluate", {"envId": "primary",
                                           "source": TILT_SOURCE.replace(
                                               "< 20", "< 21"),
                                           "stage": "development"})
    diag3 = b.dispatch("factor.evaluate", {"envId": "primary",
                                           "source": TILT_FLIP_SOURCE,
                                           "stage": "development"})
    assert all(isinstance(d.get("tail"), dict) for d in (diag, diag2, diag3))
    trail = json.loads((tmp_path / "state" / "trail_engine.json")
                       .read_text(encoding="utf-8"))
    ledger = tail_ledger(trail)
    assert len(ledger) == 3, len(ledger)   # 3 个唯一 (source, horizon, K)
    n_eff, n_total = tail_n_eff(ledger)
    assert n_total == 3
    # tilt 与 tilt<21 名单大部分重叠（20/21 资产 → Jaccard ~0.9）同簇；
    # flip 名单完全不重叠独立簇 → N_eff = 2
    assert n_eff == 2, (n_eff, ledger)
    # 重评同 source 不新增账本条目
    b.dispatch("factor.evaluate", {"envId": "primary", "source": TILT_SOURCE,
                                   "stage": "development"})
    trail2 = json.loads((tmp_path / "state" / "trail_engine.json")
                        .read_text(encoding="utf-8"))
    assert len(tail_ledger(trail2)) == 3


def test_selection_similarity_math():
    a = selection_minhash([(0, i) for i in range(20)])
    b = selection_minhash([(0, i) for i in range(20)])
    c = selection_minhash([(0, i) for i in range(20, 40)])
    assert selection_similarity(a, b) > 0.9
    assert selection_similarity(a, c) < 0.1


# ---- 2. deflation bar 单调性 ----

def test_deflation_bar_monotone():
    b1 = tail_deflation_bar(1, 100)
    b10 = tail_deflation_bar(10, 100)
    b100 = tail_deflation_bar(100, 100)
    assert b1["ok"] and b10["ok"] and b100["ok"]
    assert (b1["required_spread_ir"] < b10["required_spread_ir"]
            < b100["required_spread_ir"])
    assert tail_deflation_bar(1, 5)["ok"] is False  # 样本不足


# ---- 3. 准入链纯函数 ----

def test_admission_chain():
    good_tail = {"spread_ir": 5.0,
                 "topn": {"placebo_z": 10.0, "periods": 100}}
    # 空 ledger → N_eff=1 → bar 低 → 通过（placebo 已过）
    ok = tail_admission(good_tail, [], None)
    assert ok["accepted"] is True, ok
    # G1 拒：placebo 弱
    g1 = tail_admission({"spread_ir": 5.0,
                         "topn": {"placebo_z": 1.0, "periods": 100}}, [], None)
    assert g1["accepted"] is False and "G1" in g1["reason"]
    # G2 拒：spread 噪声门爆表
    g2 = tail_admission(good_tail, [], 5.0)
    assert g2["accepted"] is False and "G2" in g2["reason"]
    # G3 拒：N_eff 大 → 门超过 spread_ir（弱 spread + 大搜索史）
    big_ledger = [{"spread_ir": 1.0}
                  for _ in range(400)]
    big_ledger = [{"spread_ir": 1.0, "selection_mh": selection_minhash(
        [(d, j + i * 1000) for d in range(50) for j in range(20)])}
                  for i in range(400)]
    g3 = tail_admission({"spread_ir": 0.3,
                         "topn": {"placebo_z": 10.0, "periods": 100}},
                        big_ledger, None)
    assert g3["accepted"] is False and "G3" in g3["reason"], g3["reason"]


# ---- 4. submit admit_basis=tail 全链 ----

def test_submit_tail_track(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    # 先 evaluate 让 engine trail 有权威 tail 块
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "tilt_tail", "signal": "tilt on drift",
        "source": TILT_SOURCE,
        "diagnosis": diag,
        "admit_basis": "tail"})
    tt = sub.get("tail_track")
    assert isinstance(tt, dict), sub.keys()
    assert tt["admit_basis"] == "tail"
    assert isinstance(tt["diag"], dict) and "n_eff" in tt["diag"]
    # IC 轨结果留存
    assert "ic_track" in tt
    # 漂移面板信号强：tilt 应过尾部轨（placebo z 高 + spread_ir 超 N_eff=1 的 bar）
    assert sub["accepted"] is True, sub.get("reason")
    assert "[tail轨]" in sub["reason"]
    # registry 条目带 admit_basis
    reg = json.loads((tmp_path / "state" / "registry.json")
                     .read_text(encoding="utf-8"))
    assert reg[-1]["admit_basis"] == "tail"


# ---- 5. 双轨标注（2026-08-25 用户决策：registry 标注 ic/tail/双入选） ----

def test_dual_pass_annotation(tmp_path):
    """两轨提交都判：ic 轨提交也带 tracks.tail 判定 + dual_pass。
    漂移面板 tilt：强 diagnosis（ic 过）+ 尾轨过 → dual_pass=True。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    # 真实 evaluate 的 IC_IR=51 会触发 red_flag（杀 ic 轨）——双入选
    # 测试用手构合理强 diagnosis；evaluate 只为给 trail 喂权威尾块
    sub = b.dispatch("registry.submit", {
        "name": "tilt_dual", "signal": "tilt",
        "source": TILT_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    # 默认 ic 轨：acceptance 由 ic 决定
    assert sub["admit_basis"] == "ic"
    tracks = sub.get("tracks")
    assert isinstance(tracks, dict), sub.keys()
    assert tracks["ic"]["accepted"] is True, tracks["ic"]
    assert tracks["tail"]["accepted"] is True, tracks["tail"]
    assert sub["dual_pass"] is True
    assert isinstance(tracks["tail"].get("diag"), dict)
    # registry 条目同步携带
    reg = json.loads((tmp_path / "state" / "registry.json")
                     .read_text(encoding="utf-8"))
    assert reg[-1]["dual_pass"] is True
    assert set(reg[-1]["tracks"].keys()) == {"ic", "tail"}
    # ic 轨提交不产生旧 tail_track 字段（兼容字段仅 tail 轨）
    assert reg[-1]["tail_track"] is None


def test_dual_pass_false_when_ic_rejected(tmp_path):
    """ic 拒（弱 diagnosis）+ 尾轨过 → dual_pass=False（两轨判定独立）。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": TILT_SOURCE,
                                   "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "tilt_icweak", "signal": "x",
        "source": TILT_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is False                     # ic 轨管 acceptance
    assert sub["tracks"]["ic"]["accepted"] is False
    assert sub["tracks"]["tail"]["accepted"] is True    # 尾轨独立判定
    assert sub["dual_pass"] is False


def test_submit_default_ic_unchanged(tmp_path):
    """默认 admit_basis=ic：不跑尾部轨，行为与 Phase 4 前一致。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "weak_ic", "signal": "weak",
        "source": TILT_SOURCE,
        "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                      "column_perm_train": {"z": 3.5, "p": 0.0002},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.5, "n_trials": 1,
                                         "sr_hat": 0.05, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is False
    assert "tail轨" not in str(sub.get("reason"))
    assert "[tail轨]" not in str(sub.get("reason"))
    assert sub.get("tail_track") is None
    # 非法 basis 拒
    try:
        b.dispatch("registry.submit", {
            "name": "bad_basis", "signal": "x", "source": TILT_SOURCE,
            "diagnosis": {"ic_ir_train": 0.05, "ic_n_train": 50,
                          "deflated_train": {"p": 0.5}},
            "admit_basis": "both"})
        raised = False
    except BridgeError:
        raised = True
    assert raised
