# coding=utf-8
"""dispatch 出口 lossless 消毒回归（2026-08-25 factor_trail_summary 事故）。

事故链：ic_series_sketch 的 round(-0.00004, 4) = -0.0 → json.dumps 写
裸 -0.0 → JS JSON.parse 得 -0（不报错）→ DSH walkJsonValue 拒绝
（Object.is(x,-0)）→ tool "factor_trail_summary" returned invalid
output: value is not lossless JSON。生产 trail_engine.json 实测 66 个
-0.0 字面量、last-5 命中一处。

锁定：写入口归一化 + dispatch 出口消毒（NaN/±Inf→None、-0.0→+0.0、
numpy 标量→原生）——存量脏文件靠出口层兜底。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, _lossless  # noqa: E402


def _assert_js_lossless(x, path="root"):
    """模拟 DSH walkJsonValue 的拒绝条件。"""
    if x is None or isinstance(x, (str, bool)):
        return
    if isinstance(x, float):
        assert math.isfinite(x), f"{path} 非有限"
        assert not (x == 0.0 and math.copysign(1, x) < 0), f"{path} 是 -0.0"
        return
    if isinstance(x, int):
        return
    if isinstance(x, dict):
        for k, v in x.items():
            _assert_js_lossless(v, f"{path}.{k}")
        return
    if isinstance(x, list):
        for i, v in enumerate(x):
            _assert_js_lossless(v, f"{path}[{i}]")
        return
    raise AssertionError(f"{path} 非纯 JSON 值: {type(x)}")


def test_lossless_sanitizer_unit():
    out = _lossless({"nan": float("nan"), "inf": float("inf"),
                     "neg0": -0.0, "zero": 0.0, "npf": np.float64(1.5),
                     "npi": np.int64(3), "nested": [-0.0, [float("-inf")]],
                     "s": "x", "ok": 2.5})
    assert out["nan"] is None and out["inf"] is None
    assert out["neg0"] == 0.0 and math.copysign(1, out["neg0"]) > 0
    assert out["zero"] == 0.0
    assert isinstance(out["npf"], float) and out["npf"] == 1.5
    assert isinstance(out["npi"], int) and out["npi"] == 3
    assert out["nested"] == [0.0, [None]]
    _assert_js_lossless(out)


def test_trail_summary_with_dirty_file(tmp_path):
    """存量脏文件（-0.0 / NaN 字面量）→ trail_summary 响应必须过
    JS lossless 检查（出口消毒兜底）。"""
    state = tmp_path / "state"
    state.mkdir()
    # 直接构造含 -0.0 与 NaN 字面量的 engine trail（json.dumps 默认
    # allow_nan 写出 NaN token；json.loads 读回 float('nan')）
    dirty = [{"ts": "2026-08-25T00:00:00", "envId": "primary",
              "source_hash": "h1", "stage": "development", "horizon": 20,
              "ic_ir": 0.5, "ic_series_sketch": [-0.0, 0.1, -0.0]},
             {"ts": "2026-08-25T00:00:01", "envId": "primary",
              "source_hash": "h2", "stage": "development", "horizon": 20,
              "ic_ir": float("nan"), "ic_series_sketch": [float("nan")]}]
    (state / "trail_engine.json").write_text(
        json.dumps(dirty, allow_nan=True), encoding="utf-8")
    b = Bridge(state_root=str(state), execution_mode="in_process")
    r = b.dispatch("state.trail_summary", {})
    _assert_js_lossless(r)
    # 严格 dumps 也必须通过（无 NaN token）
    json.dumps(r, allow_nan=False)


def test_sketch_write_normalizes_negzero(tmp_path):
    """写入口归一化：evaluate 落盘的 sketch 不含 -0.0。"""
    rng = np.random.default_rng(4)
    T, N = 700, 35
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, T)))
        for t in range(T):
            p = float(c[t])
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = tmp_path / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"},
                    "calibration": {"dev_end": "2020-06-01",
                                    "sel_end": "2021-03-01"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    # 一个会产生微小负 IC 值的因子（随机 → IC ≈ 0，round 后必出 -0.0/0.0）
    b.dispatch("factor.evaluate", {
        "envId": "primary", "stage": "development",
        "source": "import numpy as np\n\ndef factor(env):\n"
                  "    return np.random.default_rng(1).normal("
                  "0, 0.01, env.c.shape)\n"})
    te = json.loads((tmp_path / "state" / "trail_engine.json"
                     ).read_text(encoding="utf-8"))
    for e in te:
        for v in (e.get("ic_series_sketch") or []):
            if v == 0.0:
                assert math.copysign(1, v) > 0, "sketch 写入了 -0.0"
