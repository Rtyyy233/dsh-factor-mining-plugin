# coding=utf-8
"""特质奖励全自动（PLAN-trait-reward-auto，2026-10-06）P1/P2/P5 验收：

- P1 层 M 防塌缩：trail 丢失全部注册条目（模拟 state.reset 删账）后，
  机制层 bar 只升不降、M 不变（台账独有 hash 以无 sketch 合成独立单例回来）；
- P2 判据全维：|max_rho| 绝对值口径（反相关候选必须排除）、尾轨缓议、
  A4 门（null 分位 ≤ 阈值 ∧ 残差 < 0）、族代表、配额、admit_basis/快照/
  mode/trigger 落账、二轮自动为空（已入册退场）；
- 红线 15：enabled=false → maybe_round 零行为；tick 路径 stdout 静默
  （RPC 通道无污染）；开关持久化写 mining_state。

fixture 复用 test_rescue_ops._build_root（合成 root，禁触生产）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_rescue_ops import _build_root, _entry, _sketch, _sr_for_window  # noqa: E402
from dsh_factor_mining.factor.gates import dsr_p_from_stats  # noqa: E402
from dsh_factor_mining.rescue_ops import (  # noqa: E402
    ScanContext, run_auto_round, maybe_round, rescue_auto_config,
    _set_rescue_auto_flag, _auto_candidates)

ANTI = "d0d0aa11aaaa"      # 与在册 acce5705 反相关（|ρ|≈1）——绝对值口径必须排除
TAILC = "deadbb22bbbb"     # 尾轨签名候选（registry 有 admit_basis=tail 条目）——缓议
GOOD2 = "c0cafe33cccc"     # 第二个干净漂移候选（与 GOOD 同族外另一族）


def _extend(root: Path) -> None:
    """追加反相关/尾轨/次干净候选 + evidence_fit v1（残差门可用）。"""
    trail = json.loads((root / "trail_engine.json").read_text(encoding="utf-8"))
    ctx = ScanContext(root)
    sr = _sr_for_window(ctx.bar_main, ctx.bar_mech, ctx.pool_std)
    acce_sk = next(e["ic_series_sketch"] for e in trail
                   if e.get("source_hash") == "acce5705eeee")
    rng = np.random.default_rng(7)
    new = [
        (ANTI, [-x for x in acce_sk], sr),
        # 尾轨候选：独立草图（同草图会先被正交绝对值口径拦——正交优先于
        # 缓议分支，那是正确次序，不是本测试的对象）
        (TAILC, _sketch(rng, drift=0.26, std=0.11), sr),
        (GOOD2, _sketch(rng, drift=0.28, std=0.10), sr),  # 独立漂移草图=A4 深
    ]
    for h, sk, s in new:
        trail.append(_entry(h, "2026-08-12T09:00:00", sk, s))
    (root / "trail_engine.json").write_text(
        json.dumps(trail, ensure_ascii=False), encoding="utf-8")
    with open(root / "registered_ledger.jsonl", "a", encoding="utf-8") as f:
        for h, _s, _x in new:
            f.write(json.dumps({"ts_registered": "2026-08-11T00:00:00",
                                "ts_eval": "2026-08-12T09:00:00",
                                "source_hash": h, "journal_id": f"J-{h[:4]}",
                                "ops": 5, "depth": 3,
                                "journal_lane": "test-lane"},
                               ensure_ascii=False) + "\n")
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    reg.append({"name": "tail_prior", "source_hash": TAILC, "accepted": False,
                "admit_basis": "tail",
                "tracks": {"ic": {"accepted": False, "reason": "-"},
                           "tail": {"accepted": False, "reason": "rej"}}})
    (root / "registry.json").write_text(json.dumps(reg, ensure_ascii=False),
                                        encoding="utf-8")
    for h in (ANTI, TAILC, GOOD2):
        p = root / "sources" / h[:2] / f"{h}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"def factor(env):\n    # {h}\n    return env.c\n",
                     encoding="utf-8")
    # 拟合 v1：pred≈2.0 常数 → 干净漂移候选（a4≈1e-3）残差≈-2<0 过门；
    # 噪声候选（a4 大）残差仍<0 但 null 分位超阈被分位门拦
    (root / "evidence_fit.json").write_text(json.dumps({
        "v1": {"coef": [2.0, 0.0, 0.0], "fit_set": "test",
               "n_fit": 100, "formula": "a4 ~ 1 + sr_abs + sr_abs^2 (test)"}},
        ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# P1：层 M 防塌缩
# ---------------------------------------------------------------------------
def test_mechanism_trials_reset_proof(tmp_path):
    root = _build_root(tmp_path)
    ctx0 = ScanContext(root)
    bar_full, m_full = ctx0.bar_mech, int(ctx0.stats_mech["n_trials"])
    # 模拟 state.reset 删掉全部注册条目（主群保留）
    reg_hashes = set(ctx0.registered)
    trail = json.loads((root / "trail_engine.json").read_text(encoding="utf-8"))
    trail = [e for e in trail if e.get("source_hash") not in reg_hashes]
    (root / "trail_engine.json").write_text(
        json.dumps(trail, ensure_ascii=False), encoding="utf-8")
    ctx1 = ScanContext(root)
    bar_reset, m_reset = ctx1.bar_mech, int(ctx1.stats_mech["n_trials"])
    assert m_reset == m_full, "台账行数计 M：reset 后注册试验必须以合成单例回来"
    assert bar_reset >= bar_full - 1e-9, (
        f"reset 后层 bar 只升不降（独立单例=最贵假设）：{bar_full:.3f} → {bar_reset:.3f}")


# ---------------------------------------------------------------------------
# P2：判据全维
# ---------------------------------------------------------------------------
def test_auto_selection_dimensions(tmp_path):
    root = _build_root(tmp_path)
    _extend(root)
    ctx = ScanContext(root)
    cfg = dict(rescue_auto_config({}))
    selected, deferred, n_after = _auto_candidates(ctx, cfg)
    sel_hashes = [r["hash"] for r in selected]
    assert n_after >= 1
    assert ANTI not in sel_hashes, "反相关 |ρ|≈1 必须被绝对值口径排除"
    assert TAILC in deferred and TAILC not in sel_hashes, "尾轨候选必须缓议"
    for r in selected:
        assert abs(float(r["max_rho"])) < 0.80
        assert r["calibrated_for"] == "ic_track"
        assert r["a4_dd_resid"] < 0
        assert r["a4_null_pct"] <= cfg["a4_null_pct_max"]
    # 配额
    cfg2 = dict(cfg, quota_per_round=1)
    sel1, _d, _n = _auto_candidates(ctx, cfg2)
    assert len(sel1) == 1
    # require_a4=False 时反相关仍被正交拦（A4 门不是正交的替代）
    cfg3 = dict(cfg, require_a4=False)
    sel3, _d3, _n3 = _auto_candidates(ctx, cfg3)
    assert ANTI not in [r["hash"] for r in sel3]


def test_auto_round_admits_and_ledger(tmp_path):
    root = _build_root(tmp_path)
    _extend(root)
    out = run_auto_round(str(root), trigger="manual", dry_run=False)
    assert out["admitted"], "至少放行一只"
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    auto_entries = [e for e in reg if e.get("admit_basis") == "auto_rescue_layer_v1"]
    assert len(auto_entries) == len(out["admitted"])
    e0 = auto_entries[0]
    assert e0["rescue_snapshot"].get("criteria") == "auto_v1"
    assert e0["rescue_snapshot"].get("a4_null_pct") is not None
    assert e0["tracks"]["ic"]["accepted"] is True
    rows = [json.loads(x) for x in
            (root / "rescue_scans.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    last = rows[-1]
    assert last["mode"] == "auto" and last["trigger"] == "manual"
    assert last["basis"] == "auto_rescue_layer_v1"
    assert TAILC in (last.get("deferred_human") or [])
    # 二轮：全部已入册 → 空选
    out2 = run_auto_round(str(root), trigger="tick", dry_run=False, quiet=True)
    assert out2["admitted"] == []
    rows2 = [json.loads(x) for x in
             (root / "rescue_scans.jsonl").read_text(encoding="utf-8").splitlines()
             if x.strip()]
    assert rows2[-1]["round"] > last["round"], "轮次号只增"


# ---------------------------------------------------------------------------
# 红线 15：开关/节流/静默
# ---------------------------------------------------------------------------
class _FakeBridge:
    def __init__(self, root):
        self.state_root = str(root)


def test_maybe_round_disabled_and_throttle(tmp_path, capsys):
    root = _build_root(tmp_path)
    _extend(root)
    assert maybe_round(_FakeBridge(root)) is None, "enabled=false 零行为"
    _set_rescue_auto_flag(root, True)
    assert rescue_auto_config(
        json.loads((root / "mining_state.json").read_text(encoding="utf-8"))
    )["enabled"] is True, "开关持久化"
    # 无台账 → 立即跑一轮；且 stdout 必须静默（RPC 通道无污染）
    out = maybe_round(_FakeBridge(root))
    assert out is not None and out["admitted"]
    assert capsys.readouterr().out == "", "tick 路径 stdout 必须重定向 stderr"
    # 末轮 ts 刚写 → 节流期内不再触发
    assert maybe_round(_FakeBridge(root)) is None
    # 把末轮 ts 改老 → 再触发
    rows = [json.loads(x) for x in
            (root / "rescue_scans.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()]
    rows[-1]["ts"] = "2026-01-01T00:00:00"
    (root / "rescue_scans.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    out3 = maybe_round(_FakeBridge(root))
    assert out3 is not None and out3["admitted"] == [], "存量已清，二轮空放行"
    # 急停
    _set_rescue_auto_flag(root, False)
    assert maybe_round(_FakeBridge(root)) is None


def test_auto_criteria_prices_match_engine(tmp_path):
    """auto 放行条目的快照价格与引擎公式直算一致（红线 10 同源）。"""
    root = _build_root(tmp_path)
    _extend(root)
    out = run_auto_round(str(root), trigger="manual", dry_run=False)
    ctx = ScanContext(root)
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    e0 = next(e for e in reg if e.get("admit_basis") == "auto_rescue_layer_v1")
    h = e0["source_hash"]
    d = next(en for en in ctx.trail if en.get("source_hash") == h)["dsr_stats"]
    p = dsr_p_from_stats(d["sr_hat"], d["skew"], d["kurt"], d["n_obs"],
                         ctx.bar_mech, ctx.pool_std)
    assert e0["rescue_snapshot"]["p_mech"] == pytest.approx(p, rel=1e-9)
    assert e0["rescue_snapshot"]["bar_mech"] == pytest.approx(ctx.bar_mech, abs=1e-4)
