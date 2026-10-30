# coding=utf-8
"""D1 发散相多底并行（降级形态=跨 lane 模型混布）判据测试（2026-10-14 任务书）。

启停自动规则（无需人工拍板）：三前提全满足才开；对照期 4 周或每臂
≥300 次提交，判据不满足自动关回；事件落 gate_audit.jsonl。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import divergence as dv  # noqa: E402

SRC = "def factor(env):\n    from dsh_factor_mining.factor.ops import {op}\n    return ({op}(env.{leaf}, {w})).values\n"


def _mk_root(tmp: Path):
    root = tmp / "state"
    root.mkdir(parents=True)
    return root


def _store(root: Path, h: str, op: str, leaf: str, w: int):
    p = root / "sources" / h[:2] / f"{h}.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC.format(op=op, leaf=leaf, w=w), encoding="utf-8")


def _census(path: Path, top5: list):
    path.write_text(json.dumps({"top5_family_sets": top5,
                                "n_trials_used": 100}), encoding="utf-8")


# ---- 选型依据计算器 ----

def test_selection_overlap_boundaries():
    assert dv.selection_overlap(["ts", "cs", "unary", "cmp", "cond"],
                                ["ts", "cs", "unary", "cmp", "cond"]) == 1.0
    assert dv.selection_overlap(["ts", "cs"], ["binary", "unary"]) == 0.0
    # 3/5 交叠 = 0.6 > 0.3 阈
    ov = dv.selection_overlap(["a", "b", "c", "d", "e"],
                              ["a", "b", "c", "f", "g"])
    assert abs(ov - 3 / 7) < 1e-9


# ---- 三前提核对 ----

def test_preconditions_all_needed():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = _mk_root(tmp)
        c1, c2 = tmp / "cen1.json", tmp / "cen2.json"
        # 无普查 → 不满足
        pre = dv.check_preconditions(root, [str(c1), str(c2)],
                                     lanes_bound=True)
        assert pre["ok"] is False and pre["checks"]["census_ready"] is False
        # 有普查但重叠高 → 不满足
        _census(c1, ["ts", "cs", "unary", "cmp", "cond"])
        _census(c2, ["ts", "cs", "unary", "cmp", "cond"])
        pre = dv.check_preconditions(root, [str(c1), str(c2)],
                                     lanes_bound=True)
        assert pre["ok"] is False and pre["checks"]["overlap_ok"] is False
        # 重叠低但混布未声明 → 不满足
        _census(c2, ["binary", "cond", "inv", "minv", "maxv"])
        pre = dv.check_preconditions(root, [str(c1), str(c2)],
                                     lanes_bound=False)
        assert pre["ok"] is False and pre["checks"]["lanes_bound"] is False
        # 三前提齐 → 满足（边界：恰 0.3 算过）
        pre = dv.check_preconditions(root, [str(c1), str(c2)],
                                     lanes_bound=True)
        assert pre["overlap"] <= 0.3 and pre["ok"] is True


# ---- 启停 ----

def test_maybe_enable_and_disable_events():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        root = _mk_root(tmp)
        c1, c2 = tmp / "cen1.json", tmp / "cen2.json"
        _census(c1, ["ts", "cs", "unary", "cmp", "cond"])
        _census(c2, ["binary", "cond", "inv", "minv", "maxv"])
        # lanes_bound 声明 + 普查路径走 mining_state 配置覆盖
        (root / "mining_state.json").write_text(json.dumps(
            {"multi_model_lanes_bound": True,
             "d1_census_files": [str(c1), str(c2)]}), encoding="utf-8")
        r = dv.maybe_enable(root)
        assert r["enabled"] is True
        st = json.loads((root / "mining_state.json").read_text(
            encoding="utf-8"))
        assert st["multi_model_divergence"] is True
        ev = (root / "gate_audit.jsonl").read_text(encoding="utf-8")
        assert "d1_enabled" in ev
        # 幂等：再调不重复发事件语义（already=True）
        r2 = dv.maybe_enable(root)
        assert r2.get("already") is True
        # 前提不满足时不动开关
        dv.disable(root, "test")
        (root / "mining_state.json").write_text(json.dumps(
            {"multi_model_lanes_bound": False}), encoding="utf-8")
        r3 = dv.maybe_enable(root)
        assert r3["enabled"] is False


# ---- 对照判据执行器 ----

def _arm_trail(root: Path, lane: str, op: str, leaf: str, n: int,
               start_day=1):
    """一个臂：n 个同构试验（同 op/leaf）——两两相似度高。

    哈希必须全局唯一（sources 是内容寻址库，撞哈希=覆写）。"""
    out = []
    for i in range(n):
        h = f"{lane[:2]}{op[0]}{leaf}{i:028x}"[:32].ljust(32, "0")
        _store(root, h, op, leaf, i + 3)
        out.append({"ts": f"2026-09-{start_day:02d}T00:00:00",
                    "source_hash": h, "lane": lane, "origin": "hypothesis",
                    "stage": "development", "horizon": 10})
    return out


def test_contrast_shutdown_and_keep():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        # 基线臂：单一构造域（全相似）；多底臂：两个构造域混布（更分散）
        base = _arm_trail(root, "lane_base", "ts_mean", "c", 6)
        m1 = _arm_trail(root, "lane_multi", "ts_mean", "c", 3)
        m2 = _arm_trail(root, "lane_multi", "cs_rank", "v", 3, start_day=2)
        trail = base + m1 + m2
        (root / "trail_engine.json").write_text(json.dumps(trail),
                                                encoding="utf-8")
        (root / "mining_state.json").write_text(json.dumps(
            {"multi_model_divergence": True}), encoding="utf-8")
        out = dv.contrast(root, "lane_multi", "lane_base",
                          baseline_cutoff_ts="2026-08-01T00:00:00")
        # 多底臂混合两域：两两相似度中位数 0（跨域对）< 基线臂 1.0；
        # 两臂都只命中既有箱 → 新域命中相等（≥ 成立）→ keep
        assert out["verdict"] == "keep", out
        st = json.loads((root / "mining_state.json").read_text(
            encoding="utf-8"))
        assert st["multi_model_divergence"] is True
        # 反例：多底臂比基线更同质（全克隆）→ shutdown + 自动关回
        root2_trail = []
        with tempfile.TemporaryDirectory() as d2:
            root2 = _mk_root(Path(d2))
            b = _arm_trail(root2, "lane_base", "ts_mean", "c", 3)
            b += _arm_trail(root2, "lane_base", "cs_rank", "v", 3,
                            start_day=2)
            m = _arm_trail(root2, "lane_multi", "ts_mean", "c", 6)
            (root2 / "trail_engine.json").write_text(json.dumps(b + m),
                                                     encoding="utf-8")
            (root2 / "mining_state.json").write_text(json.dumps(
                {"multi_model_divergence": True}), encoding="utf-8")
            out2 = dv.contrast(root2, "lane_multi", "lane_base",
                               baseline_cutoff_ts="2026-08-01T00:00:00")
            assert out2["verdict"] == "shutdown", out2
            st2 = json.loads((root2 / "mining_state.json").read_text(
                encoding="utf-8"))
            assert st2["multi_model_divergence"] is False
            ev = (root2 / "gate_audit.jsonl").read_text(encoding="utf-8")
            assert "d1_disabled" in ev


def test_contrast_insufficient_sample_no_flip():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        (root / "trail_engine.json").write_text(json.dumps(
            [{"ts": "2026-09-01T00:00:00", "source_hash": "a" * 32,
              "lane": "lane_multi", "origin": "hypothesis",
              "stage": "development"}]), encoding="utf-8")
        (root / "mining_state.json").write_text(json.dumps(
            {"multi_model_divergence": True}), encoding="utf-8")
        out = dv.contrast(root, "lane_multi", "lane_base")
        assert out["verdict"] == "insufficient"
        st = json.loads((root / "mining_state.json").read_text(
            encoding="utf-8"))
        assert st["multi_model_divergence"] is True   # 不虚判不动开关


def test_d1_status_view():
    with tempfile.TemporaryDirectory() as d:
        root = _mk_root(Path(d))
        st = dv.d1_status(root)
        assert st["enabled"] is False


if __name__ == "__main__":
    import tempfile as _tf
    for fn in [test_selection_overlap_boundaries,
               test_preconditions_all_needed]:
        fn()
        print(f"ok {fn.__name__}")
    for fn in [test_maybe_enable_and_disable_events,
               test_contrast_shutdown_and_keep,
               test_contrast_insufficient_sample_no_flip,
               test_d1_status_view]:
        fn()
        print(f"ok {fn.__name__}")
