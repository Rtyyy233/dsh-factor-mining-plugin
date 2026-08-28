# coding=utf-8
"""WS3 test/finalize 区尾轨消费定义回归（2026-08-25 任务书）。

锁定：
1. test 区尾块最小集：{region="test", spread_ir, tail_ic, k_typical}，
   无 null（placebo 是 train 区语义）；行集 = test 区采样日
2. 尾轨因子全链 e2e：evaluate(dev) → submit(tail) → evaluate(test)
   含 spread_ir_test + tail_decay 对照（ratio/sign_flip 或缺块 note）
3. 防线 a：test 条目带尾块但 tail_ledger 不收（不进 deflation 计价）
4. 防线 b：submit 反查排除 test 条目——同 hash 双 stage 取 train 块；
   只有 test 条目时 G1「tail 块缺失」拒绝（test spread_ir 绝不进 G3）
5. test_lock 的 diagnosis_summary 带 spread_ir_test
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor.tailgate import tail_ledger  # noqa: E402

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


# ---- 1+2. 全链 e2e：dev → submit(tail) → test ----

def test_full_chain_dev_submit_test(tmp_path):
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "development"})
    sub = b.dispatch("registry.submit", {
        "name": "tilt_e2e", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": diag, "admit_basis": "tail"})
    assert sub["accepted"] is True, sub.get("reason")

    test = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TILT_SOURCE,
                                          "stage": "test"})
    # test 尾块最小集：无 topn/placebo（null 是 train 区语义）
    tail = test.get("tail")
    assert isinstance(tail, dict) and tail.get("region") == "test", tail
    assert isinstance(tail.get("spread_ir"), float), tail
    assert "k_typical" in tail and "topn" not in tail, tail
    # train 对照在场（dev 评估先做过）
    decay = test.get("tail_decay")
    assert isinstance(decay, dict), test.keys()
    assert decay.get("spread_ir_train") == diag["tail"]["spread_ir"], decay
    assert decay.get("spread_ir_test") == tail["spread_ir"], decay
    assert isinstance(decay.get("ratio"), float), decay
    assert "sign_flip" in decay, decay
    # test_lock 的 diagnosis_summary 带 spread_ir_test
    lock = json.loads((tmp_path / "state" / "test_lock.json")
                      .read_text(encoding="utf-8"))
    assert lock["diagnosis_summary"]["spread_ir_test"] == tail["spread_ir"]
    # trail test 条目带尾块（stage="test"）
    trail = json.loads((tmp_path / "state" / "trail_engine.json")
                       .read_text(encoding="utf-8"))
    test_entries = [e for e in trail if e.get("stage") == "test"]
    assert test_entries and isinstance(test_entries[-1]["tail"], dict)


# ---- 3. 防线 a：test 条目不进账本 ----

def test_ledger_excludes_test_stage(tmp_path):
    b = _make_drift_bridge(tmp_path)
    # 2026-08-28 加固配套：test 只测已入册候选——先入册再消费
    from dsh_factor_mining.discipline import source_fingerprint
    from dsh_factor_mining.state import write_registry
    write_registry([{"name": "tilt", "source_hash": source_fingerprint(TILT_SOURCE),
                     "accepted": True, "source": TILT_SOURCE}],
                   root=str(tmp_path / "state"))
    b.dispatch("factor.evaluate", {"envId": "primary", "source": TILT_SOURCE,
                                   "stage": "development"})
    b.dispatch("factor.evaluate", {"envId": "primary", "source": TILT_SOURCE,
                                   "stage": "test"})
    trail = json.loads((tmp_path / "state" / "trail_engine.json")
                       .read_text(encoding="utf-8"))
    # test 条目带尾块但账本不收
    test_entries = [e for e in trail if e.get("stage") == "test"]
    assert test_entries and isinstance(test_entries[-1]["tail"], dict)
    dev_tail = next(e["tail"] for e in trail if e.get("stage") == "development")
    led = tail_ledger(trail)
    assert len(led) == 1, led                    # 只 dev 那条
    # 内容级：账本条目是 dev 块（placebo_z 在场；test 块无 topn）
    assert led[0]["spread_ir"] == dev_tail["spread_ir"], led
    assert led[0]["placebo_z"] == dev_tail["topn"]["placebo_z"], led
    # 直构验证：只含 test 条目的 trail → 空账本
    assert tail_ledger(test_entries) == []


# ---- 4. 防线 b：submit 反查排除 test ----

def test_submit_lookup_skips_test(tmp_path):
    from dsh_factor_mining.discipline import source_fingerprint
    b = _make_drift_bridge(tmp_path)
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    sh = source_fingerprint(TILT_SOURCE)
    strong = {"region": "train", "k_frac": 0.2, "spread_ir": 0.9,
              "spread_moments": {"g3": 0.0, "g4": 3.0, "n": 100},
              "topn": {"placebo_z": 10.0, "periods": 100}}
    weak_test = {"region": "test", "k_frac": 0.2, "spread_ir": 99.0,
                 "k_typical": 8, "n_days": 12}
    # 双 stage 同 hash：dev 强块在前、test「更强」块在后——老逻辑（无
    # stage 过滤）会拿最新的 test 块；必须取 dev 的 0.9
    trail = [
        {"ts": "2026-08-25T10:00:00", "envId": "primary", "source_hash": sh,
         "stage": "development", "horizon": 20, "tail": strong},
        {"ts": "2026-08-25T11:00:00", "envId": "primary", "source_hash": sh,
         "stage": "test", "horizon": 20, "tail": weak_test},
    ]
    (tmp_path / "state" / "trail_engine.json").write_text(
        json.dumps(trail), encoding="utf-8")
    sub = b.dispatch("registry.submit", {
        "name": "dual_stage", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1, "horizon": 20,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    tt = sub["tracks"]["tail"]
    assert tt["accepted"] is True, tt
    # 判定用的是 dev 强块（spread_ir=0.9）而非 test 的 99.0：
    # diag.bar 的 n_days 来自 topn.periods=100（test 块无 topn）
    assert tt["diag"]["bar"]["gate_kind"] == "corrected"
    assert tt["diag"]["placebo_z"] >= 3

    # 只有 test 条目 → 无可用尾块 → 拒收（红线：test spread_ir 绝不进 G3）
    (tmp_path / "state" / "trail_engine.json").write_text(
        json.dumps([trail[1]]), encoding="utf-8")
    (tmp_path / "state" / "registry.json").write_text("[]", encoding="utf-8")
    sub2 = b.dispatch("registry.submit", {
        "name": "test_only", "signal": "x", "source": TILT_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1, "horizon": 20,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    tt2 = sub2["tracks"]["tail"]
    assert tt2["accepted"] is False
    assert "tail 块缺失" in tt2["reason"], tt2["reason"]
