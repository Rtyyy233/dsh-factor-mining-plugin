# coding=utf-8
"""E3 验收（2026-10-06 假阴性救助线红线 2）：模型面零暴露。

响应面黑名单断言——evaluate / evaluate_batch / registry.submit /
state.trail_summary / registry.get / factor.null_landscape /
journal.read 的序列化响应不得含任何证据侧关键词。理由：暴露即激励
——模型见到 A4/拟合版本/回扫判据会向其适配，证据校准被侵蚀。
（与 test_prompt_blacklist.py 同目录常态化；该文件管模型面文本的
历史引用/黑话，本文件管证据值的运行时响应面。）
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

#: 证据侧关键词（值/键名/提示语一律不得出现在任何模型可见响应里）。
#: 裸词 "rescue" 不进黑名单——loop 三原语的"救援"原语（2026-10-03 交付）
#: 在响应面既有使用，与假阴性救助线是两个概念，词重叠非泄漏。
EVIDENCE_BLACKLIST = (
    "evidence", "a4_dd", "dd_ratio", "fit_v", "famsize",
    "a4_resid", "evidence_ledger", "evidence_fit", "rescue_ops",
    "human_rescue_layer", "rescue_round", "rescue_scans",
)

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


def _assert_clean(blob: str, surface: str):
    low = blob.lower()
    for kw in EVIDENCE_BLACKLIST:
        assert kw not in low, f"[{surface}] 响应面泄漏证据关键词「{kw}」"


def test_zero_exposure_all_surfaces(tmp_path):
    b = _make_bridge(tmp_path)

    d = b.dispatch("factor.evaluate",
                   {"envId": "etf", "source": SRC, "stage": "development"})
    _assert_clean(json.dumps(d, ensure_ascii=False, default=str), "factor.evaluate")

    bt = b.dispatch("factor.evaluate_batch", {"envId": "etf", "items": [
        {"source": SRC, "stage": "development"},
        {"source": SRC.replace("shift(5)", "shift(9)"),
         "stage": "development"}]})
    _assert_clean(json.dumps(bt, ensure_ascii=False, default=str),
                  "factor.evaluate_batch")

    # submit（弱因子走拒绝路径也测——拒绝文案同属模型面）
    sub = b.dispatch("registry.submit", {
        "envId": "etf", "name": "probe_zero_exposure", "source": SRC,
        "signal": "s", "diagnosis": d})
    _assert_clean(json.dumps(sub, ensure_ascii=False, default=str),
                  "registry.submit")

    ts = b.dispatch("state.trail_summary", {})
    _assert_clean(json.dumps(ts, ensure_ascii=False, default=str),
                  "state.trail_summary")

    rg = b.dispatch("registry.get", {})
    _assert_clean(json.dumps(rg, ensure_ascii=False, default=str), "registry.get")

    # null_landscape：盘上文件含 a4_dd/famsize 分位段，响应面必须剥离
    nl = b.dispatch("factor.null_landscape", {"envId": "etf"})
    _assert_clean(json.dumps(nl, ensure_ascii=False, default=str),
                  "factor.null_landscape")

    jr = b.dispatch("journal.read", {})
    _assert_clean(json.dumps(jr, ensure_ascii=False, default=str), "journal.read")

    # trail_engine 账本条目（factor_trail_* 查回面）
    trail_blob = (Path(b.state_root) / "trail_engine.json").read_text(encoding="utf-8")
    _assert_clean(trail_blob.lower(), "trail_engine.json")


def test_null_landscape_segments_on_disk_but_not_in_response(tmp_path):
    """E4 联动：n=200 校准后盘上文件带 a4_dd/famsize 段，查询响应仍零暴露。"""
    b = _make_bridge(tmp_path)
    land = b.dispatch("factor.random_generate",
                      {"envId": "etf", "mode": "null-calibration", "n": 8})
    assert isinstance(land, dict) and land.get("n_generated") == 8
    disk = json.loads((Path(b.state_root) / "null_landscape.json").read_text(
        encoding="utf-8"))
    # 盘上分位段结构（E4 schema：p10/p50/p90/p95 + n）
    h = str(land["horizons"][0])
    assert h in disk.get("a4_dd", {}) and h in disk.get("famsize", {})
    for seg in (disk["a4_dd"][h], disk["famsize"][h]):
        for q in ("p10", "p50", "p90", "p95"):
            assert q in seg or seg.get("n") == 0
    nl = b.dispatch("factor.null_landscape", {"envId": "etf"})
    blob = json.dumps(nl, ensure_ascii=False, default=str).lower()
    assert "a4_dd" not in blob and "famsize" not in blob and "evidence" not in blob
