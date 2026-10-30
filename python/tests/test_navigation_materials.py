# coding=utf-8
"""C1/C2/C3/C4 导航层 + B2 接线 + D1 判据测试（2026-10-14 任务书）。

红线断言：素材纯名词、零分数（无小数）、预算截断、无原始 σ/墙值。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import navigation as nav  # noqa: E402

_DECIMAL_RE = re.compile(r"\d+\.\d+")

SRC_TMPL = """
def factor(env):
    import pandas as pd
    from dsh_factor_mining.factor.ops import {ops}
    return ({body}).values
"""


def _mk_root(tmp: Path):
    root = tmp / "state"
    root.mkdir(parents=True)
    return root


def _store(root: Path, h: str, body: str, ops: str):
    p = root / "sources" / h[:2] / f"{h}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC_TMPL.format(ops=ops, body=body), encoding="utf-8")


def _trail(root: Path, entries: list[dict]):
    (root / "trail_engine.json").write_text(json.dumps(entries),
                                            encoding="utf-8")


def _entry(h: str, ts: str, sketch=None, origin="hypothesis", stage="batch"):
    return {"ts": ts, "source_hash": h, "origin": origin, "stage": stage,
            "horizon": 10, "ic_ir": 0.3, "verdict": "pass",
            "ic_series_sketch": sketch if sketch is not None
            else [0.1, -0.1, 0.1, -0.1, 0.1, -0.1]}


# ---- C1 覆盖地图 ----

def test_coverage_bins_density_and_recency():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        _store(root, "a" * 32, "ts_mean(env.c, 5)", "ts_mean")
        _store(root, "b" * 32, "cs_rank(env.v)", "cs_rank")
        _store(root, "c" * 32, "cs_rank(env.v)", "cs_rank")
        _trail(root, [_entry("a" * 32, "2026-09-01T00:00:00"),
                      _entry("b" * 32, "2026-09-02T00:00:00"),
                      _entry("c" * 32, "2026-09-03T00:00:00")])
        bidx = nav.load_block_index(root)
        bins = nav.coverage_bins(bidx, nav.read_engine_trail(root))
        by_key = {(b["fields"], b["families"]): b for b in bins}
        assert by_key[("close", "ts")]["density"] == 1
        assert by_key[("volume", "cs")]["density"] == 2
        assert by_key[("volume", "cs")]["last_ts"] == "2026-09-03T00:00:00"


# ---- C2 三档 ----

def _sketch(seed: int, n: int = 64) -> list:
    import random
    r = random.Random(seed)
    return [r.uniform(-1, 1) for _ in range(n)]


def test_exhaustion_tiers_boundaries():
    # 高箱内冗余（相同 64 点 sketch → |ρ|=1，达 _MIN_ALIGN=60 下限之上）
    same_sk = _sketch(1)
    same = [_entry(f"h{i}", "2026-09-01T00:00:00", same_sk)
            for i in range(6)]
    outside = [_entry(f"o{i}", "2026-09-01T00:00:00", _sketch(100 + i))
               for i in range(6)]
    assert nav.exhaustion_tier(same, outside) == "采尽"
    # 互相独立的箱（sketch 正交）→ 前缘
    indep = [_entry(f"x{i}", "2026-09-01T00:00:00", _sketch(200 + i))
             for i in range(6)]
    assert nav.exhaustion_tier(indep, []) == "前缘"
    # 样本不足 → 前缘（不冒充采尽）
    assert nav.exhaustion_tier([_entry("solo", "2026-09-01T00:00:00",
                                       _sketch(9))], []) == "前缘"


# ---- C3 禁忌 / C4 普查 ----

def test_mine_patterns_param_abstraction():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        # 参数不同的同模式 ×12 → 禁忌；独特模式 ×1
        for i in range(12):
            _store(root, f"{i:032x}", f"ts_mean(env.c, {i + 3})", "ts_mean")
        _store(root, "f" * 32, "ts_skew(env.v, 9)", "ts_skew")
        _trail(root, [_entry(f"{i:032x}", "2026-09-01T00:00:00")
                      for i in range(12)] + [_entry("f" * 32,
                                                    "2026-09-02T00:00:00")])
        bidx = nav.load_block_index(root)
        mined = nav.mine_patterns(bidx, nav.read_engine_trail(root),
                                  min_support=12)
        assert mined["n_trials"] == 13
        assert mined["sets"], "同参数抽象模式 12 次应成禁忌"
        assert mined["sets"][0][0] == ("close", "ts_mean")
        assert mined["sets"][0][1] == 12


def test_attractor_census_buckets_and_top5():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        for i in range(8):
            _store(root, f"{i:032x}", f"ts_mean(env.c, {i + 3})", "ts_mean")
        for i in range(4):
            _store(root, f"{i + 20:030x}", f"cs_rank(env.v)", "cs_rank")
        trail = ([_entry(f"{i:032x}", "2026-09-0%dT00:00:00" % (1 + i))
                  for i in range(8)]
                 + [_entry(f"{i + 20:030x}", "2026-09-20T00:00:00")
                    for i in range(4)])
        _trail(root, trail)
        cen = nav.attractor_census(nav.load_block_index(root),
                                   nav.read_engine_trail(root))
        assert cen["n_trials_used"] == 12
        assert cen["family_totals"]["ts"] == 8
        assert cen["family_totals"]["cs"] == 4
        assert cen["top5_family_sets"][0] == "ts"
        assert len(cen["buckets"]) >= 2      # 两个时间桶


# ---- 素材渲染红线 ----

def test_material_block_no_decimals_and_budget():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        for i in range(14):
            _store(root, f"{i:032x}", f"ts_mean(env.c, {i + 3})", "ts_mean")
        _store(root, "e" * 32, "cs_rank(env.v)", "cs_rank")
        _store(root, "f" * 32, "ts_skew(env.amount, 9)", "ts_skew")
        _trail(root, [_entry(f"{i:032x}", "2026-09-10T00:00:00")
                      for i in range(14)]
               + [_entry("e" * 32, "2026-09-11T00:00:00"),
                  _entry("f" * 32, "2026-09-12T00:00:00")])
        block = nav.navigation_material_block(root, 1200, cache_ttl_s=0)
        assert block, "素材非空"
        assert not _DECIMAL_RE.search(block), "素材不得含小数（评估分数形态）"
        assert "ts_mean" in block            # 禁忌模式在场（名词呈现）
        # 预算截断：小预算 ≤ 预算+单段余量
        small = nav.navigation_material_block(root, 60, cache_ttl_s=0)
        assert len(small) <= 200             # 至少一段（可能略超预算的段不砍半句）
        assert not _DECIMAL_RE.search(small)


def test_material_block_empty_root_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        assert nav.navigation_material_block(root, 1200,
                                             cache_ttl_s=0) == ""


def test_material_cache_freshness_cross_fill():
    """TTL=0 时重算：新箱填入后下次渲染即见（跨线实时性机制）。"""
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        _store(root, "a" * 32, "ts_mean(env.c, 5)", "ts_mean")
        _trail(root, [_entry("a" * 32, "2026-09-01T00:00:00")])
        b1 = nav.navigation_material_block(root, 1200, cache_ttl_s=0)
        assert "cs" not in b1
        _store(root, "b" * 32, "cs_rank(env.v)", "cs_rank")
        _trail(root, [_entry("a" * 32, "2026-09-01T00:00:00"),
                      _entry("b" * 32, "2026-09-02T00:00:00")])
        b2 = nav.navigation_material_block(root, 1200, cache_ttl_s=0)
        assert "cs" in b2, "新箱填入后重算可见"


# ---- bridge 接线：A3 名词化 + B2 取材 + navigation 段 ----

def _bridge(root: Path):
    from dsh_factor_mining.bridge import Bridge
    return Bridge(state_root=str(root), execution_mode="in_process")


def test_trail_summary_nominalized_and_navigation(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "journal" / "default").mkdir(parents=True)
    entries = [{
        "ts": "2026-08-27T12:00:00", "envId": "primary",
        "source_hash": "a" * 32, "stage": "development", "horizon": 20,
        "ic_ir": 0.22, "verdict": "pass", "origin": "hypothesis",
        "red_flags": ["f1", "f2"],
        "ic_series_sketch": [0.1] * 8,
        "tail": {"spread_ir": 0.44, "topn": {"placebo_z": 2.1}},
    }]
    (root / "trail_engine.json").write_text(json.dumps(entries),
                                            encoding="utf-8")
    b = _bridge(root)
    s = b.dispatch("state.trail_summary", {})
    last = s["last_engine_trail"][0]
    # A3 名词化：评估数字不进历史滚动视图
    assert set(last.keys()) == {"ts", "source_hash", "horizon", "stage",
                                "origin", "red_flags"}, last
    assert "ic_ir" not in last and "verdict" not in last
    assert "tail" not in last
    assert "verdict_counts" not in s["evaluations"]
    # 空库无素材：navigation 段缺省（不空挂）
    assert "navigation" not in s


def test_trail_summary_b2_dedupes_clone_wall(tmp_path):
    """同族克隆墙在历史示例视图里让位给结构不同的试验（B2）。"""
    root = tmp_path / "state"
    root.mkdir()
    clone_body = "ts_mean(add(cs_rank(env.v), absx(env.v)), {w})"
    for i in range(8):
        h = f"{i:032x}"
        p = root / "sources" / h[:2] / f"{h}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SRC_TMPL.format(
            ops="ts_mean, add, cs_rank, absx",
            body=clone_body.format(w=i + 3)), encoding="utf-8")
    dh = "d" * 32
    p = root / "sources" / "dd" / f"{dh}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC_TMPL.format(ops="ts_skew",
                                 body="ts_skew(env.amount, 9)"),
                 encoding="utf-8")
    trail = ([{"ts": f"2026-09-{i + 1:02d}T00:00:00", "source_hash":
               f"{i:032x}", "origin": "hypothesis", "stage": "development",
               "horizon": 20} for i in range(8)]
             + [{"ts": "2026-09-20T00:00:00", "source_hash": dh,
                 "origin": "hypothesis", "stage": "development",
                 "horizon": 20}])
    (root / "trail_engine.json").write_text(json.dumps(trail),
                                            encoding="utf-8")
    b = _bridge(root)
    s = b.dispatch("state.trail_summary", {})
    hashes = [e["source_hash"] for e in s["last_engine_trail"]]
    # 最新 1 条（本例恰为 distinct）保留；克隆在视图里被压缩
    assert len(hashes) <= 5
    assert dh[:12] in hashes


def test_collision_card_carries_taboo_footer(tmp_path):
    from dsh_factor_mining.collision import draw_card
    root = tmp_path / "state"
    (root / "journal" / "default").mkdir(parents=True)
    # 词表自举需词复现 ≥5 次（MIN_WORD_COUNT）
    (root / "journal" / "default" / "live.md").write_text(
        "## H-1 open 波动率聚集假设\n"
        + "波动率聚集与 volume 结构同源\n" * 6
        + "## H-2 open 反转假设\n" + "反转与 amount 惯性\n" * 6,
        encoding="utf-8")
    for i in range(14):
        h = f"{i:032x}"
        p = root / "sources" / h[:2] / f"{h}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SRC_TMPL.format(ops="ts_mean",
                                     body=f"ts_mean(env.c, {i + 3})"),
                     encoding="utf-8")
    (root / "trail_engine.json").write_text(json.dumps(
        [{"ts": "2026-09-01T00:00:00", "source_hash": f"{i:032x}",
          "origin": "hypothesis", "stage": "batch", "horizon": 10}
         for i in range(14)]), encoding="utf-8")
    card = draw_card(root, seed=42, k=2)
    assert "card_id" in card
    assert "构造禁忌" in card["note"], "负约束素材随卡可见"
    assert not _DECIMAL_RE.search(card["note"])


def test_loop_directive_carries_navigation(tmp_path):
    """驱动文本通道：策略指令尾部带名词素材（注入器注入 directive 正文）。"""
    root = tmp_path / "state"
    root.mkdir()
    (root / "journal" / "default").mkdir(parents=True)
    for i in range(14):
        h = f"{i:032x}"
        p = root / "sources" / h[:2] / f"{h}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SRC_TMPL.format(ops="ts_mean",
                                     body=f"ts_mean(env.c, {i + 3})"),
                     encoding="utf-8")
    (root / "trail_engine.json").write_text(json.dumps(
        [{"ts": "2026-09-01T00:00:00", "source_hash": f"{i:032x}",
          "origin": "hypothesis", "stage": "batch", "horizon": 10}
         for i in range(14)]), encoding="utf-8")
    b = _bridge(root)
    st = b.dispatch("state.mining.get", {})
    loop = b.dispatch("state.trail_summary", {})["loop"]
    assert loop.get("strategy") is not None
    directive = loop["strategy"].get("directive", "")
    assert "导航素材" in directive, "素材接进驱动文本"
    assert not _DECIMAL_RE.search(directive)
    assert st.get("navigation_materials") is True


if __name__ == "__main__":
    import tempfile as _tf
    for fn in [test_coverage_bins_density_and_recency,
               test_exhaustion_tiers_boundaries,
               test_mine_patterns_param_abstraction,
               test_attractor_census_buckets_and_top5,
               test_material_block_no_decimals_and_budget,
               test_material_block_empty_root_returns_empty,
               test_material_cache_freshness_cross_fill]:
        fn()
        print(f"ok {fn.__name__}")
    with _tf.TemporaryDirectory() as d:
        test_trail_summary_nominalized_and_navigation(Path(d))
        print("ok test_trail_summary_nominalized_and_navigation")
    with _tf.TemporaryDirectory() as d:
        test_trail_summary_b2_dedupes_clone_wall(Path(d))
        print("ok test_trail_summary_b2_dedupes_clone_wall")
    with _tf.TemporaryDirectory() as d:
        test_collision_card_carries_taboo_footer(Path(d))
        print("ok test_collision_card_carries_taboo_footer")
    with _tf.TemporaryDirectory() as d:
        test_loop_directive_carries_navigation(Path(d))
        print("ok test_loop_directive_carries_navigation")
