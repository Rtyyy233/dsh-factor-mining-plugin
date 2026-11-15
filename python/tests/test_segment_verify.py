# coding=utf-8
"""段隔离验证管线专项单测（2026-11-05 任务书 §七 E）。

覆盖：
- SPRT 边界/随机化统计性质（确定性种子、带内概率、段尽拒绝、早通过省段）
- 族预算记账（60/周封顶、按构造指纹分桶）
- 灾难熔断（对齐 IR < -0.5×|dev_IR| 立即拒）
- 段冻结完整性（篡改即拒 fail-closed）
- dispatch 出口 OOS 红action（verify/tier/decay_band 键剥离）
- AST IO 黑名单（含混淆用例：__import__/read_*/os.system/Path）
- 阈值冻结值（T_acc=2.6/band=0.3 与 A4 校准报告一致）
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from dsh_factor_mining.factor import verify
from dsh_factor_mining.factor.hardening import io_blacklist_check

SEGS = [
    {"id": "seg1", "start": "2022-01-01", "end": "2023-12-31",
     "burn_baseline": 103},
    {"id": "seg2", "start": "2024-01-01", "end": "2025-12-31",
     "burn_baseline": 0},
]


def _dates():
    """两段各 ~52 个周度观测。"""
    out = []
    for y0 in (2022, 2023):
        for w in range(26):
            out.append(f"{y0}-{(w // 4) + 1:02d}-{(w % 4) * 7 + 1:02d}")
    for y0 in (2024, 2025):
        for w in range(26):
            out.append(f"{y0}-{(w // 4) + 1:02d}-{(w % 4) * 7 + 1:02d}")
    return out


def _series(ir: float, n: int, seed: int, noise: float = 0.35):
    rng = np.random.default_rng(seed)
    return [ir + rng.standard_normal() * noise for _ in range(n)]


# ---------------------------------------------------------------------------
# SPRT 统计性质
# ---------------------------------------------------------------------------

def test_frozen_constants_match_calibration():
    """T_acc/band 与 A4 回放校准冻结值一致（改动须走审计期人签核）。"""
    assert verify.T_ACC == 2.6
    assert verify.BAND == 0.3
    assert verify.DISASTER_K == 0.5
    assert verify.WEAK_K == 0.25
    assert verify.WEEKLY_FAMILY_CAP == 60


def test_sprt_strong_factor_early_pass_saves_segments():
    """强因子：段 1 后 t≥T+band → 通过且不评估段 2（省处女段）。"""
    dates = _dates()
    vals = _series(0.5, len(dates), seed=11)   # t≈0.5*sqrt(52)≈3.6 ≥ 2.9
    r = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "s1")
    assert r["verdict"] == "pass"
    assert r["n_segments_evaluated"] == 1, r   # 早通过省段
    assert r["cumulative_t"] >= verify.T_ACC + verify.BAND
    assert not r["randomized"]


def test_sprt_null_factor_rejects_after_all_segments():
    """零假设因子：段尽 t 低 → 拒绝（全段评估）。"""
    dates = _dates()
    vals = _series(0.0, len(dates), seed=0)
    r = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "s2")
    assert r["verdict"] == "reject" and not r["disaster"]
    assert r["n_segments_evaluated"] == 2
    assert r["cumulative_t"] < verify.T_ACC - verify.BAND


def test_sprt_disaster_fuse_immediate():
    """灾难熔断：任一段对齐 IR < -0.5×|dev_IR| → 立即拒（先于累积）。"""
    dates = _dates()
    vals = _series(-0.9, len(dates), seed=3)   # 对齐后深负
    r = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "s3")
    assert r["verdict"] == "reject" and r["disaster"] is True
    assert r["n_segments_evaluated"] == 1


def test_sprt_weak_segment_registered_not_verdict():
    """弱段登记（对齐 IR < 0.25×|dev|）：tier 层消费，不影响 verdict 本身。"""
    dates = _dates()
    half = len(dates) // 2
    rng = np.random.default_rng(5)
    # 段 1 中等（t 不足早通过 → 段序继续）、段 2 弱（对齐 IR < 0.25×dev）
    vals = ([0.2 + rng.standard_normal() * 0.35 for _ in range(half)]
            + [0.05 + rng.standard_normal() * 0.35 for _ in range(half)])
    r = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "s4")
    assert r["n_segments_evaluated"] == 2
    assert "seg2" in r["weak_segments"]
    # 弱段只登记不改变 verdict 语义：本例累积 t 低 → 拒绝（弱段仍在案）


def test_sprt_deterministic_seed_replay():
    """同因子同段定义 → 同判定（边界随机化不是可重试彩票）。"""
    dates = _dates()
    vals = _series(0.35, len(dates), seed=9)
    r1 = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "seed-x")
    r2 = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "seed-x")
    assert r1 == r2
    r3 = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "seed-y")
    assert r3["verdict"] == r1["verdict"]  # 远离边界时判定稳定


def test_sprt_band_randomization_probability_shape():
    """带内 Φ 线性概率：两端 0/1、单调。"""
    lo = verify._pass_prob_in_band(verify.T_ACC - verify.BAND)
    hi = verify._pass_prob_in_band(verify.T_ACC + verify.BAND)
    mid = verify._pass_prob_in_band(verify.T_ACC)
    assert lo == 0.0 and hi == 1.0
    # Φ 线性：带内中点概率=Φ 轴上的中点（≈0.68 @T=2.6,band=0.3）
    assert 0.6 < mid < 0.78


def test_sprt_boundary_randomization_ratio():
    """边界带内大量因子：通过率≈理论概率（统计性质粗验）。"""
    dates = _dates()
    # 构造 t 落在带内：IR 使 t≈T_ACC
    rng = np.random.default_rng(42)
    n_pass = 0
    n = 120
    for i in range(n):
        # IR 均值=T_ACC/√n（IC 值噪声 σ=1）→ t 均值≈T_ACC（带内）
        vals = [verify.T_ACC / math.sqrt(len(dates))
                + rng.standard_normal() for _ in dates]
        r = verify.sprt_evaluate(dates, vals, 0.5, SEGS, f"band-{i}")
        n_pass += (r["verdict"] == "pass")
    # t 均值≈T_ACC → 通过率≈带内中点概率（0.6-0.78）± 抽样波动
    assert 0.4 < n_pass / n < 0.9, n_pass / n


def test_sprt_segment_below_min_observations_fails_closed():
    """段观测 <3 → SegmentsError（面板过期/段漂移——fail-closed 不判）。"""
    with pytest.raises(verify.SegmentsError):
        verify.sprt_evaluate(["2022-01-05"], [0.1], 0.5, SEGS, "s6")


# ---------------------------------------------------------------------------
# 段冻结完整性
# ---------------------------------------------------------------------------

def test_segments_tamper_detected(tmp_path):
    """篡改 segments.json（改段界）→ 完整性校验失败（fail-closed）。"""
    spec = verify.write_synthetic_segments(tmp_path)
    assert verify.load_segments(tmp_path)["content_sha"] == spec["content_sha"]
    spec2 = json.loads((tmp_path / "segments.json").read_text(encoding="utf-8"))
    spec2["segments"][0]["end"] = "2050-01-01"   # 篡改
    (tmp_path / "segments.json").write_text(
        json.dumps(spec2, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(verify.SegmentsError):
        verify.load_segments(tmp_path)


def test_segments_missing_fails_closed(tmp_path):
    with pytest.raises(verify.SegmentsError):
        verify.load_segments(tmp_path)


# ---------------------------------------------------------------------------
# 族预算
# ---------------------------------------------------------------------------

def test_family_fingerprint_stable_and_differentiating():
    s1 = "def factor(env):\n    return decay(rank(env.amount), 5)\n"
    s2 = "def factor(env):\n    return decay(rank(env.amount), 7)\n"   # 换字面量=同族
    s3 = "def factor(env):\n    return ts_rank(env.close, 10)\n"        # 不同族
    assert verify.family_fingerprint(s1) == verify.family_fingerprint(s2)
    assert verify.family_fingerprint(s1) != verify.family_fingerprint(s3)


def test_budget_exhaustion_and_band(tmp_path):
    fam = verify.family_fingerprint("def factor(env):\n    return env.close\n")
    st = verify.budget_status(tmp_path, fam)
    assert st["used"] == 0 and not st["exhausted"] and st["band"] == "充裕"
    for _ in range(verify.WEEKLY_FAMILY_CAP):
        st = verify.budget_spend(tmp_path, fam)
    assert st["exhausted"] and st["band"] == "用尽"
    assert st["used"] == verify.WEEKLY_FAMILY_CAP
    # 不同族不连坐
    fam2 = verify.family_fingerprint("def factor(env):\n    return rank(env.volume)\n")
    assert not verify.budget_status(tmp_path, fam2)["exhausted"]


def test_budget_texts_carry_no_numbers():
    """模型面三态文案：零数字零段数零原因（隔离红线）。"""
    for t in (verify.TEXT_PASS, verify.TEXT_REJECT, verify.TEXT_BUDGET,
              verify.TEXT_ERROR, verify.TEXT_SEGMENTS_BROKEN):
        assert isinstance(t, str) and t
        for bad in ("t=", "T=", "段 ", "IR", "p=", "次/", "余", "z="):
            assert bad not in t, (bad, t)


# ---------------------------------------------------------------------------
# dispatch 出口红action + 端到端二值判定
# ---------------------------------------------------------------------------

def test_lossless_redacts_oos_keys():
    from dsh_factor_mining.bridge import _lossless
    payload = {"accepted": True, "reason": "x",
               "entry": {"name": "f", "tier": "core",
                         "verify": {"cumulative_t": 3.1,
                                    "seg_evals": [{"ir": 0.5}]},
                         "decay_band": {"low": 0.2}},
               "verification": {"verdict": "pass"}}
    out = _lossless(payload)
    assert out["verification"]["verdict"] == "pass"      # 二值判定保留
    e = out["entry"]
    assert "verify" not in e and "tier" not in e and "decay_band" not in e
    assert e["name"] == "f"


def test_submit_full_pipeline_binary_verdict(tmp_path):
    """端到端：dev 评估 → 提交 → 内部段验证 → 二值判定且响应零 OOS 数字。"""
    import pandas as pd
    from dsh_factor_mining.bridge import Bridge
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
    (tmp_path / "panel.parquet").parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(tmp_path / "panel.parquet")
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet",
                               "path": str(tmp_path / "panel.parquet")},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high", "low": "low",
                                "close": "close", "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    # 低固定墙便于强因子过快检（只在本测试 stateRoot 生效）
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "null_landscape.json").write_text(json.dumps({
        "wall_mode": "fixed", "fixed_bar_ic": 0.2, "fixed_bar_tail": 0.05,
        "horizons": [20], "ic_ir": {"20": {"p10": -0.1, "p90": 0.1}},
    }), encoding="utf-8")
    # 与 test_bridge_smoke.BASELINE_SOURCE 同款（过因果/噪声门的已知构造）
    src = ("import pandas as pd\n\n"
           "def factor(env):\n"
           "    c = pd.DataFrame(env.c)\n"
           "    return (c / c.shift(20) - 1.0).values\n")
    diag = b.dispatch("factor.evaluate", {"envId": "primary", "source": src})
    sub = b.dispatch("registry.submit", {
        "name": "sprt_e2e", "signal": "x", "source": src,
        "diagnosis": diag,
        "flatness_params": [{"name": "k", "value": 20, "step": 5}]})
    assert "entry" in sub
    assert "verify" not in sub["entry"], sub["entry"].keys()
    assert "tier" not in sub["entry"]
    if sub["accepted"]:
        assert sub.get("verification", {}).get("verdict") == "pass"
        led = (tmp_path / "state" / "verify_ledger.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        assert led and "cumulative_t" in led[-1]
    else:
        assert "reason" in sub


# ---------------------------------------------------------------------------
# AST IO 黑名单（B7）
# ---------------------------------------------------------------------------

def test_io_blacklist_obfuscation_cases():
    cases = {
        "open": "def factor(env):\n    f = open('x')\n    return env.close",
        "read_parquet": ("import pandas as pd\ndef factor(env):\n"
                         "    return pd.read_parquet('a')"),
        "dunder_import": ("def factor(env):\n    m = __import__('os')\n"
                          "    return env.close"),
        "os_system": ("import os\ndef factor(env):\n    os.system('dir')\n"
                      "    return env.close"),
        "requests": "import requests\ndef factor(env):\n    return env.close",
        "pathlib": ("from pathlib import Path\ndef factor(env):\n"
                    "    return env.close"),
        "to_parquet": ("import pandas as pd\ndef factor(env):\n"
                       "    env.close.to_parquet('a')\n    return env.close"),
        "np_load": ("import numpy as np\ndef factor(env):\n"
                    "    np.load('a.npy')\n    return env.close"),
        "pickle": "import pickle\ndef factor(env):\n    return env.close",
        "socket": "import socket\ndef factor(env):\n    return env.close",
    }
    for name, src in cases.items():
        r = io_blacklist_check(src)
        assert r["rejected"], name
    clean = ("import numpy as np\nimport pandas as pd\n"
             "def factor(env):\n"
             "    c = pd.DataFrame(env.c)\n"
             "    return (c / c.shift(20) - 1.0).values\n")
    assert not io_blacklist_check(clean)["rejected"]


def test_compile_factor_rejects_io_source(tmp_path):
    from dsh_factor_mining.bridge import Bridge, BridgeError
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    with pytest.raises(BridgeError) as ei:
        Bridge._compile_factor("import pandas as pd\n\n"
                               "def factor(env):\n"
                               "    return pd.read_parquet('x')\n")
    assert "输入输出" in str(ei.value) or "纯计算" in str(ei.value)


# ---------------------------------------------------------------------------
# 尾轨段验证（2026-11-05 收尾任务书 §四 1：累积对象=逐采样日尾价差）
# ---------------------------------------------------------------------------

def test_tail_constants_frozen():
    """T_ACC_TAIL 与校准报告冻结值互锁（改动须走审计期人签核）。"""
    cal = (Path(__file__).resolve().parents[2]
           / "analysis/glm_mining/tail_sprt_calibration_20261105.json")
    assert verify.VERSION_TAIL != verify.VERSION
    if cal.exists():
        frozen = json.loads(cal.read_text(encoding="utf-8")) \
            .get("frozen_T_acc_tail")
        if frozen is not None:
            assert verify.T_ACC_TAIL == frozen
    # 回放校准一次定值（degraded 冻结：robust 召回档网格内不可达，
    # 违约最小者；保护两档全过——见校准报告）
    assert verify.T_ACC_TAIL == 2.5


def test_tail_sprt_strong_early_pass_saves_segments():
    """强尾价差：段 1 后 t≥T+band → 通过且不评估段 2（同 IC 版语义）。"""
    dates = _dates()
    vals = _series(0.5, len(dates), seed=21)
    r = verify.sprt_evaluate_tail(dates, vals, 0.5, SEGS, "t1")
    assert r["verdict"] == "pass"
    assert r["n_segments_evaluated"] == 1
    assert r["cumulative_t"] >= verify.T_ACC_TAIL + verify.BAND
    assert r["params"]["version"] == verify.VERSION_TAIL


def test_tail_sprt_null_rejects_after_all_segments():
    """零假设尾价差：段尽 t 低 → 拒绝（全段评估）。"""
    dates = _dates()
    vals = _series(0.0, len(dates), seed=20)
    r = verify.sprt_evaluate_tail(dates, vals, 0.5, SEGS, "t2")
    assert r["verdict"] == "reject" and not r["disaster"]
    assert r["n_segments_evaluated"] == 2
    assert r["cumulative_t"] < verify.T_ACC_TAIL - verify.BAND


def test_tail_sprt_disaster_fuse_immediate():
    """灾难熔断：段尾价差对齐 IR 深负 → 立即拒（先于累积）。"""
    dates = _dates()
    vals = _series(-0.9, len(dates), seed=23)
    r = verify.sprt_evaluate_tail(dates, vals, 0.5, SEGS, "t3")
    assert r["verdict"] == "reject" and r["disaster"] is True
    assert r["n_segments_evaluated"] == 1


def test_tail_sprt_weak_segment_registered():
    """弱段登记（对齐 IR < 0.25×|dev|）→ tier 层消费，不改 verdict 语义。"""
    dates = _dates()
    half = len(dates) // 2
    rng = np.random.default_rng(5)   # 与 IC 版弱段测试同种子（同轨迹镜像）
    vals = ([0.2 + rng.standard_normal() * 0.35 for _ in range(half)]
            + [0.05 + rng.standard_normal() * 0.35 for _ in range(half)])
    r = verify.sprt_evaluate_tail(dates, vals, 0.5, SEGS, "t4")
    assert r["n_segments_evaluated"] == 2
    assert "seg2" in r["weak_segments"]


def test_tail_seed_domain_independent_of_ic():
    """两轨种子域独立：同输入同 seed 下尾轨判定只依赖尾轨种子域，
    且各自确定性复现（边界随机化不是可重试彩票）。"""
    dates = _dates()
    rng = np.random.default_rng(77)
    vals = [verify.T_ACC_TAIL / math.sqrt(len(dates))
            + rng.standard_normal() for _ in dates]
    r_ic = verify.sprt_evaluate(dates, vals, 0.5, SEGS, "same-seed")
    r_tail = verify.sprt_evaluate_tail(dates, vals, 0.5, SEGS, "same-seed")
    assert r_ic["params"]["version"] != r_tail["params"]["version"]
    assert verify.sprt_evaluate_tail(dates, vals, 0.5, SEGS,
                                     "same-seed") == r_tail


def _drift_bridge(tmp_path):
    """合成面板 + 低固定墙桥：每符号漂移线性分布（-0.2%..+0.2%/日）——
    长动量在截面上真实可预测（尾门链 placebo 有真信号可越），供尾轨
    端到端/直调用例。"""
    import pandas as pd
    from dsh_factor_mining.bridge import Bridge
    rng = np.random.default_rng(4)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    drift = np.linspace(-0.002, 0.002, N)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(drift[i], 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(tmp_path / "panel.parquet")
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet",
                               "path": str(tmp_path / "panel.parquet")},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high", "low": "low",
                                "close": "close", "volume": "volume",
                                "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 5})
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "null_landscape.json").write_text(json.dumps({
        "wall_mode": "fixed", "fixed_bar_ic": 0.2, "fixed_bar_tail": 0.05,
        "horizons": [20], "ic_ir": {"20": {"p10": -0.1, "p90": 0.1}},
        "spread": {"20": {"std": 0.05}},
    }), encoding="utf-8")
    return b


TAIL_MOMENTUM_SRC = (
    "import pandas as pd\n\n"
    "def factor(env):\n"
    "    c = pd.DataFrame(env.c)\n"
    "    return (c / c.shift(60) - 1.0).values\n")


def test_compute_tail_spread_series_aligned(tmp_path):
    """尾价差序列：dates/spreads 严格对齐、全有限、长度 ≥3。"""
    b = _drift_bridge(tmp_path)
    env = b._require_panel_env("primary")
    dates, spreads = verify.compute_tail_spread_series(
        TAIL_MOMENTUM_SRC, env, None)
    assert len(dates) == len(spreads) >= 3
    assert all(isinstance(d, str) and d[:2] == "20" for d in dates)
    assert all(np.isfinite(s) for s in spreads)


def test_run_internal_verification_tail_direct(tmp_path):
    """直调主入口：dev 参照缺省由 dev 侧切片就地计算；ledger 带 track=tail；
    真发判定消耗族额度（与 IC 轨同桶）。"""
    import hashlib
    b = _drift_bridge(tmp_path)
    env = b._require_panel_env("primary")
    root = Path(b.state_root)
    # 自写带 dev_end 的段（覆盖 conftest 全覆盖窗——dev 切片需真实界）
    spec = {
        "line": "TEST", "panel": {"start": "2019-01-02", "end": "2099-12-31",
                                  "dev_end": "2021-06-01",
                                  "source": "synthetic-test"},
        "segments": [
            {"id": "seg1", "start": "2021-06-01", "end": "2099-12-31",
             "burn_baseline": 0}],
        "upgrade_clause": "synthetic-test"}
    spec["content_sha"] = hashlib.sha256(json.dumps(
        {k: spec[k] for k in ("line", "panel", "segments", "upgrade_clause")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    (root / "segments.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    # 覆盖 conftest 注入的 freeze 锚（sha 必须一致，否则 fail-closed）
    (root / "segments.freeze.json").write_text(json.dumps(
        {"frozen_at": "synthetic-test", "content_sha": spec["content_sha"],
         "authorized_by": "synthetic-test",
         "modification_rule": "synthetic-test"}, ensure_ascii=False),
        encoding="utf-8")
    out = verify.run_internal_verification_tail(
        str(root), TAIL_MOMENTUM_SRC, env, None, None, "taildirect0001",
        name="tail_direct")
    assert out["verdict"] in ("pass", "reject")
    rec = out["report"]
    assert rec["track"] == "tail" and rec["dev_spread_ir"] != 0
    led = (root / "verify_ledger.jsonl").read_text(encoding="utf-8")
    assert '"track": "tail"' in led
    fam = verify.family_fingerprint(TAIL_MOMENTUM_SRC)
    assert verify.budget_status(str(root), fam)["used"] == 1


def test_submit_tail_track_segment_verified_e2e(tmp_path):
    """端到端：尾轨提交接入段验证——响应零 tail_track_unverified_v1；
    若过 dev 门链则得二值判定且 ledger 记 track=tail。"""
    b = _drift_bridge(tmp_path)
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": TAIL_MOMENTUM_SRC})
    sub = b.dispatch("registry.submit", {
        "name": "tail_sprt_e2e", "signal": "x", "source": TAIL_MOMENTUM_SRC,
        "diagnosis": diag, "admit_basis": "tail",
        "flatness_params": [{"name": "k", "value": 60, "step": 5}]})
    blob = json.dumps(sub, ensure_ascii=False)
    assert "tail_track_unverified_v1" not in blob
    assert "verify" not in sub.get("entry", {})
    led_p = Path(b.state_root) / "verify_ledger.jsonl"
    if sub.get("accepted"):
        assert sub.get("verification", {}).get("verdict") == "pass"
        led = led_p.read_text(encoding="utf-8").strip().splitlines()
        assert led and '"track": "tail"' in led[-1]
    else:
        assert "reason" in sub


# ---------------------------------------------------------------------------
# ts 线（factor-mining-futures，2026-11-07 任务书 §八）：SPRT 终审端到端
# ---------------------------------------------------------------------------

TS_MOM_SRC = (
    "import pandas as pd\n\n"
    "def factor(env):\n"
    "    c = pd.Series(env.c)\n"
    "    mom = c / c.shift(60) - 1.0\n"
    "    return mom.values\n")


def _ts_momentum_bridge(tmp_path):
    """合成单品种时序面板 + 低固定墙桥：慢变正弦漂移（周期 300 日）使
    60 日动量在时间轴上真实可预测——ts 乘积序列 IR 显著，供 ts 线端到端
    （evaluate→提交链五门→SPRT 二值判定）与直调用例。"""
    import pandas as pd
    from dsh_factor_mining.bridge import Bridge
    rng = np.random.default_rng(11)
    T = 1600
    dates = pd.bdate_range("2018-01-02", periods=T)
    # 慢变漂移：月度尺度正弦——trailing 60d 均值收益预测未来 20d 收益
    mu = 0.006 * np.sin(2.0 * np.pi * np.arange(T) / 300.0)
    ret = mu + rng.normal(0.0, 0.008, T)
    c = 3500.0 * np.cumprod(1.0 + ret)
    o = c * (1.0 + rng.normal(0.0, 0.002, T))
    h = np.maximum(o, c) * 1.002
    l = np.minimum(o, c) * 0.998
    v = rng.lognormal(9.0, 0.4, T)
    oi = rng.lognormal(12.0, 0.3, T)
    pd.DataFrame({
        "date": [str(d.date()) for d in dates],
        "open": o, "high": h, "low": l, "close": c,
        "volume": v, "oi": oi,
    }).to_parquet(tmp_path / "ts_rb.parquet")
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "rb": {"label": "螺纹钢主力连续", "kind": "ts_single",
               "source": {"type": "parquet",
                          "path": str(tmp_path / "ts_rb.parquet")},
               "layout": "long",
               "mapping": {"date": "date", "open": "open", "high": "high",
                           "low": "low", "close": "close",
                           "volume": "volume", "oi": "oi"},
               "calibration": {"horizon": 20, "cost_bps": 2,
                               "dev_end": "2022-01-01",
                               "sel_end": "2023-01-01",
                               "limit_pct": 0.06}}}}})
    b.dispatch("data.load", {"envId": "rb"})
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "null_landscape.json").write_text(json.dumps({
        "wall_mode": "fixed", "fixed_bar_ic": 0.2, "fixed_bar_tail": 0.05,
        "horizons": [20], "ic_ir": {"20": {"p10": -0.1, "p90": 0.1}},
    }), encoding="utf-8")
    return b


def test_ts_env_load_and_evaluate_diagnosis_shape(tmp_path):
    """ts 环境加载 + 评估诊断同形：键集 ⊆ 消费面（discipline/submit）。"""
    b = _ts_momentum_bridge(tmp_path)
    ld = b.dispatch("data.load", {"envId": "rb"})
    assert ld["ok"] and ld["kind"] == "ts_single" and ld["instrument"] == "rb"
    diag = b.dispatch("factor.evaluate", {"envId": "rb",
                                          "source": TS_MOM_SRC})
    assert "ic_ir_train" in diag and diag.get("ic_n_train", 0) >= 20
    for key in ("ic_mean_train", "ic_ir_train", "ic_n_train",
                "column_perm_train", "deflated_train",
                "verdict", "red_flags", "train_sensitivity", "carving",
                "yearly_consistency_train", "date_shift"):
        assert key in diag or diag.get("error"), key
    assert diag["column_perm_train"]["method"] == "ts_block_perm"
    assert diag.get("topn") is None or isinstance(diag["topn"], dict)
    # 模型面零 OOS：ic_series_train 被 _wrap_diagnosis 摘走
    assert "ic_series_train" not in diag


def test_ts_cs_operator_rejected(tmp_path):
    """cs_* 截面算子在 ts 线被引擎校验拒评估（固定文案）。"""
    b = _ts_momentum_bridge(tmp_path)
    from dsh_factor_mining.bridge import BridgeError
    bad = ("import pandas as pd\n"
           "from dsh_factor_mining.factor.ops import cs_rank\n\n"
           "def factor(env):\n"
           "    return cs_rank(pd.Series(env.c)).values\n")
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.evaluate", {"envId": "rb", "source": bad})
    assert "截面" in str(ei.value)


def test_ts_submit_full_pipeline_binary_verdict(tmp_path):
    """ts 端到端：评估→提交（噪声 ts 版/day-perm/平坦性/墙/M8/M3/正交跳过
    →SPRT）→二值判定，响应零 OOS 数字，ledger 全数字。"""
    b = _ts_momentum_bridge(tmp_path)
    diag = b.dispatch("factor.evaluate", {"envId": "rb",
                                          "source": TS_MOM_SRC})
    sub = b.dispatch("registry.submit", {
        "name": "ts_sprt_e2e", "signal": "rb momentum 60d",
        "source": TS_MOM_SRC, "diagnosis": diag,
        "flatness_params": [{"name": "k", "value": 60, "step": 10}]})
    blob = json.dumps(sub, ensure_ascii=False)
    assert "entry" in sub
    # 尾轨不适用标注（ts 单轨）；无 spread/placebo 门痕迹
    assert sub["entry"]["admit_basis"] == "ic"
    assert "verify" not in sub["entry"] and "tier" not in sub["entry"]
    assert "尾轨" in json.dumps(sub["entry"].get("tracks", {}), ensure_ascii=False)
    if sub["accepted"]:
        assert sub.get("verification", {}).get("verdict") == "pass"
        led = (Path(b.state_root) / "verify_ledger.jsonl").read_text(
            encoding="utf-8").strip().splitlines()
        assert led and "cumulative_t" in led[-1]
    else:
        assert "reason" in sub
    # 二值面无数字（隔离红线：三态文案零数字）
    v = sub.get("verification", {})
    if v:
        assert v.get("verdict") in ("pass", "reject")


def test_run_internal_verification_ts_direct(tmp_path):
    """ts 直调终审：compute_ic_series ts 分派（全面板重算）喂 SPRT；ledger
    记 line=TEST 段线；真发判定消耗族额度。"""
    import hashlib
    from dsh_factor_mining.factor.evaluate_ts import (evaluate_ts,
                                                      ts_product_series)
    b = _ts_momentum_bridge(tmp_path)
    env = b._require_eval_env("rb")
    root = Path(b.state_root)
    # 自写带 dev_end 的段（覆盖 conftest 全覆盖窗）
    spec = {
        "line": "TEST", "panel": {"start": "2018-01-02", "end": "2099-12-31",
                                  "dev_end": "2022-01-01",
                                  "source": "synthetic-test"},
        "segments": [
            {"id": "seg1", "start": "2022-01-02", "end": "2099-12-31",
             "burn_baseline": 0}],
        "upgrade_clause": "synthetic-test"}
    spec["content_sha"] = hashlib.sha256(json.dumps(
        {k: spec[k] for k in ("line", "panel", "segments", "upgrade_clause")},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    (root / "segments.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    (root / "segments.freeze.json").write_text(json.dumps(
        {"frozen_at": "synthetic-test", "content_sha": spec["content_sha"],
         "authorized_by": "synthetic-test",
         "modification_rule": "synthetic-test"}, ensure_ascii=False),
        encoding="utf-8")
    ns = {}
    exec(TS_MOM_SRC, ns)
    F = ns["factor"](env)
    d = evaluate_ts(F, env)
    out = verify.run_internal_verification(
        str(root), TS_MOM_SRC, env, float(d["ic_ir_train"]), None,
        "tsdirect0001", name="ts_direct")
    assert out["verdict"] in ("pass", "reject")
    rec = out["report"]
    assert rec["line"] == "TEST"
    # ts 分派口径：ledger 的段统计 = 全面板乘积序列重算（不信 dev 诊断）
    dates, values = verify.compute_ic_series(TS_MOM_SRC, env, None)
    s = ts_product_series(F, env)
    assert len(dates) == len(s) and math.isclose(
        float(np.mean(values)), float(s.mean()), rel_tol=1e-9)
    fam = verify.family_fingerprint(TS_MOM_SRC)
    assert verify.budget_status(str(root), fam)["used"] == 1
