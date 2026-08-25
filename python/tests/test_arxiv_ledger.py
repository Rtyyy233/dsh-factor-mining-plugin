# coding=utf-8
"""arxiv 文献通道修复 F1-F5 回归（2026-08-24 审计产品化）。

审计实证（session.jsonl 28 次调用 / 140 条返回）：
- 31% 返回是物理/数学噪声（cross-section/volume/momentum 与物理词碰撞）
- 查询零重复但 26% 论文级重叠（查询层去重无效，必须论文层）
- 论文→假设无溯源，「迁移自论文X」宣称不可审计
- 查询全落在 agent 当前族词汇内（相关性检索 = 强化先验的回音室）

修复：F1 论文台账 / F2 q-fin 类目默认 / F3 分层采样+分页 /
F4 papers 溯源 + exhausted / F5 种子轮转。全部 mock 网络层。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dsh_factor_mining.arxiv as arxiv_mod  # noqa: E402
from dsh_factor_mining.bridge import (  # noqa: E402
    Bridge, BridgeError, _QFIN_FILTER, _pick_strategy,
)


def _mkbridge(tmp_path: Path) -> Bridge:
    return Bridge(state_root=str(tmp_path / "state"),
                  execution_mode="in_process")


def _fake_papers(n: int, prefix: str = "2108."):
    return [{"title": f"Paper {prefix}{i:05d}", "summary": "s",
             "arxiv_id": f"http://arxiv.org/abs/{prefix}{i:05d}",
             "published": "2024-01-01"} for i in range(n)]


# ---- F2: 类目过滤默认 ----

def test_qfin_filter_default(tmp_path, monkeypatch):
    """未传 category → 注入 q-fin OR 链；显式传 → 单类目。"""
    b = _mkbridge(tmp_path)
    captured = []

    def fake_search(query, max_results=10, category_filter=None,
                    sort_by="relevance", start=0):
        captured.append(category_filter)
        return _fake_papers(3)

    monkeypatch.setattr(arxiv_mod, "search", fake_search)
    b.dispatch("arxiv.search", {"query": "momentum cross-section"})
    assert all(c == _QFIN_FILTER for c in captured), captured
    assert "q-fin.ST" in _QFIN_FILTER

    captured.clear()
    b.dispatch("arxiv.search", {"query": "momentum", "category": "q-fin.ST"})
    # 分层采样 → 两次调用（relevance + submittedDate）都用显式类目
    assert set(captured) == {"cat:q-fin.ST"}, captured


def test_url_composition_pure():
    """URL 组合纯函数：分层/分页参数直达 API。"""
    url = arxiv_mod.compose_search_url(
        "momentum", 10, "(cat:q-fin.ST OR cat:q-fin.PM)",
        "submittedDate", 40)
    assert "cat%3Aq-fin.ST+OR" in url or "cat:q-fin.ST OR" in url.replace("+", " ")
    assert "sortBy=submittedDate" in url
    assert "start=40" in url


def test_search_body_executes(monkeypatch):
    """真实执行 search 函数体（mock 网络层返回 Atom XML）——
    防「签名改了函数体还引用旧变量名」类 bug（2026-08-24 部署实测踩过：
    category→category_filter 重命名漏改函数体，mock 测试全部漏检）。"""
    atom = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><title>Momentum Factor </title>"
        "<summary> method </summary>"
        "<id>http://arxiv.org/abs/2511.12490v1</id>"
        "<published>2025-11-15T00:00:00Z</published></entry>"
        "</feed>")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return atom.encode("utf-8")

    def fake_urlopen(url, timeout=30):
        assert "search_query=" in url
        return _Resp()

    monkeypatch.setattr(arxiv_mod.urllib.request, "urlopen", fake_urlopen)
    out = arxiv_mod.search("momentum", 5, "(cat:q-fin.ST)", "relevance", 0)
    assert isinstance(out, list) and out[0]["arxiv_id"].endswith("2511.12490v1")
    assert out[0]["title"] == "Momentum Factor"


# ---- F3: 分层采样 + 分页 ----

def test_stratified_two_calls(tmp_path, monkeypatch):
    """无 start → 拆 relevance + submittedDate 两次调用（新旧覆盖）。"""
    b = _mkbridge(tmp_path)
    sorts = []

    def fake_search(query, max_results=10, category_filter=None,
                    sort_by="relevance", start=0):
        sorts.append(sort_by)
        return _fake_papers(3, prefix="2201.")

    monkeypatch.setattr(arxiv_mod, "search", fake_search)
    b.dispatch("arxiv.search", {"query": "volatility"})
    assert sorted(sorts) == ["relevance", "submittedDate"], sorts


def test_start_deep_walk_single_call(tmp_path, monkeypatch):
    """显式 start → 单次 relevance 调用带偏移（深部翻页模式）。"""
    b = _mkbridge(tmp_path)
    calls = []

    def fake_search(query, max_results=10, category_filter=None,
                    sort_by="relevance", start=0):
        calls.append((sort_by, start))
        return _fake_papers(3)

    monkeypatch.setattr(arxiv_mod, "search", fake_search)
    b.dispatch("arxiv.search", {"query": "volatility", "start": 30})
    assert calls == [("relevance", 30)], calls


# ---- F1: 台账去重 ----

def test_ledger_seen_before_and_fresh_first(tmp_path, monkeypatch):
    """第二次检索：已见论文带 seen_before 且让位给未见论文。"""
    b = _mkbridge(tmp_path)
    pool = _fake_papers(6, prefix="2301.")
    state = {"shift": 0}

    def fake_search(query, max_results=10, category_filter=None,
                    sort_by="relevance", start=0):
        # 第一次返回前 4，第二次返回同一批 4 + 2 新（模拟结果集重叠）
        if state["shift"] == 0:
            state["shift"] = 1
            return pool[:4]
        return pool  # 全部 6 篇（含已见 4 + 新 2）

    monkeypatch.setattr(arxiv_mod, "search", fake_search)
    r1 = b.dispatch("arxiv.search", {"query": "q", "max_results": 4})
    assert r1["fresh"] == 4
    r2 = b.dispatch("arxiv.search", {"query": "q2", "max_results": 4})
    ids = [x["arxiv_id"] for x in r2["results"]]
    # 未见 2 篇优先占槽
    assert pool[4]["arxiv_id"] in ids and pool[5]["arxiv_id"] in ids
    # 已见论文带 seen_before 标注
    seen = [x for x in r2["results"] if "seen_before" in x]
    assert seen, r2["results"]
    # 台账落盘
    d = json.loads((tmp_path / "state" / "papers.json").read_text("utf-8"))
    assert d["searches"] == 2
    assert len(d["papers"]) >= 6


def test_excluded_exhausted(tmp_path, monkeypatch):
    """exhausted 论文直接从结果排除。"""
    b = _mkbridge(tmp_path)
    # 预置台账：paper 0 已 exhausted
    d = {"papers": {"2108.00000": {"exhausted": True, "times_returned": 3}},
         "searches": 5}
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "papers.json").write_text(
        json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def fake_search(query, max_results=10, category_filter=None,
                    sort_by="relevance", start=0):
        return _fake_papers(2)

    import dsh_factor_mining.bridge as bridge_mod
    monkeypatch.setattr(bridge_mod, "_unused_", None, raising=False)
    monkeypatch.setattr(arxiv_mod, "search", fake_search)
    r = b.dispatch("arxiv.search", {"query": "q", "max_results": 5})
    ids = [x["arxiv_id"] for x in r["results"]]
    assert "http://arxiv.org/abs/2108.00000" not in ids, ids
    assert r["ledger"]["exhausted"] == 1


# ---- F4: papers 溯源 ----

def test_trail_papers_validation_and_citation(tmp_path):
    """trail papers 字段：非法拒绝；合法记入台账 cited_rounds。"""
    b = _mkbridge(tmp_path)
    with pytest.raises(BridgeError, match="papers"):
        b.dispatch("paths.append", {"layer": "trail", "entry": {
            "round": 3, "signal": "x", "attribution": "y",
            "next_hypothesis": "测试", "new_information": "z",
            "papers": ["not-an-id"]}})
    with pytest.raises(BridgeError, match="非空"):
        b.dispatch("paths.append", {"layer": "trail", "entry": {
            "round": 3, "signal": "x", "attribution": "y",
            "next_hypothesis": "测试", "new_information": "z",
            "papers": []}})
    b.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 3, "signal": "x", "attribution": "y",
        "next_hypothesis": "测试", "new_information": "z",
        "papers": ["http://arxiv.org/abs/2511.12490v1"]}})
    d = json.loads((tmp_path / "state" / "papers.json").read_text("utf-8"))
    assert "2511.12490" in d["papers"]
    assert d["papers"]["2511.12490"]["cited_rounds"] == [3]


def test_explored_papers_marks_exhausted(tmp_path):
    """explored 证伪条目带 papers → 台账标 exhausted。"""
    b = _mkbridge(tmp_path)
    b.dispatch("paths.append", {"layer": "explored", "entry": {
        "exploration": "skew 因子族", "evidence": "IC_IR=0.05, z=0.8",
        "root_cause": "OHLCV 无偏度信息", "papers": ["2108.05721"]}})
    d = json.loads((tmp_path / "state" / "papers.json").read_text("utf-8"))
    assert d["papers"]["2108.05721"]["exhausted"] is True


# ---- F5: 种子轮转 ----

def test_literature_seed_rotation():
    """literature 指令携带按检索数轮转的正交种子（v6：边际驱动触发）。"""
    base = dict(stop_kind=None, streak=9, pending_rejected=None,
                frozen=False, plateau=False, pass_unadmitted=0,
                accepted_n=0, agent_rounds=1, n_trials=1,
                family_marginal={"enough_data": True, "marginal": -0.15,
                                 "family_best": 0.8, "recent_best": 0.65})
    s1 = _pick_strategy(**base, lit_search_count=0)
    s2 = _pick_strategy(**base, lit_search_count=1)
    assert s1["type"] == "literature" and s2["type"] == "literature"
    assert s1["directive"] != s2["directive"]
    assert "种子查询" in s1["directive"] and "papers" in s1["directive"]
    s_wrap = _pick_strategy(**base, lit_search_count=14)
    assert s_wrap["directive"] == s1["directive"]
