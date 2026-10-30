# coding=utf-8
"""E5/E6 验收（2026-10-06 假阴性救助线任务 B）：scan 过滤链与 admit 台账。

合成 stateRoot（全部临时目录，禁触生产 root——§〇 红线）：
- 主群 80 个互不相关试验 → bar_main 高墙；注册群 6 个 → bar_mech 低墙；
- 候选 sr 落在 p_main>0.05 ∧ p_mech≤0.0253 的假阴性窗口（窗口由引擎
  重放算出的 bar 反解，fixture 不硬编码墙值）。

断言面：B.1 过滤链（时序/红旗/已入册/p 两档）、卡片双格式、重跑逐位
一致、admit 四重拒绝+红线 5/6、快照与台账字段、轮次号不重复。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.factor.gates import dsr_p_from_stats  # noqa: E402
from dsh_factor_mining.rescue_ops import (  # noqa: E402
    ALPHA_LAYER, cmd_admit, cmd_scan)


def _sketch(rng, n=160, drift=0.0, std=0.3):
    return [round(float(x), 4) + 0.0
            for x in (drift + rng.normal(0, std, n))]


def _entry(h, ts, sketch, sr, origin="hypothesis", red_flags=None,
           horizon=10, verdict="needs_review"):
    return {"source_hash": h, "stage": "development", "horizon": horizon,
            "origin": origin, "ts": ts, "ic_series_sketch": sketch,
            "ic_ir": sr, "verdict": verdict,
            "red_flags": red_flags or [],
            "dsr_stats": {"sr_hat": sr, "skew": 0.0, "kurt": 3.0,
                          "n_obs": 100}}


def _sr_for_window(bar_main, bar_mech, pool):
    """窗口反解：p_mech≤0.0253 ∧ p_main>0.05 的 sr（对固定 g3=0,g4=3,n=100）。"""
    def p(sr, bar):
        return dsr_p_from_stats(sr, 0.0, 3.0, 100, bar, pool)
    lo = 0.2
    while p(lo, bar_mech) is None or p(lo, bar_mech) > ALPHA_LAYER:
        lo += 0.05
    hi = lo
    while p(hi, bar_main) is None or p(hi, bar_main) > 0.05:
        hi += 0.02
    assert lo < hi, f"fixture 墙距不足（bar_main={bar_main} bar_mech={bar_mech}）"
    return round((lo + hi) / 2, 4)


def _build_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    rng = np.random.default_rng(2026)
    # null 地形先行（pool_std≈(p90−p10)/2.5631；含 a4_dd/famsize 段供卡片消费）
    (root / "null_landscape.json").write_text(json.dumps({
        "n_generated": 200, "horizons": [10], "seed": 42,
        "ic_ir": {"10": {"p10": -0.4, "p90": 0.4}},
        "a4_dd": {"10": {"p10": 0.05, "p50": 0.20, "p90": 0.60, "p95": 0.80,
                         "n": 180}},
        "famsize": {"10": {"p10": 1.0, "p50": 1.0, "p90": 2.0, "p95": 3.0,
                           "n_families": 170}},
    }, ensure_ascii=False), encoding="utf-8")
    trail = []
    # 主群 80 个互不相关试验（抬 bar_main）
    for i in range(80):
        trail.append(_entry(f"aa{i:04x}0000", f"2026-08-01T00:{i % 60:02d}:00",
                            _sketch(rng), 0.1 + 0.001 * i))
    # 注册群草图先定（墙只依赖草图，与 sr 无关）——占位 sr 落盘拿墙，
    # 反解窗口后只改 dsr 数字重写：两次落盘的 bar 完全一致。
    sk_cand = _sketch(rng, drift=0.30, std=0.10)
    cand_specs = [
        ("cafe0001aaaa", "2026-08-10T01:00:00", sk_cand, None, []),
        ("ba0e0202bbbb", "2026-08-10T02:00:00", list(sk_cand), None, []),
        ("710e03cccccc", "2026-08-10T03:00:00", _sketch(rng), None, []),
        # 硬拒旗（样本不足）——红线 6 剔除
        ("edd0f10ddddd", "2026-08-10T04:00:00", _sketch(rng), None,
         ["IC_IR=0.88 但 n=12 样本不足，警惕小样本巧合"]),
        # 折减族旗（deflated p 不显著）——救助对象本身的挂线标记，不剔除
        ("0dd1a10fffff", "2026-08-10T10:00:00", _sketch(rng), None,
         ["deflated p=0.9800 > 0.05：63 次已试假设的多重检验校正下不显著"]),
        ("acce5705eeee", "2026-08-10T05:00:00", _sketch(rng), None, []),
        ("a1b51906ffff", "2026-08-10T06:00:00", _sketch(rng), None, []),
        ("3ec4a107bbbbb", "2026-08-10T07:00:00", _sketch(rng), None, []),
        ("f111e108aaaa", "2026-08-10T08:00:00", _sketch(rng), 0.15, []),
        ("f111e109bbbb", "2026-08-10T09:00:00", _sketch(rng), -0.15, []),
    ]

    def _flush(trail_):
        (root / "trail_engine.json").write_text(
            json.dumps(trail_, ensure_ascii=False), encoding="utf-8")

    _flush(trail + [_entry(h, ts, s, 0.5, red_flags=rf)
                    for h, ts, s, _sr, rf in cand_specs])
    # 注册台账先行（首个 ScanContext 要按它算 bar_mech）：
    # 好候选 registered_ts < 首评 ts；TIMING 反之（事后补票）
    rows = []
    for h, _ts, *_ in cand_specs:
        reg_ts = "2026-08-11T00:00:00" if h == "710e03cccccc" else "2026-08-09T00:00:00"
        rows.append({"ts_registered": reg_ts, "ts_eval": "2026-08-10T12:00:00",
                     "source_hash": h, "journal_id": f"J-{h[:4]}",
                     "ops": 5, "depth": 3, "journal_lane": "test-lane"})
    (root / "registered_ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    # registry：ACCEPTED 已入册（IC 轨）——timeline 候选的正交靶也用它
    (root / "registry.json").write_text(json.dumps([
        {"name": "acce5705eeee_factor", "source_hash": "acce5705eeee",
         "accepted": True, "admit_basis": "ic",
         "tracks": {"ic": {"accepted": True, "reason": "test"},
                    "tail": {"accepted": False, "reason": "-"}}},
    ], ensure_ascii=False), encoding="utf-8")

    from dsh_factor_mining.rescue_ops import ScanContext
    ctx = ScanContext(root)
    assert ctx.pool_std is not None, "landscape fixture 缺失"
    assert int(ctx.stats_mech["n_trials"]) == len(cand_specs), \
        "机制层 M 必须已含全部注册候选（窗口反解的前提）"
    sr_win = _sr_for_window(ctx.bar_main, ctx.bar_mech, ctx.pool_std)
    mult = {"cafe0001aaaa": 1.0, "ba0e0202bbbb": 1.0, "710e03cccccc": 1.0,
            "edd0f10ddddd": 1.0, "0dd1a10fffff": 1.0, "acce5705eeee": 1.0,
            "a1b51906ffff": 2.2, "3ec4a107bbbbb": 0.6,
            "f111e108aaaa": 0.0, "f111e109bbbb": 0.0}
    trail += [_entry(h, ts, s, round(sr_win * mult[h], 4), red_flags=rf)
              for h, ts, s, _sr, rf in cand_specs]
    _flush(trail)

    # journal 预期（live.md，ref 键关联）
    (root / "journal" / "test-lane").mkdir(parents=True)
    (root / "journal" / "test-lane" / "live.md").write_text(
        "## J-cand registered 横截面动量\n"
        "ref:cafe0001aaaa\nts:2026-08-09T00:00:00\n"
        "预期：IC_IR>0.3 且平稳\n\n"
        "## J-red registered 预期：xxx\n"
        "ref:edd0f10ddddd\n",
        encoding="utf-8")

    # sources 外挂库
    for h in ("cafe0001aaaa", "ba0e0202bbbb", "710e03cccccc",
              "edd0f10ddddd", "0dd1a10fffff", "a1b51906ffff",
              "3ec4a107bbbbb"):
        p = root / "sources" / h[:2] / f"{h}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"def factor(env):\n    # {h}\n    return env.c\n",
                     encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# E5：scan
# ---------------------------------------------------------------------------
def test_scan_filters_and_card(tmp_path, capsys):
    root = _build_root(tmp_path)
    out = cmd_scan(str(root))
    capsys.readouterr()
    st = out["statuses"]
    # CAND/TWINSKY + 折减族旗候选（deflated p 不显著=挂线标记，非硬拒）
    assert st.get("ok", 0) >= 3, "CAND/TWINSKY/折减旗候选必须在卡"
    assert st.get("timing", 0) == 1
    assert st.get("red_flags", 0) == 1, "样本不足等硬拒旗必须剔除"
    assert st.get("already_accepted", 0) == 1
    assert st.get("main_significant", 0) == 1
    assert out["pool_std"] == pytest.approx(0.8 / 2.5631, rel=1e-9)


def test_scan_determinism(tmp_path, capsys):
    root = _build_root(tmp_path)
    cmd_scan(str(root))
    a = capsys.readouterr().out
    cmd_scan(str(root))
    b = capsys.readouterr().out
    assert a == b, "重跑逐位一致（卡片内容不含墙钟）"


def test_scan_card_columns_and_out(tmp_path, capsys):
    root = _build_root(tmp_path)
    outdir = tmp_path / "cards"
    cmd_scan(str(root), out_dir=str(outdir))
    stdout = capsys.readouterr().out
    files = sorted(outdir.glob("rescue_card_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]
    hashes = {r["hash"] for r in rows}
    assert "cafe0001aaaa" in hashes and "ba0e0202bbbb" in hashes
    assert "710e03cccccc" not in hashes and "edd0f10ddddd" not in hashes
    assert "acce5705eeee" not in hashes, "已入册不进卡"
    assert "a1b51906ffff" not in hashes, "主价已过门（非假阴性）不进卡"
    for r in rows:
        # E7 四证据列：A4 残差+null 分位 / famsize+caliber / max_rho / fit_v
        for k in ("a4_dd_resid", "a4_null_pct", "famsize", "famsize_caliber",
                  "max_rho", "family_rep", "family_n", "registered_ts",
                  "first_eval_ts", "journal_excerpt", "first_red_flags"):
            assert k in r, f"卡片缺列 {k}"
        assert r["calibrated_for"] == "ic_track"
    cand = next(r for r in rows if r["hash"] == "cafe0001aaaa")
    assert cand["p_main"] > 0.05 and cand["p_mech"] <= ALPHA_LAYER
    assert cand["journal_excerpt"] and "IC_IR" in cand["journal_excerpt"]
    # 同族归并：CAND 与 TWINSKY 同 sketch → 族代表=族内 p_mech 最小者
    twins = next(r for r in rows if r["hash"] == "ba0e0202bbbb")
    assert cand["family_rep"] == twins["family_rep"] and cand["family_n"] == 2


# ---------------------------------------------------------------------------
# E6：admit
# ---------------------------------------------------------------------------
def test_admit_dry_run_default_no_write(tmp_path, capsys):
    root = _build_root(tmp_path)
    cmd_admit(str(root), ["cafe0001aaaa"])
    capsys.readouterr()
    assert not (root / "rescue_scans.jsonl").exists(), "缺省 dry-run 不落台账"
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert len(reg) == 1, "缺省 dry-run 不写 registry"


def test_admit_success_fields_and_ledger(tmp_path, capsys):
    root = _build_root(tmp_path)
    res = cmd_admit(str(root), ["cafe0001aaaa"], yes=True)
    capsys.readouterr()
    assert res["round"] == 1 and len(res["admitted"]) == 1
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    e = next(x for x in reg if x.get("admit_basis") == "human_rescue_layer")
    assert e["accepted"] is True and e["rescue_round"] == 1
    assert e["tracks"]["ic"]["accepted"] is True
    assert e["tracks"]["ic"]["reason"] == "human_rescue_layer round 1"
    assert e["source_hash"] == "cafe0001aaaa"
    assert "def factor" in e["source"]
    snap = e["rescue_snapshot"]
    for k in ("p_main", "p_mech", "bar_main", "bar_mech", "pool_std",
              "alpha_layer", "round_id", "scan_ts"):
        assert k in snap, f"快照缺字段 {k}"
    assert snap["alpha_layer"] == ALPHA_LAYER
    rows = [json.loads(x) for x in
            (root / "rescue_scans.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["round"] == 1 and rows[0]["quota"] == 10
    assert rows[0]["operator"] and rows[0]["admitted"] and rows[0]["candidates"]
    # 已放行 → 再扫不在卡（already_accepted）
    out = cmd_scan(str(root))
    capsys.readouterr()
    assert out["statuses"].get("already_accepted", 0) == 2


def test_admit_four_rejections(tmp_path, capsys):
    root = _build_root(tmp_path)
    res = cmd_admit(str(root), ["710e03cccccc", "edd0f10ddddd",
                                "acce5705eeee", "0000deadbeef"],
                    yes=True)
    capsys.readouterr()
    assert not res["admitted"]
    reasons = {r["hash"]: r["reason"] for r in res["rejected"]}
    assert "时序非法" in reasons["710e03cccccc"]
    assert "red_flags" in reasons["edd0f10ddddd"]
    assert "已入册" in reasons["acce5705eeee"]
    assert "非注册候选" in reasons["0000deadbeef"]
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert len(reg) == 1, "全拒——registry 不增"
    rows = [json.loads(x) for x in
            (root / "rescue_scans.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["round"] == 1 and not rows[0]["admitted"]
    # 轮次号不重复：同命令重跑 → round 2（全"已入册"照记不重复）
    res2 = cmd_admit(str(root), ["710e03cccccc", "acce5705eeee"], yes=True)
    assert res2["round"] == 2


def test_admit_quota_hard_cap(tmp_path, capsys):
    root = _build_root(tmp_path)
    # 造 12 个互不相关 sketch 的窗口候选（同 sr 窗口）
    from dsh_factor_mining.rescue_ops import ScanContext
    rng = np.random.default_rng(99)
    ctx = ScanContext(root)
    sr_win = _sr_for_window(ctx.bar_main, ctx.bar_mech, ctx.pool_std)
    trail = json.loads((root / "trail_engine.json").read_text(encoding="utf-8"))
    rows = []
    for i in range(12):
        h = f"9a{i:04x}0aaaaa"
        trail.append(_entry(h, f"2026-08-20T0{i % 10}:00:00",
                            _sketch(rng, std=0.35), sr_win))
        rows.append({"ts_registered": "2026-08-19T00:00:00",
                     "ts_eval": "2026-08-20T12:00:00", "source_hash": h,
                     "journal_id": f"Q-{i}", "ops": 5, "depth": 3,
                     "journal_lane": "test-lane"})
        p = root / "sources" / h[:2] / f"{h}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def factor(env):\n    return env.c\n", encoding="utf-8")
    (root / "trail_engine.json").write_text(
        json.dumps(trail, ensure_ascii=False), encoding="utf-8")
    with open(root / "registered_ledger.jsonl", "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    hashes = [r["source_hash"] for r in rows]
    res = cmd_admit(str(root), hashes, yes=True)
    capsys.readouterr()
    assert len(res["admitted"]) == 10, "红线 5：每轮每账本 ≤10"
    quota_rejects = [r for r in res["rejected"] if "超配额" in r["reason"]]
    assert len(quota_rejects) == 2
    reg = json.loads((root / "registry.json").read_text(encoding="utf-8"))
    assert sum(1 for e in reg if e.get("admit_basis") == "human_rescue_layer") == 10


def test_admit_orthogonality_hard_reject(tmp_path, capsys):
    root = _build_root(tmp_path)
    # 与在册 acce5705eeee 的 sketch 逐位相同（|ρ|=1）→ 正交硬拒
    trail = json.loads((root / "trail_engine.json").read_text(encoding="utf-8"))
    acc_sk = next(e["ic_series_sketch"] for e in trail
                  if e["source_hash"] == "acce5705eeee")
    from dsh_factor_mining.rescue_ops import ScanContext
    ctx = ScanContext(root)
    sr_win = _sr_for_window(ctx.bar_main, ctx.bar_mech, ctx.pool_std)
    trail.append(_entry("027900aaaaaaa", "2026-08-25T01:00:00", list(acc_sk),
                        sr_win))
    (root / "trail_engine.json").write_text(
        json.dumps(trail, ensure_ascii=False), encoding="utf-8")
    with open(root / "registered_ledger.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts_registered": "2026-08-24T00:00:00",
                            "ts_eval": "2026-08-25T02:00:00",
                            "source_hash": "027900aaaaaaa", "journal_id": "O-1",
                            "ops": 5, "depth": 3, "journal_lane": "test-lane"},
                           ensure_ascii=False) + "\n")
    p = root / "sources" / "or" / "027900aaaaaaa.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def factor(env):\n    return env.c\n", encoding="utf-8")
    res = cmd_admit(str(root), ["027900aaaaaaa"], yes=True)
    capsys.readouterr()
    assert not res["admitted"]
    assert any("正交" in r["reason"] and "0.80" in r["reason"]
               for r in res["rejected"]), res["rejected"]
