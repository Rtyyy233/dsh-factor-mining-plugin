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
    # 空 ledger → N_eff=1 → bar 低 → 通过（placebo 已过；G2 z 正常无 artifact）
    ok = tail_admission(good_tail, [], 0.5)
    assert ok["accepted"] is True, ok
    # G1 拒：placebo 弱
    g1 = tail_admission({"spread_ir": 5.0,
                         "topn": {"placebo_z": 1.0, "periods": 100}}, [], 0.5)
    assert g1["accepted"] is False and "G1" in g1["reason"]
    # G2 拒：spread 噪声门爆表
    g2 = tail_admission(good_tail, [], 5.0)
    assert g2["accepted"] is False and "G2" in g2["reason"]
    # G2 无法判定 = 不判过（fail-closed，2026-08-25：与 IC 轨噪声门
    # 「无法判定 = 事务中止」同一纪律——z 不可计算不得静默放行）
    g2n = tail_admission(good_tail, [], None)
    assert g2n["accepted"] is False and "G2 无法判定" in g2n["reason"], g2n
    # G3 拒：N_eff 大 → 门超过 spread_ir（弱 spread + 大搜索史）
    big_ledger = [{"spread_ir": 1.0}
                  for _ in range(400)]
    big_ledger = [{"spread_ir": 1.0, "selection_mh": selection_minhash(
        [(d, j + i * 1000) for d in range(50) for j in range(20)])}
                  for i in range(400)]
    g3 = tail_admission({"spread_ir": 0.3,
                         "topn": {"placebo_z": 10.0, "periods": 100}},
                        big_ledger, 0.5)
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
    """ic 拒（红牌）+ 尾轨过 → dual_pass=False（两轨判定独立）。

    2026-08-25 改用真实 evaluate 诊断：submit 重算现在优先用 trail_engine
    的权威充分统计量（防手构 sr_hat），手构弱数字会被引擎侧真实数字
    覆盖——「弱 diagnosis」必须来自真实弱评估。TILT 真实 IC_IR≈51 →
    |IC_IR|>5 红牌 → ic 轨拒；尾轨（强 tilt）独立通过。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    assert diag.get("red_flags"), "TILT 真实 IC_IR 应触发红牌"
    sub = b.dispatch("registry.submit", {
        "name": "tilt_icweak", "signal": "x",
        "source": TILT_SOURCE,
        "diagnosis": diag})
    assert sub["accepted"] is False                     # ic 轨管 acceptance（红牌拒）
    assert sub["tracks"]["ic"]["accepted"] is False
    assert sub["tracks"]["tail"]["accepted"] is True    # 尾轨独立判定
    assert sub["dual_pass"] is False


def test_submit_default_ic_unchanged(tmp_path):
    """默认 admit_basis=ic：acceptance 由 ic 轨管（尾轨照常判定作
    tracks.tail 标注），但不产生 tail_track 兼容字段。"""
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


# ---- 6. G2 fail-closed + 尾块 horizon 匹配（2026-08-25 review 修复） ----

def test_g2_undeterminable_fail_closed(tmp_path, monkeypatch):
    """G2（spread 噪声门）无法判定：tail 轨准入 = 事务中止（不烧名，
    与 IC 轨噪声门「无法判定 = 中止」同纪律）；ic 轨提交 = tracks.tail
    保守判「不判过」（标注不受 G2 fail-open 静默放行）。"""
    from dsh_factor_mining.factor import noise as noise_mod
    real = noise_mod.noise_test

    def fake_noise(fn, env, m, base_seed, **kw):
        # bridge 的 in_process 分发把 "spread" 字符串换成了统计量函数再
        # 传入——IC 版不传 statistic，spread 版传非 None
        if kw.get("statistic") is not None:
            return {"m": m, "requested_m": m, "n_valid": 1,
                    "z": float("nan"), "artifact": None,
                    "note": "有效世界不足，无法判定"}
        return real(fn, env, m, base_seed, **kw)

    monkeypatch.setattr(noise_mod, "noise_test", fake_noise)
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    # tail 轨准入 → 事务中止，registry 不落盘
    try:
        b.dispatch("registry.submit", {
            "name": "g2_abort", "signal": "x", "source": TILT_SOURCE,
            "diagnosis": diag, "admit_basis": "tail"})
        raised = False
    except BridgeError as e:
        raised = True
        assert "spread 噪声门无法判定" in str(e), str(e)
    assert raised
    assert not (tmp_path / "state" / "registry.json").exists()
    # ic 轨提交 → 主判定走 ic，尾轨标注保守（G2 无法判定 → 不判过）
    sub = b.dispatch("registry.submit", {
        "name": "g2_ic", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag})
    assert sub["tracks"]["tail"]["accepted"] is False, sub["tracks"]["tail"]
    assert "G2 无法判定" in sub["tracks"]["tail"]["reason"]
    assert sub["dual_pass"] is False


def _write_two_horizon_trail(state_root: Path, sh: str) -> None:
    """同 source_hash 两个 horizon 的尾块：旧条目 horizon=5 强、
    新条目 horizon=20 弱——老逻辑（只比 hash）会拿新的弱块。"""
    strong = {"k_frac": 0.2, "spread_ir": 0.9,
              "topn": {"placebo_z": 10.0, "periods": 100}}
    weak = {"k_frac": 0.2, "spread_ir": 0.05,
            "topn": {"placebo_z": 0.5, "periods": 100}}
    trail = [
        {"ts": "2026-08-25T10:00:00", "envId": "primary", "source_hash": sh,
         "stage": "development", "horizon": 5, "tail": strong},
        {"ts": "2026-08-25T11:00:00", "envId": "primary", "source_hash": sh,
         "stage": "development", "horizon": 20, "tail": weak},
    ]
    (Path(state_root) / "trail_engine.json").write_text(
        json.dumps(trail), encoding="utf-8")


_WEAK_DIAG = {"ic_ir_train": 0.25, "ic_n_train": 50,
              "column_perm_train": {"z": 4.0, "p": 0.0001},
              "beta_exposure": 0.1,
              "deflated_train": {"p": 0.001, "n_trials": 1,
                                 "sr_hat": 0.5, "skew": 0.0,
                                 "kurt": 3.0, "n_obs": 60}}


def test_tail_block_horizon_matched(tmp_path):
    """尾块按 (source_hash, horizon) 匹配：提交 horizon=5 的诊断必须
    用 horizon=5 的尾块（强），不得被最新的 horizon=20 弱块遮蔽。"""
    from dsh_factor_mining.discipline import source_fingerprint
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    _write_two_horizon_trail(tmp_path / "state",
                             source_fingerprint(TILT_SOURCE))
    sub = b.dispatch("registry.submit", {
        "name": "hm5", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": {**_WEAK_DIAG, "horizon": 5}})
    assert sub["tracks"]["tail"]["accepted"] is True, sub["tracks"]["tail"]
    assert sub["tracks"]["tail"]["diag"]["n_eff"] == 2  # 两个尾块账本条目


def test_tail_block_horizon_mismatch_uses_own(tmp_path):
    """对照：提交 horizon=20 的诊断用 horizon=20 的弱块 → 拒收。
    WS2 语义更新：G1 判定值来自 submit 权威重跑（tilt 因子真实 placebo
    z 高 → G1 过），弱尾块的 spread_ir=0.05 在 G3 拒——horizon 匹配
    语义仍被验证（若错拿 h=5 强块 spread_ir=0.9 则会通过）。"""
    from dsh_factor_mining.discipline import source_fingerprint
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    _write_two_horizon_trail(tmp_path / "state",
                             source_fingerprint(TILT_SOURCE))
    sub = b.dispatch("registry.submit", {
        "name": "hm20", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": {**_WEAK_DIAG, "horizon": 20}})
    assert sub["tracks"]["tail"]["accepted"] is False
    assert "G3" in sub["tracks"]["tail"]["reason"], sub["tracks"]["tail"]
    # G1 走 submit 重算值（非弱块的轻量 placebo_z=0.5）
    diag = sub["tracks"]["tail"]["diag"]
    assert diag["placebo_degraded"] is False and diag["placebo_z"] >= 3, diag
