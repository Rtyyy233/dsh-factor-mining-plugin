# coding=utf-8
"""registry.get 紧凑视图回归（2026-08-27 规划书 WS-A / D1）。

取证动机：全量 registry.get 响应 232KB（29 条 × ~8KB），DH 呈现层按
字符截断且 registry 按时间序追加——截掉的正是最新条目（拒收理由、
tracks/dual_pass 标注在每条 entry 尾部）。紧凑投影每条 ≤ ~200B，
截断永不触发；无 agent 侧全量入口（全量 = 人读 registry.json）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402

_LONG_REASON = "G3 拒收：spread_ir=0.312 低于门 0.471（E[max|X|]=2.31σ×s0，N_eff=29）——" \
               "尾部线多重检验未过，继续构造变体或换方向，勿重复提交同源因子"


def _entry(name: str, *, accepted: bool, **kw) -> dict:
    # 生产 entry ≈ 8KB/条（diagnosis 全量诊断 + noise_gate/day_perm/
    # flatness 报告）；尺寸预算测试需要逼真的重载荷
    e = {"name": name, "signal": f"sig:{name}", "accepted": accepted,
         "ic_ir_train": 0.31, "source": "def factor(env): ...",
         "source_hash": f"hash_{name}", "engine_version": "0.1.0",
         "verdict": "pass" if accepted else "reject",
         "diagnosis": {"ic_ir_train": 0.31, "ic_mean_train": 0.02,
                       "payload": "x" * 4000},
         "noise_gate": {"z": 0.5, "n_valid": 50, "detail": "y" * 1500},
         "day_perm": {"p_align": 0.3, "detail": "z" * 800},
         "flatness": {"n_params": 2, "detail": "w" * 800}}
    e.update(kw)
    return e


def _accepted_entry(name: str, basis: str = "ic", dual: bool = False) -> dict:
    return _entry(
        name, accepted=True, admit_basis=basis, dual_pass=dual,
        ts=f"2026-08-27T10:00:{name[-1]}",
        tracks={"ic": {"accepted": True, "reason": "ok"},
                "tail": {"accepted": dual, "reason": "ok" if dual else "G3"}},
        diagnosis={"ic_ir_train": 0.31,
                   "tail": {"spread_ir": 0.51 if dual else 0.12}})


def _rejected_entry(name: str, reject_kind: str, reason: str) -> dict:
    return _entry(
        name, accepted=False, reject_kind=reject_kind, reason=reason,
        ts=f"2026-08-27T11:00:{name[-1]}",
        tracks={"ic": {"accepted": False, "reason": reason[:300]},
                "tail": {"accepted": False, "reason": reason[:300]}},
        diagnosis={"ic_ir_train": 0.05, "tail": {"spread_ir": 0.08}})


def _write_registry(root: Path, entries: list) -> None:
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "registry.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8")


def _bridge(root: Path) -> Bridge:
    return Bridge(state_root=str(root / "state"), execution_mode="in_process")


# ---- 1. 形状：白名单字段 / count / by_track ----

def test_registry_get_compact_whitelist(tmp_path):
    """每条 entry 只含投影字段（无 diagnosis/noise_gate/flatness/source
    等大字段）；count/accepted/by_track 计数正确。"""
    entries = [
        _accepted_entry("f_ic_1", basis="ic"),
        _accepted_entry("f_dual_2", basis="tail", dual=True),
        _accepted_entry("f_tail_3", basis="tail"),
        _rejected_entry("f_rej_4", "substantive", _LONG_REASON),
        _rejected_entry("f_rej_5", "procedural", "G2 无法判定：spread 噪声门 z 不可计算"),
    ]
    _write_registry(tmp_path, entries)
    b = _bridge(tmp_path)
    out = b.dispatch("registry.get", {})
    assert "registry" not in out, "全量 registry 键不得再出现（D1：无 agent 侧全量入口）"
    assert out["count"] == 5, out
    assert out["accepted"] == 3, out
    assert out["by_track"] == {"ic": 1, "tail": 1, "dual": 1}, out
    allowed = {"name", "ts", "accepted", "admit_basis", "tracks",
               "ic_ir", "spread_ir", "net_spread_ir", "turn",
               "break_even_cost", "reject_kind", "reject_reason"}
    for e in out["entries"]:
        assert set(e.keys()) <= allowed, e
    by_name = {e["name"]: e for e in out["entries"]}
    # 双轨主数字
    assert by_name["f_dual_2"]["spread_ir"] == 0.51, by_name["f_dual_2"]
    assert by_name["f_ic_1"]["tracks"] == {"ic": True, "tail": False}
    assert by_name["f_dual_2"]["tracks"] == {"ic": True, "tail": True}
    # 拒收条目：程序性/实质性 + 理由摘要
    rej = by_name["f_rej_4"]
    assert rej["reject_kind"] == "substantive"
    assert rej["reject_reason"].startswith("G3 拒收")
    assert len(rej["reject_reason"]) <= 120
    proc = by_name["f_rej_5"]
    assert proc["reject_kind"] == "procedural"
    # 接受条目不带拒收字段
    assert "reject_kind" not in by_name["f_ic_1"]
    assert "reject_reason" not in by_name["f_ic_1"]


def test_registry_get_size_budget(tmp_path):
    """尺寸预算：投影后响应序列化远小于全量（29 条量级 ≈ KB 级），
    每条投影 ≤ ~300B。"""
    entries = ([_accepted_entry(f"f_ok_{i:02d}", dual=(i % 3 == 0))
                for i in range(20)]
               + [_rejected_entry(f"f_rej_{i:02d}", "substantive", _LONG_REASON)
                  for i in range(9)])
    _write_registry(tmp_path, entries)
    b = _bridge(tmp_path)
    out = b.dispatch("registry.get", {})
    compact = len(json.dumps(out, ensure_ascii=False))
    full = len(json.dumps({"registry": entries}, ensure_ascii=False))
    assert full > 60_000, full  # 全量确实是数十 KB 级
    assert compact < full / 10, (compact, full)
    # 每条投影预算（字符数）：接受条目 ~150，拒收条目含 120 字符理由
    # 摘要 +0，均远低于呈现层截断阈值
    for e in out["entries"]:
        assert len(json.dumps(e, ensure_ascii=False)) < 400, e


def test_registry_get_empty(tmp_path):
    """空 registry → count=0 不炸。"""
    b = _bridge(tmp_path)
    out = b.dispatch("registry.get", {})
    assert out["count"] == 0 and out["entries"] == []
    assert out["accepted"] == 0


def test_registry_get_old_entries_without_new_fields(tmp_path):
    """旧条目（无 ts/admit_basis/tracks/dual_pass——2026-08-26 前）：
    投影容忍缺省，不炸，admit_basis 回退 ic。"""
    old = [{"name": "legacy_1", "signal": "s", "accepted": True,
            "reason": "ok", "ic_ir_train": 0.4,
            "diagnosis": {"ic_ir_train": 0.4},
            "source": "...", "source_hash": "h1"}]
    _write_registry(tmp_path, old)
    out = _bridge(tmp_path).dispatch("registry.get", {})
    assert out["count"] == 1
    e = out["entries"][0]
    assert e["ts"] is None and e["admit_basis"] == "ic"
    assert e["tracks"] == {"ic": False, "tail": False}


def test_registry_submit_stamps_ts(tmp_path):
    """新 submit 的 entry 落盘带 ts（投影的时间字段）。"""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(4)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
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
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    sub = b.dispatch("registry.submit", {
        "name": "ts_probe", "signal": "momentum",
        "source": "\ndef factor(env):\n    import pandas as pd\n"
                  "    c = pd.DataFrame(env.c)\n"
                  "    return (c / c.shift(20) - 1.0).values\n",
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is True, sub
    out = b.dispatch("registry.get", {})
    assert out["entries"][0]["ts"] is not None, out


# ---- 2. trail_summary：last_engine_trail 投影 + 前移 ----

def test_trail_summary_compact_projection(tmp_path):
    """last_engine_trail 条目白名单：无 ic_series_sketch/suspects/
    construction_fp/dsr_stats；tail 只留 spread_ir/placebo_z。"""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    entries = [{
        "ts": "2026-08-27T12:00:00", "envId": "primary",
        "source_hash": "a" * 32, "stage": "development", "horizon": 20,
        "ic_ir": 0.22, "verdict": "pass", "red_flags": ["f1", "f2", "f3"],
        "suspects": {"duplicate_suspect": "x"},
        "ic_series_sketch": [0.1] * 64,
        "construction_fp": [1] * 32,
        "dsr_stats": {"sr_hat": 0.3, "skew": 0.0, "kurt": 3.0, "n_obs": 60},
        "tail": {"spread_ir": 0.44, "tail_ic": 0.5, "selection_mh": [0] * 32,
                 "topn": {"placebo_z": 2.1, "periods": 152,
                          "net_mean": 0.001}},
    }]
    (state / "trail_engine.json").write_text(
        json.dumps(entries), encoding="utf-8")
    b = _bridge(tmp_path)
    s = b.dispatch("state.trail_summary", {})
    last = s["last_engine_trail"]
    assert len(last) == 1
    e = last[0]
    allowed = {"ts", "source_hash", "horizon", "stage", "ic_ir", "verdict",
               "red_flags", "tail"}
    assert set(e.keys()) == allowed, e
    assert e["source_hash"] == "a" * 12  # 前缀
    assert e["red_flags"] == ["f1", "f2"]  # 截 2
    assert e["tail"] == {"spread_ir": 0.44, "placebo_z": 2.1}
    ser = json.dumps(e, ensure_ascii=False)
    assert len(ser) < 300, len(ser)


def test_trail_summary_last_engine_trail_positioned_after_loop(tmp_path):
    """前移（WS-A.2）：last_engine_trail 位于 loop 之后——即使未来响应
    再被截断，最近历史最先存活。"""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "trail_engine.json").write_text(json.dumps([
        {"ts": "2026-08-27T12:00:00", "source_hash": "b" * 32,
         "stage": "development", "horizon": 20, "ic_ir": 0.1,
         "verdict": "pass"}]), encoding="utf-8")
    b = _bridge(tmp_path)
    s = b.dispatch("state.trail_summary", {})
    keys = list(s.keys())
    assert keys.index("loop") < keys.index("last_engine_trail") < keys.index(
        "evaluations"), keys


if __name__ == "__main__":
    import tempfile
    for fn in [test_registry_get_compact_whitelist,
               test_registry_get_size_budget,
               test_registry_get_empty,
               test_registry_get_old_entries_without_new_fields,
               test_trail_summary_compact_projection,
               test_trail_summary_last_engine_trail_positioned_after_loop]:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            fn(p)
            print(f"PASS {fn.__name__}")
    print("REGISTRY_VIEW_TESTS PASS")
