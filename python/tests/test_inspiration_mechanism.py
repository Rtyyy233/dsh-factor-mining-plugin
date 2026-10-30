# coding=utf-8
"""灵感源二代回归（2026-09-22 PLAN-inspiration-mechanism：WS-2A mechanism
模式 / WS-2B trail_similarity 查重不查新 / WS-2C 概念碰撞卡 / WS-2D origin
五值枚举）。

D1 三模式共享声明配额 2/段；D2 三源确定性轮转（%3：0→lit、1→random、
2→mechanism）；D3 碰撞卡=机制策略内工具（弃权免费）；零脚手架——机制
假设内容零校验是红线。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import (  # noqa: E402
    Bridge,
    _inspiration_decl_match,
    _pick_strategy,
    _STRATEGY_DIRECTIVES,
)
from dsh_factor_mining import collision  # noqa: E402
from dsh_factor_mining.state import (  # noqa: E402
    arc_rounds_bump,
    inspiration_declare,
    lane_inspiration_mode,
    lane_mechanism_used,
    mechanism_emit_count,
    read_mining_state,
)

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""

# 与 BASELINE 规范化后逐行一致（注释/docstring 被指纹剥除）→ sim=1.0
NEAR_COPY_SOURCE = '''
# 完全不同的注释壳
import pandas as pd

def factor(env):
    # 注释行同样不参与指纹
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
'''

DIFFERENT_SOURCE = """
import numpy as np

def factor(env):
    c = np.asarray(env.c, dtype=float)
    x = np.tanh(c * 0.037) + 3.7
    return np.where(x > 0.5, -x, x * 0.25)
"""


def _make_bridge(root: Path) -> Bridge:
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
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


def _write_trail_entry(b: Bridge, nh: str, **extra) -> None:
    entry = {"round": 1, "signal": "momentum", "attribution": "IC_IR 弱",
             "next_hypothesis": nh, "new_information": "新增波动率维度"}
    entry.update(extra)
    b.dispatch("paths.append", {"layer": "trail", "entry": entry})


# ---- 1. 声明解析（WS-2A：mechanism 入枚举） ----

def test_decl_match_mechanism():
    assert _inspiration_decl_match(
        "[灵感重置:mechanism] 低流动性溢价族已平台，自己提新机制"
    ) == ("mechanism", "低流动性溢价族已平台，自己提新机制")
    assert _inspiration_decl_match(
        "[灵感重置：mechanism] 换自提机制"
    )[0] == "mechanism"                       # 全角冒号
    assert _inspiration_decl_match("[灵感重置:mechanism]") is None  # 空理由


# ---- 2. 声明受理：ACK + 强制 strategy=mechanism + 发射记账 ----

def test_mechanism_declaration_ack(tmp_path):
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "[灵感重置:mechanism] 反转族边际枯竭，自提新机制")
    loop = b._loop_directive()
    assert loop["strategy"]["type"] == "mechanism"
    assert "灵感重置（agent 声明）" in loop["strategy"]["why"]
    assert "零校验" not in loop["strategy"]["directive"]
    # 指令三句式：查重指引 + 碰撞卡出口
    assert "factor_journal_read" in loop["strategy"]["directive"]
    assert "trail_similarity" in loop["strategy"]["directive"]
    assert "collision-card" in loop["strategy"]["directive"]
    assert loop["pending_hypothesis"] is None
    ir = loop["inspiration_reset"]
    assert ir["acknowledged"] is True and ir["mode"] == "mechanism"
    assert ir["quota_left"] == 1 and ir["mechanism_available"] is False
    st = read_mining_state(tmp_path / "state")
    bucket = st["lanes"]["default"]
    # 声明扣共享配额 1；mechanism 发射同步记账（挂出即计，与 random 同构）
    assert bucket["inspiration_used"] == 1
    assert bucket["mechanism_used"] == 1 and st["mechanism_runs"] == 1
    # origin 模式窗开启
    assert bucket["inspiration_mode"] == "mechanism"


def test_directive_text_three_clauses():
    txt = _STRATEGY_DIRECTIVES["mechanism"]
    assert "查重" in txt and "trail_similarity" in txt
    assert "collision-card" in txt and "弃权" in txt


# ---- 3. 配额（D1 共享声明配额 + mechanism 独立发射配额） ----

def test_mechanism_quota_rejection_then_literature(tmp_path):
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "[灵感重置:mechanism] 第一次机制换源")
    assert b._loop_directive()["strategy"]["type"] == "mechanism"
    # 第二次 mechanism → 发射配额 1 已用，拒
    _write_trail_entry(b, "[灵感重置:mechanism] 再来一次机制")
    loop = b._loop_directive()
    assert loop["inspiration_reset"]["acknowledged"] is False
    assert "mechanism 策略本方向段已用" in loop["pending_rejected"]["reason"]
    # 改声明 literature → 共享声明配额还剩 1，受理
    _write_trail_entry(b, "[灵感重置:literature] 改走文献")
    loop = b._loop_directive()
    assert loop["inspiration_reset"]["acknowledged"] is True
    assert loop["strategy"]["type"] == "literature"


def test_shared_declaration_quota_exhaustion(tmp_path):
    """D1：三模式共享每方向段 2 次声明配额——第 3 次（任意模式）拒。"""
    root = tmp_path / "state"
    assert inspiration_declare(root, None, 1, "mechanism")["accepted"]
    assert inspiration_declare(root, None, 2, "random")["accepted"]
    third = inspiration_declare(root, None, 3, "literature")
    assert not third["accepted"] and "超限" in third["reason"]


def test_mechanism_emit_dedup_and_arc_reset(tmp_path):
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    mechanism_emit_count(root, None, 5)
    mechanism_emit_count(root, None, 5)          # 同轮去重
    assert lane_mechanism_used(read_mining_state(root), None) == 1
    assert lane_inspiration_mode(read_mining_state(root), None) is None
    # 发射已耗尽本段 mechanism 配额 → 同段声明被拒（声明与轮转共用）
    rejected = inspiration_declare(root, None, 6, "mechanism")
    assert not rejected["accepted"] and "mechanism 策略本方向段已用" in rejected["reason"]
    assert lane_inspiration_mode(read_mining_state(root), None) is None
    arc_rounds_bump(root, reset=True)            # 断链：配额+模式窗同清
    st = read_mining_state(root)
    bucket = st["lanes"]["default"]
    assert bucket["mechanism_used"] == 0 and bucket["inspiration_used"] == 0
    assert lane_inspiration_mode(st, None) is None
    assert inspiration_declare(root, None, 7, "mechanism")["accepted"]  # 新段重新可用
    assert lane_inspiration_mode(read_mining_state(root), None) == "mechanism"


# ---- 4. D2 三源轮转（纯函数层） ----

_FAM_DEAD = {"enough_data": True, "marginal": -0.20,
             "tail_marginal": {"enough_data": True, "marginal": -0.15}}


def _pick(**kw):
    base = dict(streak=0, pending_rejected=None, frozen=False, plateau=False,
                pass_unadmitted=0, accepted_n=0, agent_rounds=1, n_trials=1,
                family_marginal=_FAM_DEAD)
    base.update(kw)
    return _pick_strategy(**base)


def test_three_source_rotation_cycle():
    types = [_pick(lit_search_count=n, random_available=True,
                   mechanism_available=True)["type"] for n in range(6)]
    assert types == ["literature", "random", "mechanism",
                     "literature", "random", "mechanism"]


# ---- 5. WS-2C 概念碰撞卡（seed 可复现 + 日志链 + bridge 分发） ----

def test_collision_card_seed_reproducible_and_logged(tmp_path):
    root = tmp_path / "state"
    (root / "journal" / "lane-a").mkdir(parents=True)
    (root / "journal" / "lane-a" / "live.md").write_text(
        "# journal INDEX\n\n## J1 registered 反转族实验\n"
        + "低流动性溢价在宽基 ETF 截面持续兑现，量能安静度与开盘衰减交互。\n" * 8,
        encoding="utf-8")
    c1 = collision.draw_card(root, seed=777)
    c2 = collision.draw_card(root, seed=777)
    assert c1["card_id"] == c2["card_id"]
    assert c1["stimuli"] == c2["stimuli"]
    assert all("text" in s and "origin" in s and "context" in s
               for s in c1["stimuli"])
    assert any(s["origin"] == "journal-vocab" for s in c1["stimuli"])
    # 日志链：两次抽卡各落一条，card_id 可被 origin 校验读回
    assert c1["card_id"] in collision.known_card_ids(root)
    log = (root / "collision_log.jsonl").read_text(encoding="utf-8").strip()
    assert len(log.splitlines()) == 2
    assert "弃权" in c1["note"]


def test_collision_card_bounds_and_empty_vocab(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    empty = collision.draw_card(root, seed=1)
    assert "error" in empty                      # 词表为空如实报错不硬造
    (root / "journal" / "x").mkdir(parents=True)
    (root / "journal" / "x" / "live.md").write_text(
        "动量反转流动性溢价\n" * 10, encoding="utf-8")
    c3 = collision.draw_card(root, seed=9, k=3)
    assert 1 <= len(c3["stimuli"]) <= 3


def test_collision_card_via_bridge(tmp_path):
    b = _make_bridge(tmp_path)
    (tmp_path / "state" / "journal" / "ln").mkdir(parents=True)
    (tmp_path / "state" / "journal" / "ln" / "live.md").write_text(
        "## J1 active 量价交互\n" + "开盘缺口与成交量秩序的合取结构。\n" * 8,
        encoding="utf-8")
    out = b.dispatch("factor.random_generate",
                     {"envId": "primary", "mode": "collision-card", "seed": 5})
    assert out["card_id"].startswith("cc-")
    assert collision.known_card_ids(tmp_path / "state")
    # 抽卡不烧挖掘活动（act_n 不动——卡不是评估）
    st = read_mining_state(tmp_path / "state")
    assert int((st.get("lanes") or {}).get("default", {}).get("act_n", 0)) == 0


# ---- 6. WS-2B trail_similarity（查重不查新，只读） ----

def test_trail_similarity_hits_and_readonly(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": BASELINE_SOURCE,
                                   "stage": "development"})
    te = tmp_path / "state" / "trail_engine.json"
    before = te.read_text(encoding="utf-8")
    st0 = read_mining_state(tmp_path / "state")
    act0 = int((st0.get("lanes") or {}).get("default", {}).get("act_n", 0))
    # 近拷贝（规范化后同码）→ 高相似命中
    out = b.dispatch("paths.query", {"layer": "trail_similarity",
                                     "query": "-", "source": NEAR_COPY_SOURCE})
    assert out["best_sim"] > 0.5
    assert out["hits"] and out["hits"][0]["sim"] == out["best_sim"]
    assert "查重不查新" in out["note"]
    # 异构构造相似度低于近拷贝
    out2 = b.dispatch("paths.query", {"layer": "trail_similarity",
                                      "query": "-", "source": DIFFERENT_SOURCE})
    assert out2["best_sim"] < out["best_sim"]
    # 只读性：查询前后 trail_engine 字节不变、挖掘活动计数不前进
    assert te.read_text(encoding="utf-8") == before
    st1 = read_mining_state(tmp_path / "state")
    act1 = int((st1.get("lanes") or {}).get("default", {}).get("act_n", 0))
    assert act1 == act0


def test_trail_similarity_requires_source(tmp_path):
    b = _make_bridge(tmp_path)
    from dsh_factor_mining.bridge import BridgeError
    with pytest.raises(BridgeError):
        b.dispatch("paths.query", {"layer": "trail_similarity",
                                   "query": "-", "source": "  "})


# ---- 7. WS-2D origin 五值（mechanism 模式窗 / collision 卡校验） ----

def test_origin_mechanism_window_and_collision(tmp_path):
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    (state / "journal" / "ln").mkdir(parents=True)
    (state / "journal" / "ln" / "live.md").write_text(
        "## J1 active 机制假设窗\n" + "自提机制推导中的隔夜跳空构造。\n" * 8,
        encoding="utf-8")
    # 机制声明 → status/loop ACK（模式窗在此落账）→ 试验 origin=mechanism
    _write_trail_entry(b, "[灵感重置:mechanism] 反转族枯竭，自提隔夜跳空机制")
    assert b._loop_directive()["strategy"]["type"] == "mechanism"
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": BASELINE_SOURCE,
                                   "stage": "development"})
    entries = json.loads((state / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries[-1]["origin"] == "mechanism"
    assert entries[-1]["authored"] is True
    # 碰撞卡连接：trail 条目带合法 card_id → origin=collision（新试验）
    card = b.dispatch("factor.random_generate",
                      {"envId": "primary", "mode": "collision-card", "seed": 11})
    _write_trail_entry(b, "换波动率维度构造新假设",
                       collision_card=card["card_id"])
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": DIFFERENT_SOURCE,
                                   "stage": "development"})
    entries = json.loads((state / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries[-1]["origin"] == "collision"
    # 伪 card_id（日志中不存在）→ 不享受 collision 标注，回落
    _write_trail_entry(b, "又一个新假设", collision_card="cc-0000000000")
    src3 = DIFFERENT_SOURCE.replace("0.25", "0.26")
    b.dispatch("factor.evaluate", {"envId": "primary", "source": src3,
                                   "stage": "development"})
    entries = json.loads((state / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries[-1]["origin"] in ("hypothesis", "mechanism")
