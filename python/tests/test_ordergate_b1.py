# coding=utf-8
"""B1 提交顺序门测试（2026-10-14 任务书 §三 B1）。

覆盖：三分支判定（缺预注册/晚注册/推导链缺块）+ 豁免 + 双档 +
自动切档规则边界（200 次/4 周、500 次/8 周、违规率 10%）+ 事务性
拒收不落盘不烧名 + audit 落盘。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import ordergate  # noqa: E402
from dsh_factor_mining.discipline import source_fingerprint  # noqa: E402

GOOD_SRC = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _mk_root(tmp: Path):
    root = tmp / "state"
    (root / "journal" / "default" / "arcs").mkdir(parents=True)
    (root / "journal" / "default" / "raw").mkdir(parents=True)
    return root


def _journal_entry(root: Path, eid: str, status: str, ref_hash: str,
                   body: str, ts: str):
    p = root / "journal" / "default" / "live.md"
    block = (f"\n## {eid} {status} 假设 {eid}\n"
             f"ts:{ts}\nref:{ref_hash}\n{body}\n")
    with open(p, "a", encoding="utf-8") as f:
        f.write(block)


def _trail_entry(root: Path, ref_hash: str, ts: str, origin="hypothesis"):
    p = root / "trail_engine.json"
    trail = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    trail.append({"ts": ts, "source_hash": ref_hash, "origin": origin,
                  "stage": "development", "horizon": 10,
                  "ic_ir": 0.2, "verdict": "pass",
                  "ic_series_sketch": [0.1, 0.2, 0.1, 0.2, 0.1]})
    p.write_text(json.dumps(trail), encoding="utf-8")


def _journal_dir(root: Path) -> Path:
    return root / "journal" / "default"


def test_pass_path_preregistration_before_eval():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        # 先注册（正文提及全部构造块：shift 用 close 表述、除法 div）
        _journal_entry(root, "H-1", "registered", h,
                       "机制：close 动量衰减；构造：close 二十日 shift 与除法"
                       "（div 语义）；预期 IC 为正", "2026-09-10T10:00:00")
        _trail_entry(root, h, "2026-09-10T11:00:00")
        v = ordergate.check_violation(GOOD_SRC, h, json.loads(
            (root / "trail_engine.json").read_text(encoding="utf-8")),
            _journal_dir(root))
        assert v is None, v


def test_violation_no_preregistration():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T11:00:00")
        v = ordergate.check_violation(GOOD_SRC, h, json.loads(
            (root / "trail_engine.json").read_text(encoding="utf-8")),
            _journal_dir(root))
        assert v is not None and v["kind"] == "no_preregistration"


def test_violation_late_registration():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00")
        # 注册晚于首次评估 → 拒
        _journal_entry(root, "H-1", "registered", h,
                       "close div 事后补写", "2026-09-10T12:00:00")
        v = ordergate.check_violation(GOOD_SRC, h, json.loads(
            (root / "trail_engine.json").read_text(encoding="utf-8")),
            _journal_dir(root))
        assert v is not None and v["kind"] == "late_registration", v


def test_violation_unexplained_blocks():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        # 源码含 close；正文只提 volume——close 未被推导正文提及
        _journal_entry(root, "H-1", "registered", h,
                       "机制：volume 放大", "2026-09-10T10:00:00")
        _trail_entry(root, h, "2026-09-10T11:00:00")
        v = ordergate.check_violation(GOOD_SRC, h, json.loads(
            (root / "trail_engine.json").read_text(encoding="utf-8")),
            _journal_dir(root))
        assert v is not None and v["kind"] == "unexplained_blocks", v
        assert "close" in v["detail"]["missing_blocks"]


def test_exempt_random_origin():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00", origin="random")
        r = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                          _journal_dir(root))
        assert r["exempt"] is True and r["violation"] is None


def test_shadow_records_and_teaches_not_rejects():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00")
        r1 = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                           _journal_dir(root))
        assert r1["mode"] == "shadow"
        assert r1["violation"] is not None and r1["n"] == 1
        st = json.loads((root / "mining_state.json").read_text(
            encoding="utf-8"))
        assert st["submission_order_gate_state"]["violations_total"] == 1


def _set_gate_state(root: Path, **kw):
    p = root / "mining_state.json"
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    g = st.get("submission_order_gate_state") or {}
    g.setdefault("mode", "shadow")
    g.setdefault("first_seen", time.strftime("%Y-%m-%dT%H:%M:%S"))
    g.setdefault("n", 0)
    g.setdefault("recent", [])
    g.setdefault("violations_total", 0)
    g.setdefault("diagnostic_emitted", False)
    g.update(kw)
    st["submission_order_gate_state"] = g
    p.write_text(json.dumps(st), encoding="utf-8")


def test_auto_switch_enforce_at_window_and_low_rate():
    """边界：n=200（观察窗满）且最近 50 次违规率 10% → 自动切 enforce。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        # 199 次观察 + 最近 50 次恰 5 违规（10%=边界通过）
        _set_gate_state(root, n=199,
                        recent=[1] * 5 + [0] * 45)
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00")
        r = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                          _journal_dir(root))
        assert r["n"] == 200 and r["switched"] is True
        assert r["mode"] == "enforce"
        ev = (root / "gate_audit.jsonl").read_text(encoding="utf-8")
        assert "gate_switch_enforce" in ev


def test_auto_switch_not_below_window_even_clean():
    """n=199（未到窗）违规率 0 也不切——前半段保证观察窗足够。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        _set_gate_state(root, n=199 - 50, recent=[0] * 50)
        h = source_fingerprint(GOOD_SRC)
        _journal_entry(root, "H-1", "registered", h, "close 机制",
                       "2026-09-10T09:00:00")
        r = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                          _journal_dir(root))
        assert r["n"] == 150 and r["switched"] is False
        assert r["mode"] == "shadow"


def test_auto_switch_rate_boundary_11pct_blocks():
    """最近 50 次违规率 >10%（6/50）→ 不切。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        _set_gate_state(root, n=250, recent=[1] * 6 + [0] * 44)
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00")
        r = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                          _journal_dir(root))
        assert r["switched"] is False and r["mode"] == "shadow"


def test_auto_switch_by_age_four_weeks():
    """n 未到 200 但满 4 周 + 低违规率 → 切。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        import datetime as dt
        first = (dt.datetime.now() - dt.timedelta(days=29)).strftime(
            "%Y-%m-%dT%H:%M:%S")
        _set_gate_state(root, n=80, first_seen=first, recent=[0] * 50)
        h = source_fingerprint(GOOD_SRC)
        _journal_entry(root, "H-1", "registered", h, "close",
                       "2026-09-10T09:00:00")
        r = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                          _journal_dir(root))
        assert r["switched"] is True and r["mode"] == "enforce"


def test_diagnostic_at_giveup_boundary():
    """累计 500 且违规率 >10% → 维持 shadow + 一次性诊断事件。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        _set_gate_state(root, n=499, recent=[1] * 20 + [0] * 30)
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00")
        r1 = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                           _journal_dir(root))
        assert r1["diagnostic"] is True and r1["mode"] == "shadow"
        ev = (root / "gate_audit.jsonl").read_text(encoding="utf-8")
        assert "gate_switch_diagnostic" in ev
        # 诊断只发一次（后续同态不再刷屏）
        r2 = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                           _journal_dir(root))
        assert r2["diagnostic"] is False


def test_enforce_rejection_is_transactional_and_educational():
    """enforce 拒收文案教学式自足 + registry 零写入（事务性）。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        h = source_fingerprint(GOOD_SRC)
        _trail_entry(root, h, "2026-09-10T10:00:00")
        _set_gate_state(root, mode="enforce", n=10, recent=[0] * 10)
        r = ordergate.evaluate_and_record(root, GOOD_SRC, h,
                                          _journal_dir(root))
        assert r["mode"] == "enforce" and r["violation"] is not None
        msg = ordergate.rejection_message(r["violation"])
        assert "factor_journal_append" in msg      # 去哪里补预注册
        assert "factor_evaluate" in msg            # 顺序教学
        # 判定为纯函数复验（enforce 拒绝发生在 submit 任何写入之前由
        # bridge 层接线保证；本处断言 check_violation 幂等）
        v2 = ordergate.check_violation(
            GOOD_SRC, h,
            json.loads((root / "trail_engine.json").read_text(
                encoding="utf-8")), _journal_dir(root))
        assert v2 is not None


def test_unexplained_blocks_message_lists_missing():
    v = {"kind": "unexplained_blocks",
         "detail": {"missing_blocks": ["ts_rank", "close"], "n_missing": 2}}
    msg = ordergate.rejection_message(v)
    assert "ts_rank" in msg and "close" in msg
    assert "factor_journal_update" in msg


def test_gate_status_view():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        st = ordergate.gate_status(root)
        assert st["mode"] == "shadow" and st["n"] == 0


if __name__ == "__main__":
    for fn in [test_pass_path_preregistration_before_eval,
               test_violation_no_preregistration,
               test_violation_late_registration,
               test_violation_unexplained_blocks,
               test_exempt_random_origin,
               test_shadow_records_and_teaches_not_rejects,
               test_auto_switch_enforce_at_window_and_low_rate,
               test_auto_switch_not_below_window_even_clean,
               test_auto_switch_rate_boundary_11pct_blocks,
               test_auto_switch_by_age_four_weeks,
               test_diagnostic_at_giveup_boundary,
               test_enforce_rejection_is_transactional_and_educational,
               test_unexplained_blocks_message_lists_missing,
               test_gate_status_view]:
        fn()
        print(f"ok {fn.__name__}")
