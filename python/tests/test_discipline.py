# coding=utf-8
"""纪律层测试（批次1a）：指纹/签名/低效扫描/红旗/receipt/强制因果/DSR/敏感性/双池/引擎trail/schema。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError
from dsh_factor_mining.discipline import (
    dict_fingerprint,
    file_fingerprint,
    red_flags_and_verdict,
    scan_inefficiency,
    signature_similarity,
    source_fingerprint,
    structure_signature,
)

GOOD = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""

FUTURE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return c.shift(-1).values
"""

LOOPY = """
import pandas as pd
import numpy as np

def factor(env):
    out = np.zeros((env.T, env.N))
    for j in range(env.N):
        col = env.c[:, j]
        for t in range(env.T):
            parts = []
            for k in range(t):
                parts.append(pd.DataFrame({"x": [col[k]]}))
            out[t, j] = sum(p["x"].iloc[0] for p in parts) if parts else 0.0
    return out
"""


def _panel(path: Path, T=800, N=40, seed=4, start="2019-01-02"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=T)
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


def _setup(root: Path):
    data = root / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {"etf": {
        "source": {"type": "parquet", "path": str(data)}, "layout": "long",
        "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "volume",
                    "amount": "amount"}}}}})
    return b


def test_fingerprints_stable_and_distinct(tmp_path=None):
    with tempfile.TemporaryDirectory() as d:
        p1 = Path(d) / "a.parquet"
        p2 = Path(d) / "b.parquet"
        p1.write_bytes(b"hello"); p2.write_bytes(b"world")
        assert file_fingerprint(p1) == file_fingerprint(p1)
        assert file_fingerprint(p1) != file_fingerprint(p2)
    assert dict_fingerprint({"a": 1, "b": 2}) == dict_fingerprint({"b": 2, "a": 1})
    assert source_fingerprint(GOOD) == source_fingerprint(GOOD)
    assert source_fingerprint(GOOD) != source_fingerprint(FUTURE)


def test_structure_signature_and_similarity():
    sig_a = structure_signature("import pandas as pd\ndef factor(env):\n    return (pd.DataFrame(env.c) / pd.DataFrame(env.c).shift(20)).values\n")
    sig_b = structure_signature("import pandas as pd\ndef factor(env):\n    d = pd.DataFrame(env.c)\n    return (d / d.shift(20)).values\n")
    assert "shift[20]" in sig_a["set"]
    assert signature_similarity(sig_a, sig_b) > 0.6, "同构因子签名应相似"
    sig_c = structure_signature("import numpy as np\ndef factor(env):\n    return np.log(env.v + 1.0)\n")
    assert signature_similarity(sig_a, sig_c) < 0.3, "不同方法签名应低相似"


def test_scan_inefficiency():
    assert scan_inefficiency(GOOD)["ok"] is True
    hits = scan_inefficiency(LOOPY)
    assert hits["ok"] is False and len(hits["hits"]) >= 2, hits
    rowiter = scan_inefficiency("import pandas as pd\ndef factor(env):\n    df = pd.DataFrame(env.c)\n    return df.apply(lambda r: r).values\n")
    assert rowiter["ok"] is False


def test_red_flags_and_verdict():
    absurd = {"ic_ir_train": 6.9, "ic_n_train": 100,
              "column_perm_train": {"z": 8.0}, "beta_exposure": 0.1}
    gv = red_flags_and_verdict(absurd)
    assert gv["verdict"] == "needs_review" and any("离谱" in f for f in gv["red_flags"])
    good = {"ic_ir_train": 0.8, "ic_n_train": 100,
            "column_perm_train": {"z": 4.0}, "beta_exposure": 0.1}
    assert red_flags_and_verdict(good)["verdict"] == "pass"
    betaish = {"ic_ir_train": 0.8, "ic_n_train": 100,
               "column_perm_train": {"z": 4.0}, "beta_exposure": 0.5}
    assert red_flags_and_verdict(betaish)["verdict"] == "needs_review"
    weak = {"ic_ir_train": 0.1, "ic_n_train": 100,
            "column_perm_train": {"z": 1.2}, "beta_exposure": 0.1}
    assert red_flags_and_verdict(weak)["verdict"] == "fail"


def test_evaluate_full_diagnosis_discipline():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        diag = b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD,
                                              "stage": "development"})
        # 纪律层字段全到位
        assert diag["verdict"] in ("pass", "fail", "needs_review")
        assert isinstance(diag.get("red_flags"), list)
        assert "deflated_train" in diag and diag["deflated_train"]["n_trials"] >= 1
        assert "train_sensitivity" in diag
        assert diag["train_sensitivity"]["verdict"] in (
            "stable", "magnitude_fragile", "sign_fragile", "insufficient")
        # 敏感性检验不得触碰 sel_end 之后的日期（test 保护）
        variants = diag["train_sensitivity"]["variants"]
        for v in variants:
            assert v["offset_months"] in (-3, -2, -1, 1, 2, 3)
        # _meta 溯源三元组
        meta = diag["_meta"]
        assert meta["engine_version"] and meta["source_hash"] and meta["fingerprint"]
        assert meta["receipt"]
        # 引擎层 trail 自动记录
        trail = json.loads((Path(d) / "state" / "trail_engine.json").read_text(encoding="utf-8"))
        assert any(e["source_hash"] == meta["source_hash"] for e in trail)


def test_causality_enforced_before_evaluate():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        try:
            b.dispatch("factor.evaluate", {"envId": "primary", "source": FUTURE,
                                           "stage": "development"})
            raise AssertionError("前视因子必须被拒绝评估")
        except BridgeError as e:
            assert e.code == -32003
        # 缓存生效：第二次同 source 直接从缓存拒绝
        try:
            b.dispatch("factor.evaluate", {"envId": "primary", "source": FUTURE,
                                           "stage": "development"})
            raise AssertionError("缓存拒绝同样生效")
        except BridgeError as e:
            assert e.code == -32003


def test_receipt_verification_flow():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        diag = b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD,
                                              "stage": "development"})
        # 1) 带 receipt 的真实诊断 → verified
        r = b.dispatch("registry.submit", {
            "envId": "primary", "source": GOOD, "name": "mom20",
            "diagnosis": diag})
        assert r["receipt_verified"] is True and r["entry"]["verified"] is True
        # 2) 编造数字（改 ic_ir_train 但偷 receipt）→ 验证失败
        forged = dict(diag)
        forged["ic_ir_train"] = 99.0
        r2 = b.dispatch("registry.submit", {
            "envId": "primary", "source": GOOD + "\n# variant", "name": "forged",
            "diagnosis": forged})
        assert r2["receipt_verified"] is False and r2["entry"]["verified"] is False
        # 3) 同一 source 换名重复登记 → 铁律拒绝
        try:
            b.dispatch("registry.submit", {
                "envId": "primary", "source": GOOD, "name": "rename_dup",
                "diagnosis": diag})
            raise AssertionError("同一因子换名重登应被铁律拒绝")
        except BridgeError as e:
            assert "铁律" in e.message


def test_memory_pool_dedup_and_falsified():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        # 双池对表直接测（evaluate 的 pass 门控按设计只放 pass/needs_review 入池；
        # 随机游走面板动量因子 verdict=fail，不进强池——单独测池逻辑）
        from dsh_factor_mining.factor.memory_pool import MemoryPool
        pool = MemoryPool(Path(d) / "state")
        rng = np.random.default_rng(7)
        F1 = rng.normal(size=(60, 40))
        F_same = F1 + rng.normal(scale=1e-6, size=F1.shape)  # 数值上同因子
        F_diff = rng.normal(size=(60, 40))                    # 独立因子
        sig_idx = np.arange(0, 60, 5)
        pool.offer_active(F1, sig_idx, "def f1(): return F1", "f1", ic_ir=0.8)
        sus_same = pool.check(F_same, sig_idx, "def f1(): return F1  # copy")
        assert sus_same["duplicate_suspect"] is not None
        assert sus_same["duplicate_suspect"]["corr"] > 0.95
        sus_diff = pool.check(F_diff, sig_idx, "def other(): return F2")
        assert sus_diff["duplicate_suspect"] is None
        # 证伪入池（record_explored with source）
        b.dispatch("paths.append", {"layer": "explored", "entry": {
            "exploration": "20日动量在该面板不可用",
            "evidence": "column-perm z 不显著",
            "root_cause": "面板为随机游走，无动量结构",
            "source": GOOD}})
        stats = b.dispatch("state.trail_summary", {})["pool"]
        assert stats["falsified"] >= 1, stats
        # 证伪池成员拒绝回强池（同一 source = GOOD；重建实例从盘读最新池）
        pool = MemoryPool(Path(d) / "state")
        r = pool.offer_active(F1, sig_idx, GOOD, "f1_reborn", ic_ir=0.9)
        assert r["admitted"] is False and "证伪池" in r["note"]


def test_trail_summary_and_export_report():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD,
                                       "stage": "development"})
        summary = b.dispatch("state.trail_summary", {})
        assert summary["evaluations"]["total"] >= 1
        assert "termination" in summary and "pool" in summary
        rep = b.dispatch("report.export", {"envId": "primary", "source": GOOD,
                                           "name": "mom20"})
        assert rep["ok"] is True and "mom20" in rep["content"]
        assert Path(rep["path"]).exists()
        # 未评估过的因子导出 → 明确报错
        rep2 = b.dispatch("report.export", {"envId": "primary",
                                            "source": GOOD + "\n# never evaluated",
                                            "name": "ghost"})
        assert rep2["ok"] is False


def test_record_schema_enforcement():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        for layer, bad in (("explored", {"exploration": "x"}),   # 缺 evidence/root_cause
                           ("trail", {"round": 1})):             # 缺 signal/attribution/...
            try:
                b.dispatch("paths.append", {"layer": layer, "entry": bad})
                raise AssertionError(f"{layer} 缺字段应被 schema 拒绝")
            except BridgeError as e:
                assert "必填" in e.message


def test_memory_estimate_in_probe():
    with tempfile.TemporaryDirectory() as d:
        data = Path(d) / "panel.parquet"
        _panel(data, T=800, N=40)
        b = Bridge(state_root=str(Path(d) / "state"))
        probe = b.dispatch("data.probe", {"path": str(data)})
        est = probe.get("memory_estimate", {})
        assert est.get("estimate_gb") is not None and est["estimate_gb"] > 0
        assert "warning" not in est  # 小数据无警告
