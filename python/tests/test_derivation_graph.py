# -*- coding: utf-8 -*-
"""派生图存储五不变量 + golden 重放（调度器票 1a，2026-09-29 任务书）。

锁四件事：
I1 无环（ancestry 环检测 ValueError + check_invariants.acyclic）
I2 子表达式可复现——drop_leg 可重放 op：zero_leg_source 变换的
   source_fingerprint 与图记录 hash 一致（golden：真实多腿源码）
I3 单写者——全部写路径过 state_write_lock（进程内重入下嵌套安全）
I5 首见冻结 + 状态尾行覆盖 + 台账投影导出
边来源三纪律枚举校验。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining import derivation
from dsh_factor_mining.discipline import source_fingerprint
from dsh_factor_mining.factor.attribution import zero_leg_source


QUINT5 = '''def factor(env):
    import pandas as pd
    from dsh_factor_mining.factor.ops import cs_rank, ts_cov, ts_std, ts_mean, log1p_abs, ts_scale, ts_momentum, ts_corr, negx
    h = pd.DataFrame(env.h)
    v = pd.DataFrame(env.v)
    c = pd.DataFrame(env.c)
    amt = pd.DataFrame(env.amount)
    s1 = cs_rank(-ts_cov(h, v, 10))
    s2 = cs_rank(-ts_std(h, 5))
    s3 = cs_rank(-ts_mean(log1p_abs(cs_scale(ts_momentum(amt, 120))), 3))
    s4 = cs_rank(ts_corr(v, negx(c), 10))
    return ((s1 + s2 + s3 + s4) / 4.0).values
'''


def test_node_idempotent_first_seen_frozen(tmp_path):
    root = str(tmp_path)
    r1 = derivation.note_node(root, "hashA", None, None, "root_unlinked",
                               lane="default", origin="hypothesis")
    r2 = derivation.note_node(root, "hashA", "other", "add_leg", "declared")
    assert r1["dedup"] is False and r2["dedup"] is True
    nodes = derivation.load_nodes(root)
    assert nodes["hashA"]["parent"] is None, "首见冻结：族谱不可事后改写"
    assert nodes["hashA"]["op"] is None


def test_edge_source_enum_enforced(tmp_path):
    with pytest.raises(ValueError):
        derivation.note_node(tmp_path, "x", None, None, "bogus_source")


def test_status_tail_line_overrides(tmp_path):
    root = str(tmp_path)
    derivation.note_node(root, "h1", None, None, "farm_seed")
    derivation.update_status(root, "h1", "incubating")
    derivation.update_status(root, "h1", "frontier")
    nodes = derivation.load_nodes(root)
    assert nodes["h1"]["status"] == "frontier", "状态机活：尾行覆盖"
    assert nodes["h1"]["edge_source"] == "farm_seed", "边字段仍首见冻结"


def test_acyclic_detection(tmp_path):
    root = str(tmp_path)
    # 首见即环：a→b→c→a（手工写行——生产写路径推断不出环，这里验证
    # 读端防御 I1 的机械性）
    rows = [
        {"hash": "a", "parent": "c", "op": "variant",
         "edge_source": "declared", "ts": "t1", "status": "frontier"},
        {"hash": "b", "parent": "a", "op": "variant",
         "edge_source": "declared", "ts": "t2", "status": "frontier"},
        {"hash": "c", "parent": "b", "op": "variant",
         "edge_source": "declared", "ts": "t3", "status": "frontier"},
    ]
    with open(derivation._graph_path(Path(root)), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    nodes = derivation.load_nodes(root)
    with pytest.raises(ValueError):
        derivation.ancestry(root, "a", nodes)
    inv = derivation.check_invariants(root, nodes)
    assert inv["acyclic"] is False
    assert set(inv["cycles"]) == {"a", "b", "c"}


def test_golden_replay_drop_leg(tmp_path):
    """I2 golden：多腿源码 → drop_leg 变换 → 重放哈希一致（任务书 1a
    验收：任一因子从事件流重建族谱，重放哈希一致）。"""
    root = str(tmp_path)
    parent_h = source_fingerprint(QUINT5)
    child_src = zero_leg_source(QUINT5, "s2")
    child_h = source_fingerprint(child_src)
    derivation.note_node(root, parent_h, None, None, "root_unlinked",
                         origin="hypothesis")
    derivation.note_node(root, child_h, parent_h, "drop_leg", "order",
                         op_params={"leg": "s2"})
    inv = derivation.check_invariants(root)
    assert inv["acyclic"] is True
    assert inv["replay"] is True and not inv["replay_failures"], \
        f"重放校验失败: {inv['replay_failures']}"
    # 注册表直查
    replayed = derivation.replay_child("drop_leg", QUINT5, {"leg": "s2"})
    assert source_fingerprint(replayed) == child_h


def test_replay_detects_drift(tmp_path):
    """边记录漂移（op_params 错腿）→ 重放失败被查出（防账本与源码失联）。"""
    root = str(tmp_path)
    parent_h = source_fingerprint(QUINT5)
    child_h = source_fingerprint(zero_leg_source(QUINT5, "s1"))
    derivation.note_node(root, parent_h, None, None, "root_unlinked")
    # 记录的腿与实际子代不符（s3 ≠ s1）
    derivation.note_node(root, child_h, parent_h, "drop_leg", "order",
                         op_params={"leg": "s3"})
    from dsh_factor_mining.state import store_factor_source
    store_factor_source(QUINT5, root)
    inv = derivation.check_invariants(root)
    assert inv["replay"] is False and inv["replay_failures"], \
        "漂移边必须被重放校验查出"


def test_family_and_descendants(tmp_path):
    root = str(tmp_path)
    derivation.note_node(root, "r", None, None, "farm_seed")
    derivation.note_node(root, "x", "r", "variant", "declared")
    derivation.note_node(root, "y", "x", "variant", "declared")
    derivation.note_node(root, "z", None, None, "root_unlinked")
    assert derivation.family_root(root, "y") == "r"
    assert set(derivation.descendants(root, "r")) == {"r", "x", "y"}
    assert derivation.family_root(root, "z") == "z"


def test_events_append_and_cache_invalidation(tmp_path):
    root = str(tmp_path)
    derivation.note_expand(root, "child1", "p", "variant", "declared",
                           compute={"cpu_s": 1.2, "slots": 1},
                           new_candidate=True, lane="default",
                           origin="hypothesis", horizon=20,
                           stage="development",
                           extra={"p_def": 0.12, "gain": 0.31})
    derivation.note_terminal(root, "child1", "admitted", t_def=2.5,
                             p_def=0.03)
    with pytest.raises(ValueError):
        derivation.note_score(root, "child1", {"A": 1.0})  # 缺 fn_version 拒绝
    derivation.note_score(root, "child1", {"A": 1.0}, rank=0,
                          fn_version="v1.1")
    evs = derivation.load_events(root)
    kinds = [e["ev"] for e in evs]
    assert kinds == ["expand", "terminal", "score"]
    assert evs[0]["p_def"] == 0.12 and evs[0]["gain"] == 0.31, \
        "extra 快照字段必须并入事件"
    assert evs[2]["fn_version"] == "v1.1"


def test_ledger_projection(tmp_path):
    root = str(tmp_path)
    derivation.note_node(root, "r", None, None, "farm_seed", origin="random")
    derivation.note_node(root, "x", "r", "variant", "declared",
                         lane="default")
    derivation.update_status(root, "x", "admitted")
    text = derivation.export_ledger_view(root, out_path=tmp_path / "v.md")
    assert "族 " in text and "admitted" in text
    assert (tmp_path / "v.md").exists()
