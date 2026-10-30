# coding=utf-8
"""E1/E2 验收（2026-10-06 假阴性救助线任务 A）：evidence_ledger 旁路账本
与 evidence_fit 冻结拟合。

- E1：评估落账尾部追加 evidence_ledger.jsonl（锁内、失败不阻断）；
  同输入重跑值逐位一致；evidence.enabled=false 后不写且评估照常；
  证据值不进 trail_engine 条目（红线 2 在账本侧的断言面）。
- E2：v1 拟合从 random 出身池现算；残差=手工公式；refit 版本递增、
  旧档留存、旧 fit_v 残差仍可复算（红线 11）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.factor import evidence as ev  # noqa: E402
from dsh_factor_mining.rescue_ops import cmd_evidence_refit  # noqa: E402
from dsh_factor_mining.state import MINING_CONFIG  # noqa: E402

SRC = ("def factor(env):\n"
       "    import pandas as pd\n"
       "    c = pd.DataFrame(env.c)\n"
       "    return (c / c.shift(5) - 1.0).values\n")


def _make_bridge(tmp_path: Path) -> Bridge:
    data = tmp_path / "panel.parquet"
    rng = np.random.default_rng(4)
    dates = pd.bdate_range("2019-01-02", periods=600)
    rows = []
    for i in range(30):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, 600))
        for t in range(600):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}", "open": p * 1.001,
                         "high": p * 1.01, "low": p * 0.99, "close": p,
                         "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "etf": {"source": {"type": "parquet", "path": str(data)}, "layout": "long",
                "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                            "high": "high", "low": "low", "close": "close",
                            "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "etf"})
    b.dispatch("factor.random_generate",
               {"envId": "etf", "mode": "null-calibration", "n": 5})
    return b


def _read_ledger(root: Path) -> list[dict]:
    p = Path(root) / "evidence_ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------------------
# A4 公式（手工算例逐位）
# ---------------------------------------------------------------------------
def test_a4_manual_value():
    s = [0.1] * 10 + [-0.3] + [0.1] * 9
    # cumsum: 至 idx9=1.0；idx10=0.7（dd=1.0−0.7=0.3）；期末=1.6 → 0.3/1.6
    assert abs(ev.a4_dd_ratio(s) - 0.3 / 1.6) < 1e-12


def test_a4_sign_alignment_and_guards():
    s = [0.1] * 10 + [-0.3] + [0.1] * 9
    assert abs(ev.a4_dd_ratio([-x for x in s]) - ev.a4_dd_ratio(s)) < 1e-12
    assert ev.a4_dd_ratio([0.0] * 20) is None          # 期末累计退化
    assert ev.a4_dd_ratio([0.1, 0.2, 0.3]) is None     # 长度 <5
    assert ev.a4_dd_ratio([0.1] * 5 + [float("nan")]) is None


# ---------------------------------------------------------------------------
# E1：ledger 写时
# ---------------------------------------------------------------------------
def test_ledger_schema_and_replay_identity(tmp_path):
    b = _make_bridge(tmp_path)
    d1 = b.dispatch("factor.evaluate",
                    {"envId": "etf", "source": SRC, "stage": "development"})
    d2 = b.dispatch("factor.evaluate",
                    {"envId": "etf", "source": SRC, "stage": "development"})
    root = Path(b.state_root)
    lines = _read_ledger(root)
    assert len(lines) == 2, "每次评估各留一行（重评=更新试验也留痕）"
    for ln in lines:
        assert set(ln) == {"ts", "hash", "horizon", "sr_abs", "origin",
                           "a4_dd_ratio", "fit_v"}
        assert ln["sr_abs"] > 0 and ln["a4_dd_ratio"] > 0
    a, c = {k: v for k, v in lines[0].items() if k != "ts"}, \
           {k: v for k, v in lines[1].items() if k != "ts"}
    assert a == c, "同输入重跑：除时间戳外逐位一致"
    assert lines[0]["hash"] == d1.get("_meta", {}).get("source_hash") or \
        lines[0]["hash"] == lines[1]["hash"]


def test_ledger_disabled_switch(tmp_path):
    b = _make_bridge(tmp_path)
    MINING_CONFIG["evidence"] = {"enabled": False}
    try:
        d = b.dispatch("factor.evaluate",
                       {"envId": "etf", "source": SRC, "stage": "development"})
        assert d.get("verdict") is not None, "关开关后评估照常"
    finally:
        del MINING_CONFIG["evidence"]
    assert _read_ledger(b.state_root) == [], "关=旁路整段跳过（逐比特现状）"


def test_ledger_values_not_in_trail_or_response(tmp_path):
    b = _make_bridge(tmp_path)
    d = b.dispatch("factor.evaluate",
                   {"envId": "etf", "source": SRC, "stage": "development"})
    blob = json.dumps(d, ensure_ascii=False, default=str)
    assert "a4_dd" not in blob and "evidence" not in blob and "fit_v" not in blob
    trail_blob = (Path(b.state_root) / "trail_engine.json").read_text(encoding="utf-8")
    assert "a4_dd" not in trail_blob and "evidence_ledger" not in trail_blob


# ---------------------------------------------------------------------------
# E2：拟合冻结与残差
# ---------------------------------------------------------------------------
def _synthetic_fit_root(tmp_path: Path) -> Path:
    """30 个 random 出身试验单元（含一个期末退化 sketch——不计入拟合）。"""
    root = tmp_path / "fitroot"
    root.mkdir()
    rng = np.random.default_rng(7)
    trail = []
    for i in range(30):
        sk = list(np.round(rng.normal(0.05, 0.3, 60), 4))
        if i == 5:
            sk = [0.0] * 60
        trail.append({"source_hash": f"a{i:04x}", "stage": "development",
                      "horizon": 10, "origin": "random", "ts": f"2026-09-0{i % 9 + 1}T00:00:0{i % 10}",
                      "ic_series_sketch": sk,
                      "dsr_stats": {"sr_hat": float(0.2 + 0.02 * i),
                                    "skew": 0.0, "kurt": 3.0, "n_obs": 60},
                      "ic_ir": float(0.2 + 0.02 * i), "red_flags": []})
    # 非 random 出身不进拟合集
    trail.append({**trail[0], "source_hash": "bbbb", "origin": "hypothesis"})
    (root / "trail_engine.json").write_text(
        json.dumps(trail, ensure_ascii=False), encoding="utf-8")
    return root


def test_refit_v1_residual_manual_and_versioning(tmp_path):
    root = _synthetic_fit_root(tmp_path)
    out = cmd_evidence_refit(str(root))
    assert out["version"] == "v1"
    assert out["n_fit"] == 29, "30 random 单元 −1 期末退化 = 29"
    fit = ev.load_fit(str(root))
    b0, b1, b2 = fit["v1"]["coef"]
    # 手工 OLS 复算（拟合集=29 个有效样本，二次基）
    xs, ys = [], []
    rng = np.random.default_rng(7)
    for i in range(30):
        sk = list(np.round(rng.normal(0.05, 0.3, 60), 4))
        if i == 5:
            sk = [0.0] * 60
        a4 = ev.a4_dd_ratio(sk)
        if a4 is None:
            continue
        xs.append(abs(0.2 + 0.02 * i))
        ys.append(a4)
    X = np.stack([np.ones_like(xs), xs, np.asarray(xs) ** 2], axis=1)
    beta, *_ = np.linalg.lstsq(X, np.asarray(ys), rcond=None)
    assert np.allclose([b0, b1, b2], beta, atol=1e-6)
    # 残差=手工公式
    x, y = xs[0], ys[0]
    assert abs(ev.residual(fit["v1"]["coef"], x, y) - (y - (b0 + b1 * x + b2 * x * x))) < 1e-12


def test_refit_versioning_keeps_old(tmp_path):
    root = _synthetic_fit_root(tmp_path)
    cmd_evidence_refit(str(root))
    # 换拟合集（追加 10 个 random 单元）→ v2；v1 留存且可复算
    trail = json.loads((root / "trail_engine.json").read_text(encoding="utf-8"))
    rng = np.random.default_rng(11)
    for i in range(10):
        trail.append({"source_hash": f"c{i:04x}", "stage": "development",
                      "horizon": 10, "origin": "random",
                      "ts": "2026-09-08T00:00:00",
                      "ic_series_sketch": list(np.round(rng.normal(0.1, 0.2, 60), 4)),
                      "dsr_stats": {"sr_hat": 0.3, "skew": 0.0, "kurt": 3.0,
                                    "n_obs": 60},
                      "ic_ir": 0.3, "red_flags": []})
    (root / "trail_engine.json").write_text(
        json.dumps(trail, ensure_ascii=False), encoding="utf-8")
    out2 = cmd_evidence_refit(str(root))
    assert out2["version"] == "v2" and out2["n_fit"] == 39
    fit = ev.load_fit(str(root))
    assert "v1" in fit and "v2" in fit, "旧档留存（红线 11）"
    assert fit["v2"]["supersedes"] == "v1"
    assert ev.latest_fit_version(fit) == "v2"
    r_old = ev.residual(fit["v1"]["coef"], 0.4, 0.5)
    assert r_old is not None, "旧 fit_v 残差仍可复算"


def test_ledger_fit_v_follows_latest(tmp_path):
    """E1×E2 联动：root 有拟合档时，ledger 行 fit_v=当前最新版本。"""
    import shutil
    root = _synthetic_fit_root(tmp_path)
    cmd_evidence_refit(str(root))
    b = _make_bridge(tmp_path)
    shutil.copy(ev.fit_path(root), ev.fit_path(b.state_root))
    b.dispatch("factor.evaluate",
               {"envId": "etf", "source": SRC, "stage": "development"})
    lines = _read_ledger(b.state_root)
    assert lines and lines[-1]["fit_v"] == "v1"
