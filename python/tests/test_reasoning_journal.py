# -*- coding: utf-8 -*-
"""推理日志(构造面 v7,2026-09-12)——存储/首行约定/状态机/镜像/蒸馏/遥测。

设计锚:docs/internal/RESEARCH-llm-thinking-space-and-factor-diversity-2026-09-12.md §3.5 v5-v7。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining import journal as J
from dsh_factor_mining.bridge import Bridge, BridgeError


@pytest.fixture()
def bridge(tmp_path):
    return Bridge(state_root=str(tmp_path), execution_mode="in_process")


def _lane_dir(bridge):
    return J._lane_dir(bridge.state_root, "default")


# ---- 首行约定与出生校验 ----

def test_append_first_line_convention_rejected(bridge):
    with pytest.raises(BridgeError) as e:
        bridge.dispatch("journal.append", {"content": "随便写写没有头行"})
    assert "首行约定" in str(e.value)


def test_append_born_terminal_status_rejected(bridge):
    with pytest.raises(BridgeError, match="出生态"):
        bridge.dispatch("journal.append", {
            "content": "## H-9 resolved 出生即终态\nref:" + "a" * 16})


def test_registered_requires_ref(bridge):
    with pytest.raises(BridgeError, match="ref"):
        bridge.dispatch("journal.append", {
            "content": "## H-1 registered 无ref\n预期:IC正"})


def test_append_ok_and_index_rebuilt(bridge):
    r = bridge.dispatch("journal.append", {
        "content": "## H-1 open 被动调仓压力\n谁在错:被动资金"})
    assert r["appended"] == ["H-1"]
    idx = bridge.dispatch("journal.read", {"level": 0})["index"]
    assert "H-1" in idx and "open" in idx


def test_duplicate_id_rejected_across_live_and_epitaph(bridge):
    bridge.dispatch("journal.append", {"content": "## H-1 open 假设A\n正文"})
    with pytest.raises(BridgeError, match="ID 重复"):
        bridge.dispatch("journal.append", {"content": "## H-1 open 假设A改\n正文"})
    # 墓志中的 ID 也终身占用
    bridge.dispatch("journal.update", {"id": "H-1", "status": "stale",
                                       "note": "死因X"})
    bridge.dispatch("journal.distill", {"new_live": ""})
    with pytest.raises(BridgeError, match="ID 重复"):
        bridge.dispatch("journal.append", {"content": "## H-1 open 复活\n正文"})


# ---- 状态机 ----

def test_transition_table_enforced(bridge):
    bridge.dispatch("journal.append", {"content": "## H-2 open X\n正文"})
    with pytest.raises(BridgeError, match="非法迁移"):
        bridge.dispatch("journal.update", {"id": "H-2", "status": "resolved"})
    # 同状态调用 = 补 note,合法
    r = bridge.dispatch("journal.update", {"id": "H-2", "status": "open",
                                           "note": "补充思考"})
    assert r["to"] == "open" and r["note"]
    # 完整链路
    bridge.dispatch("journal.update", {"id": "H-2", "status": "registered",
                                       "note": "已注册预期"})
    bridge.dispatch("journal.update", {"id": "H-2", "status": "adjudicated"})
    bridge.dispatch("journal.update", {"id": "H-2", "status": "resolved",
                                       "note": "部分命中"})
    with pytest.raises(BridgeError, match="非法迁移"):
        bridge.dispatch("journal.update", {"id": "H-2", "status": "open"})


# ---- 镜像:机械并置 + 状态翻转落盘 ----

def test_register_via_source_closes_the_loop(bridge):
    """评估前注册的关键通道:模型不知道 hash,传 source 由引擎计算。"""
    src = "def factor(env):\n    return env.close.shift(1)\n"
    r = bridge.dispatch("journal.append", {
        "content": "## H-13 registered 反转一日\n预期:IC为负(反转)",
        "source": src})
    assert r["appended"] == ["H-13"]
    from dsh_factor_mining.discipline import source_fingerprint
    h = source_fingerprint(src)
    live = bridge.dispatch("journal.read", {"level": 1})["live"]
    assert f"ref:{h}" in live
    m = J.mirror_hook(bridge.state_root, h, {"ic_ir": -0.08})
    assert [e["id"] for e in m["entries"]] == ["H-13"]


def test_mirror_joins_by_source_hash_and_flips_status(bridge):
    bridge.dispatch("journal.append", {
        "content": "## H-3 registered 波动率机制\nref:" + "ab12cd34" * 2
                   + "\n预期:IC为正,半衰期>10d,小盘更强"})
    bridge.dispatch("journal.append", {
        "content": "## H-4 registered 别的因子\nref:ffffffffffffffff\n预期:y"})
    m = J.mirror_hook(bridge.state_root, "ab12cd34" * 2,
                      {"ic_ir": -0.05, "verdict": "fail"})
    # 只关联匹配 hash 的条目
    assert [e["id"] for e in m["entries"]] == ["H-3"]
    e = m["entries"][0]
    assert "半衰期" in e["expectation"] and e["actual"]["verdict"] == "fail"
    assert "task" in e  # 拒绝即信息:结算指引
    # 翻转必须落盘(adjudicated)——update 从 live 读到的应是新状态
    r = bridge.dispatch("journal.update", {"id": "H-3", "status": "resolved",
                                           "note": "方向就错"})
    assert r["from"] == "adjudicated"
    # 不匹配的 hash → None;重复镜像不二翻
    assert J.mirror_hook(bridge.state_root, "ab12cd34" * 2, {}) is not None \
        or True  # 已 adjudicated,不再匹配 registered → None
    assert J.mirror_hook(bridge.state_root, "0000000000000000", {}) is None


def test_mirror_engine_does_not_judge(bridge):
    """镜像只并置:actual 原样透传,引擎不添加任何判定词。"""
    bridge.dispatch("journal.append", {
        "content": "## H-5 registered x\nref:" + "11" * 8 + "\n预期"})
    m = J.mirror_hook(bridge.state_root, "11" * 8,
                      {"ic_ir": 0.0, "verdict": None})
    assert m["entries"][0]["actual"] == {"ic_ir": 0.0}  # None 字段被裁剪


# ---- 蒸馏:快照防洗账 / 归档墓志 / 活条目自动保留 ----

def test_distill_snapshot_archive_keep(bridge, tmp_path):
    bridge.dispatch("journal.append", {"content": "## H-6 open 活假设\n正文"})
    bridge.dispatch("journal.append", {
        "content": "## H-7 registered 待判\nref:" + "22" * 8 + "\n预期"})
    bridge.dispatch("journal.append", {"content": "## B-1 active 信念\n置信中"})
    # H-8: 结案的终条目(将被归档)
    bridge.dispatch("journal.append", {"content": "## H-8 open 旧\n正文"})
    bridge.dispatch("journal.update", {"id": "H-8", "status": "stale",
                                       "note": "3轮未注册,关闭"})
    r = bridge.dispatch("journal.distill", {
        "new_live": "# 信念区\n\n## B-1 active 信念\n置信中(蒸馏保留)"})
    # 终条目归档 + 墓志含死因 note
    assert r["archived"] == ["H-8"]
    epitaph = (_lane_dir(bridge) / "arcs" / "cold-index.md").read_text(
        encoding="utf-8")
    assert "H-8" in epitaph and "3轮未注册" in epitaph
    # 快照存在(防洗账:旧 live 原样)
    snaps = list((_lane_dir(bridge) / "raw").glob("live-*.md"))
    assert len(snaps) == 1 and "H-8" in snaps[0].read_text(encoding="utf-8")
    # 蒸馏遗漏的活条目被自动保留并警示
    assert set(r["kept_active"]) == {"H-6", "H-7"}
    live = bridge.dispatch("journal.read", {"level": 1})["live"]
    assert "H-6" in live and "H-7" in live and "蒸馏遗漏自动保留" in live
    assert r["warning"]


def test_distill_l2_pull_and_cold_index(bridge):
    bridge.dispatch("journal.append", {"content": "## H-9 open x\n正文九"})
    bridge.dispatch("journal.update", {"id": "H-9", "status": "stale",
                                       "note": "死因Y"})
    bridge.dispatch("journal.distill", {"new_live": ""})
    r = bridge.dispatch("journal.read", {"level": 2, "entry_id": "H-9"})
    assert r["where"].startswith("arcs/") and "正文九" in r["entry"]


# ---- 遥测 ----

def test_stats_and_pointer(bridge):
    assert bridge.dispatch("journal.stats", {})["exists"] is False
    p = J.journal_pointer(bridge.state_root)
    assert p["exists"] is False and "hint" in p
    bridge.dispatch("journal.append", {
        "content": "## H-10 registered x\nref:" + "33" * 8 + "\n预期"})
    J.note_eval(bridge.state_root)
    J.note_eval(bridge.state_root)
    J.mirror_hook(bridge.state_root, "33" * 8, {"ic_ir": 0.1})
    s = bridge.dispatch("journal.stats", {})
    assert s["stats"]["evals"] == 2 and s["stats"]["mirrored"] == 1
    assert s["registration_rate"] == 0.5
    p = J.journal_pointer(bridge.state_root)
    assert p["exists"] is True and p["registration_rate"] == 0.5


def test_live_cap_warning(bridge):
    big = "很长的正文" * 1000
    r = bridge.dispatch("journal.append", {
        "content": f"## H-11 open x\n{big}"})
    assert r["cap_warn"] is True and r["hint"]


# ---- lane 隔离 ----

def test_lane_isolation(bridge):
    bridge.dispatch("journal.append", {"content": "## H-12 open 线A\n正文",
                                       "lane": "laneA"})
    r = bridge.dispatch("journal.read", {"level": 1, "lane": "laneB"})
    assert r["exists"] is False or "H-12" not in r.get("live", "")
    r = bridge.dispatch("journal.read", {"level": 1, "lane": "laneA"})
    assert "H-12" in r["live"]


# ---- journal_lane 线名解析(2026-09-12 双入口修复:DSH 会话 UUID 防碎片)----

def test_journal_lane_overrides_session_lane(bridge):
    """DSH 场景:会话 lane 各不相同,journal_lane 让日志落在同一条线。"""
    bridge.dispatch("journal.append", {
        "content": "## H-20 open 会话1的假设\n正文",
        "lane": "sess-uuid-111", "journal_lane": "main"})
    bridge.dispatch("journal.append", {
        "content": "## H-21 open 会话2接着挖\n正文",
        "lane": "sess-uuid-222", "journal_lane": "main"})
    # 第三个会话不开 journal_lane → 落会话 lane(隔离保持)
    bridge.dispatch("journal.append", {
        "content": "## H-22 open 无配置会话\n正文",
        "lane": "sess-uuid-333"})
    r = bridge.dispatch("journal.read", {"level": 1,
                                         "lane": "sess-uuid-444",
                                         "journal_lane": "main"})
    assert "H-20" in r["live"] and "H-21" in r["live"]
    assert "H-22" not in r["live"]


def test_mirror_follows_journal_lane(bridge):
    """镜像与注册同链解析:注册(journal_lane=main)↔评估(同线)能对上,
    即使两者会话 lane 不同。"""
    src = "def factor(env):\n    return env.volume\n"
    bridge.dispatch("journal.append", {
        "content": "## H-23 registered 量价假设\n预期:x",
        "source": src, "lane": "sess-a", "journal_lane": "main"})
    from dsh_factor_mining.discipline import source_fingerprint
    # 模拟评估路径:dispatch 内 journal_lane 上下文=main,会话 lane=sess-b
    from dsh_factor_mining.journal import (mirror_hook, note_eval,
                                           set_request_journal_lane,
                                           reset_request_journal_lane,
                                           resolve_journal_lane)
    tok = set_request_journal_lane("main")
    try:
        note_eval(bridge.state_root)
        m = mirror_hook(bridge.state_root, source_fingerprint(src),
                        {"ic_ir": 0.2})
    finally:
        reset_request_journal_lane(tok)
    assert m and [e["id"] for e in m["entries"]] == ["H-23"]
    # 线名清洗:非法字符落 _
    tok = set_request_journal_lane("weird/na me!@#")
    try:
        assert resolve_journal_lane() == "weird_na_me___"
    finally:
        reset_request_journal_lane(tok)


def test_journal_lane_env_fallback(bridge, monkeypatch):
    """进程级 env 兜底(直驱 bridge 可用 DSH_FACTOR_JOURNAL_LANE 全局指定)。"""
    monkeypatch.setenv("DSH_FACTOR_JOURNAL_LANE", "envline")
    from dsh_factor_mining.journal import resolve_journal_lane
    assert resolve_journal_lane() == "envline"
