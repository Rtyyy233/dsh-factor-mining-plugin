# coding=utf-8
"""程序性拒绝治愈 + source 一致性校验回归（2026-08-26
rank_persistence_w30 事故产品化）。

事故链：agent 提交时重打的 source 与评估时字节不一致 → hash 漂移 →
尾块反查按提交 hash 查 trail 0 条 →「tail 块缺失」程序性拒绝烧名；
重试被铁律拦（名撞 + hash 撞均无治愈通道）→ 被推向微调源码绕过。

锁定：
1. 拒绝分类：程序性（尾块反查失败/基础设施）写 reject_kind=procedural；
   实质性（门真判了）= substantive
2. 名撞铁律治愈：程序性拒绝的旧 entry 重提交时删除放行（legacy 无
   字段按 reason 回退）；实质性照旧烧名
3. hash 撞铁律治愈（新增——事故中换名提交被这条拦死）
4. source 一致性：diagnosis._meta.source_hash ≠ 提交 source →
   事务性拒绝（-32002，registry 不落盘），错误信息含双侧 hash
5. 尾块缺失 reason 带 hash 前缀与修法指引（可行动错误）
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
from dsh_factor_mining.discipline import source_fingerprint  # noqa: E402

TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
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


def _read_reg(root: Path) -> list:
    return json.loads((root / "state" / "registry.json").read_text(
        encoding="utf-8"))


# ---- 1+5. 程序性拒绝落盘分类 + 可行动 reason ----

def test_procedural_reject_tagged_and_actionable(tmp_path):
    """从未评估过的 source + 手工诊断（无 _meta）走 tail 轨：尾块反查
    失败 → 程序性拒绝落盘，entry 带 reject_kind=procedural，reason
    含提交 hash 前缀与修法指引。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    never_evaluated = TILT_SOURCE + "\n# variant never evaluated\n"
    sub = b.dispatch("registry.submit", {
        "name": "ghost", "signal": "x", "source": never_evaluated,
        "diagnosis": {"ic_ir_train": 0.2, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.4, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}},
        "admit_basis": "tail"})
    assert sub["accepted"] is False
    assert "tail 块缺失" in sub["reason"]
    # 可行动信息：hash 前缀 + 字节一致指引
    sh = source_fingerprint(never_evaluated)
    assert sh[:12] in sub["reason"], sub["reason"]
    assert "字节不一致" in sub["reason"]
    e = _read_reg(tmp_path)[-1]
    assert e["reject_kind"] == "procedural", e
    assert "trail_engine 无评估记录" in e["reason"]


# ---- 2. 名撞治愈（含 legacy 回退） ----

def test_name_iron_rule_heals_procedural(tmp_path):
    """legacy 程序性拒绝 entry（无 reject_kind 字段——生产现状的
    rank_persistence_w30）+ 同名同 source 重提交（这次先 evaluate）：
    旧 entry 删除放行，新提交正常判定。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    sh = source_fingerprint(TILT_SOURCE)
    # 直写生产现状的 legacy 烧名 entry（无 reject_kind 字段）
    reg = [{"name": "rp_w30", "accepted": False,
            "reason": "[tail轨] tail 块缺失或计算失败——尾部轨准入需要 "
                      "evaluate 自动尾部诊断",
            "source_hash": sh, "source": TILT_SOURCE, "signal": "x"}]
    (tmp_path / "state" / "registry.json").write_text(
        json.dumps(reg), encoding="utf-8")
    # 正规流程：evaluate（trail 有了尾块）→ 同名同 source submit
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "rp_w30", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag, "admit_basis": "tail"})
    assert sub["accepted"] is True, sub.get("reason")
    reg2 = _read_reg(tmp_path)
    assert len(reg2) == 1 and reg2[0]["name"] == "rp_w30"
    assert reg2[0]["accepted"] is True        # 旧程序性条目已治愈替换


# ---- 3. hash 撞治愈（事故中换名提交被这条拦死） ----

def test_hash_iron_rule_heals_procedural(tmp_path):
    """程序性拒绝的旧 entry + 换名同 source 提交：删除放行。
    实质性拒绝的旧 entry + 换名同 source：铁律照旧。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    sh = source_fingerprint(TILT_SOURCE)
    reg = [{"name": "burned", "accepted": False,
            "reason": "[tail轨] tail 块缺失或计算失败——尾部轨准入需要 "
                      "evaluate 自动尾部诊断",
            "source_hash": sh, "source": TILT_SOURCE, "signal": "x"}]
    (tmp_path / "state" / "registry.json").write_text(
        json.dumps(reg), encoding="utf-8")
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "fresh_name", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag, "admit_basis": "tail"})
    assert sub["accepted"] is True, sub.get("reason")
    names = [e["name"] for e in _read_reg(tmp_path)]
    assert names == ["fresh_name"], names       # 换名治愈，旧条目删除

    # 对照：实质性拒绝（G3 真判了）+ 换名 → 铁律照拦
    reg2 = _read_reg(tmp_path)
    reg2[0]["accepted"] = False
    reg2[0]["reason"] = "[tail轨] G3 拒收：spread_ir=0.100 < 门 0.300"
    reg2[0]["reject_kind"] = "substantive"
    (tmp_path / "state" / "registry.json").write_text(
        json.dumps(reg2), encoding="utf-8")
    try:
        b.dispatch("registry.submit", {
            "name": "another_name", "signal": "x", "source": TILT_SOURCE,
            "diagnosis": diag, "admit_basis": "tail"})
        raised = False
    except BridgeError as e:
        raised = True
        assert "不得重复登记" in str(e), str(e)
    assert raised


# ---- 4. source 一致性硬校验 ----

def test_source_mismatch_transactional_reject(tmp_path):
    """诊断 _meta.source_hash ≠ 提交 source hash（事故根因的字节漂移）
    → -32002 事务性拒绝，registry 不落盘，错误信息含双侧 hash。"""
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    drifted = TILT_SOURCE + "\n"          # 一个换行 = 字节漂移
    assert source_fingerprint(drifted) != source_fingerprint(TILT_SOURCE)
    try:
        b.dispatch("registry.submit", {
            "name": "drift", "signal": "x", "source": drifted,
            "diagnosis": diag})
        raised = False
    except BridgeError as e:
        raised = True
        assert "不一致" in str(e), str(e)
        assert source_fingerprint(drifted)[:12] in str(e), str(e)
        assert source_fingerprint(TILT_SOURCE)[:12] in str(e), str(e)
    assert raised
    assert not (tmp_path / "state" / "registry.json").exists() or \
        _read_reg(tmp_path) == []          # 事务性：无落盘
