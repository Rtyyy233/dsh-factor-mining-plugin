# coding=utf-8
"""灵感源自主切换回归（2026-09-01 规划书 PLAN-inspiration-source-autonomy：
WS-A random 策略 / WS-B 声明通道 / WS-C origin 标注；D1-D4 拍板值锁定）。

D1 声明即切（限频兜底而非判据兜底）；D2 random 同时走声明与 R1 确定性
轮转（lit_search_count % 3 == 2）；D3 每方向段声明 ≤2、random ≤1、
origin 三值枚举入账；D4 literature 冷启动=断链+清 pending+papers 必填。
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
    BridgeError,
    _inspiration_decl_match,
    _pick_strategy,
)
from dsh_factor_mining.state import (  # noqa: E402
    arc_rounds_bump,
    inspiration_declare,
    lane_random_used,
    random_emit_count,
    read_mining_state,
)

BASELINE_SOURCE = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
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


def _write_trail_entry(b: Bridge, nh: str, papers=None) -> None:
    entry = {
        "round": 1, "signal": "momentum", "attribution": "IC_IR 弱",
        "next_hypothesis": nh, "new_information": "新增波动率维度",
    }
    if papers is not None:
        entry["papers"] = papers
    b.dispatch("paths.append", {"layer": "trail", "entry": entry})


# ---- 1. 声明解析（WS-B：前缀语法/全角冒号/空理由/非法模式） ----

def test_decl_match_forms():
    assert _inspiration_decl_match(
        "[灵感重置:literature] 动量族已证伪，从文献冷启动"
    ) == ("literature", "动量族已证伪，从文献冷启动")
    assert _inspiration_decl_match(
        "[灵感重置：random] 假设源枯竭"
    ) == ("random", "假设源枯竭")          # 全角冒号
    assert _inspiration_decl_match(
        "[灵感重置:random]\n多行理由\n第二行"
    )[0] == "random"                        # DOTALL：多行理由
    assert _inspiration_decl_match("[灵感重置:random]") is None      # 空理由
    assert _inspiration_decl_match("[灵感重置:random]   ") is None   # 空白理由
    assert _inspiration_decl_match("[灵感重置:gpt] 理由") is None    # 模式不在枚举
    assert _inspiration_decl_match("换高阶矩维度") is None            # 无前缀
    assert _inspiration_decl_match("") is None
    assert _inspiration_decl_match(None) is None


# ---- 2. 写入口：声明旁路停笔拒收（判定序在 surrender 之前） ----

def test_write_bypasses_surrender(tmp_path):
    b = _make_bridge(tmp_path)
    # 普通停笔宣言照旧拒收
    with pytest.raises(BridgeError):
        _write_trail_entry(b, "因子空间已穷尽，本次探索结束")
    # 声明的理由里带停笔措辞不连坐——换源宣言不是停笔宣言
    _write_trail_entry(
        b, "[灵感重置:literature] 动量族已穷尽且证伪，从文献冷启动")
    trail = json.loads((tmp_path / "state" / "trail.json").read_text(
        encoding="utf-8"))
    assert len(trail) == 1
    # 非法模式 + 停笔词 → 不享受旁路（decl 判 None 后走 surrender 拒收）
    with pytest.raises(BridgeError):
        _write_trail_entry(b, "[灵感重置:穷尽] 伪声明")


# ---- 3. 声明受理：ACK + 强制策略 + pending 作废 + 回执（D1/D4） ----

def test_declaration_ack_forces_strategy(tmp_path):
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "[灵感重置:literature] 动量族边际枯竭，从文献重启")
    loop = b._loop_directive()
    assert loop["strategy"]["type"] == "literature"
    assert "灵感重置（agent 声明）" in loop["strategy"]["why"]
    # D4：papers 必填写进指令；pending 不回显
    assert "papers 字段必填" in loop["strategy"]["directive"]
    assert loop["pending_hypothesis"] is None
    assert "灵感重置已确认" in loop["obligation"]
    ir = loop["inspiration_reset"]
    assert ir["acknowledged"] is True and ir["mode"] == "literature"
    assert ir["quota_left"] == 1 and ir["random_available"] is True
    # 同轮重复观察（status/evaluate 多次调用 loop）不重复扣额
    b._loop_directive()
    st = read_mining_state(tmp_path / "state")
    assert st["lanes"]["default"]["inspiration_used"] == 1


def test_declaration_random_mode(tmp_path):
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "[灵感重置:random] 文献两轮未破局，换无先验探索")
    loop = b._loop_directive()
    assert loop["strategy"]["type"] == "random"
    assert "origin=random" in loop["strategy"]["directive"]
    # random 发射记账（声明与轮转共用配额）
    st = read_mining_state(tmp_path / "state")
    assert st["lanes"]["default"]["random_used"] == 1
    assert st["random_runs"] == 1
    assert loop["inspiration_reset"]["random_available"] is False


# ---- 4. 限频（D3：每方向段声明 ≤2；第 3 次降级 pending_rejected） ----

def test_declaration_quota_rejection(tmp_path):
    b = _make_bridge(tmp_path)
    for i, mode in ((1, "literature"), (2, "random")):
        _write_trail_entry(b, f"[灵感重置:{mode}] 第{i}次换源：边际枯竭")
        loop = b._loop_directive()
        assert loop["inspiration_reset"]["acknowledged"] is True, (i, loop)
    # 第 3 次（新叙事轮）→ 超限拒收，降级 pending_rejected，策略回常规表
    _write_trail_entry(b, "[灵感重置:literature] 第三次还想换")
    loop = b._loop_directive()
    assert loop["inspiration_reset"]["acknowledged"] is False
    assert "超限" in loop["pending_rejected"]["reason"]
    assert loop["strategy"]["type"] == "rotate"   # R4：pending_rejected → rotate


def test_random_quota_shared_by_declaration(tmp_path):
    """random 配额 1/1：声明发射过一次后，第二次 random 声明被拒——
    但 literature 声明额度独立（inspiration 配额 2 里还剩 1）。"""
    b = _make_bridge(tmp_path)
    _write_trail_entry(b, "[灵感重置:random] 假设源枯竭")
    assert b._loop_directive()["strategy"]["type"] == "random"
    _write_trail_entry(b, "[灵感重置:random] 再来一次随机")
    loop = b._loop_directive()
    assert loop["inspiration_reset"]["acknowledged"] is False
    assert "random 策略本方向段已用" in loop["pending_rejected"]["reason"]
    _write_trail_entry(b, "[灵感重置:literature] 改走文献")
    loop = b._loop_directive()
    assert loop["inspiration_reset"]["acknowledged"] is True
    assert loop["strategy"]["type"] == "literature"


# ---- 5. 断链重置配额（D3：配额与 arc 同一方向段生命周期） ----

def test_chain_break_resets_quota(tmp_path):
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    assert inspiration_declare(root, None, 1, "random")["accepted"]
    random_emit_count(root, None, 1)
    arc_rounds_bump(root, reset=True)          # 机械换向事件
    st = read_mining_state(root)
    bucket = st["lanes"]["default"]
    assert bucket["inspiration_used"] == 0 and bucket["random_used"] == 0
    assert inspiration_declare(root, None, 2, "random")["accepted"]  # 新段重新可用


def test_emit_and_declare_round_dedup(tmp_path):
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    inspiration_declare(root, None, 5, "literature")
    again = inspiration_declare(root, None, 5, "literature")
    assert again["accepted"] and again["inspiration_used"] == 1
    random_emit_count(root, None, 7)
    random_emit_count(root, None, 7)           # 同轮第二次不重复计
    assert lane_random_used(read_mining_state(root), None) == 1


# ---- 6. R1 确定性轮转（D2：lit_search_count % 3 == 2 → random） ----

_FAM_DEAD = {"enough_data": True, "marginal": -0.20,
             "tail_marginal": {"enough_data": True, "marginal": -0.15}}


def _pick(**kw):
    base = dict(streak=0, pending_rejected=None, frozen=False, plateau=False,
                pass_unadmitted=0, accepted_n=0, agent_rounds=1, n_trials=1,
                family_marginal=_FAM_DEAD)
    base.update(kw)
    return _pick_strategy(**base)


def test_r1_rotation_deterministic():
    # 2026-09-22 三源确定性轮转：lit_search_count % 3——1→random、
    # 2→mechanism、0→literature；配额尽的轮转位回落 literature
    assert _pick(lit_search_count=1, random_available=True,
                 mechanism_available=True)["type"] == "random"
    assert _pick(lit_search_count=2, random_available=True,
                 mechanism_available=True)["type"] == "mechanism"
    assert _pick(lit_search_count=3, random_available=True,
                 mechanism_available=True)["type"] == "literature"
    assert _pick(lit_search_count=4, random_available=True,
                 mechanism_available=True)["type"] == "random"
    assert _pick(lit_search_count=5, random_available=True,
                 mechanism_available=True)["type"] == "mechanism"
    # 轮转位配额尽 → 回落 literature 且如实说明
    s = _pick(lit_search_count=1, random_available=False,
              mechanism_available=True)
    assert s["type"] == "literature" and "random 配额已用" in s["why"]
    s0 = _pick(lit_search_count=2, random_available=True,
               mechanism_available=False)
    assert s0["type"] == "literature" and "mechanism 配额已用" in s0["why"]
    # 原有 R1 文案口径不漂移（literature 位）
    s2 = _pick(lit_search_count=3, random_available=True,
               mechanism_available=True)
    assert "内生假设源枯竭，必须文献注入" in s2["why"]
    # direction_budget 强制路径同样轮转
    s3 = _pick(stop_kind="direction_budget", lit_search_count=1,
               random_available=True, mechanism_available=True)
    assert s3["type"] == "random"
    assert "断链后预算自动重置" in s3["directive"]   # direction_budget 后缀保留
    # 声明强制路径优先于 direction_budget（D1/D4）
    s4 = _pick(stop_kind="direction_budget", lit_search_count=3,
               inspiration_reset={"mode": "random", "reason": "枯竭"})
    assert s4["type"] == "random" and "灵感重置（agent 声明）" in s4["why"]
    # mechanism 声明同样强制
    s4m = _pick(stop_kind="direction_budget",
                inspiration_reset={"mode": "mechanism", "reason": "枯竭"})
    assert s4m["type"] == "mechanism" and "灵感重置（agent 声明）" in s4m["why"]
    # random 指令不依赖配额参数（声明受理时已查）：
    s5 = _pick(inspiration_reset={"mode": "random", "reason": "枯竭"},
               random_available=False)
    assert s5["type"] == "random"
    # mechanism 指令同理
    s5m = _pick(inspiration_reset={"mode": "mechanism", "reason": "枯竭"},
                mechanism_available=False)
    assert s5m["type"] == "mechanism"


# ---- 7. origin 标注（WS-C：random > literature > hypothesis） ----

def test_origin_annotation(tmp_path):
    b = _make_bridge(tmp_path)
    state = tmp_path / "state"
    # 文献驱动：叙事条目带 papers → 本线后续试验 origin=literature
    _write_trail_entry(b, "迁移论文 2108.05721 的构造思路",
                       papers=["2108.05721"])
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": BASELINE_SOURCE,
                                   "stage": "development"})
    entries = json.loads((state / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries[-1]["origin"] == "literature"

    # 随机幸存：explore 返回原文直进管线 → origin=random（覆盖文献上下文）
    exp = b.dispatch("factor.random_generate", {"envId": "primary",
                                                "mode": "explore",
                                                "n": 6, "top_k": 2})
    assert exp["top"], "explore 至少应有一个幸存者"
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": exp["top"][0]["source"],
                                   "stage": "development"})
    entries = json.loads((state / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries[-1]["origin"] == "random"

    # 假设驱动：无 papers 的叙事条目 → hypothesis（缺省）
    _write_trail_entry(b, "换波动率维度构造新假设")
    src2 = BASELINE_SOURCE.replace("shift(20)", "shift(10)")
    b.dispatch("factor.evaluate", {"envId": "primary", "source": src2,
                                   "stage": "development"})
    entries = json.loads((state / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries[-1]["origin"] == "hypothesis"


def test_origin_in_trail_summary_projection(tmp_path):
    b = _make_bridge(tmp_path)
    b.dispatch("factor.evaluate", {"envId": "primary",
                                   "source": BASELINE_SOURCE,
                                   "stage": "development"})
    summ = b.dispatch("state.trail_summary", {})
    last = summ["last_engine_trail"][-1]
    assert last["origin"] == "hypothesis"   # 紧凑投影透传 + 缺省值


def test_registry_entry_carries_origin(tmp_path):
    b = _make_bridge(tmp_path)
    # null 校准先行（接受门需要 pool_std；与 test_accepted_is_not_a_stop 同构）
    b.dispatch("factor.random_generate", {"envId": "primary",
                                          "mode": "null-calibration", "n": 5})
    _write_trail_entry(b, "迁移论文思路", papers=["2108.05721"])
    sub = b.dispatch("registry.submit", {
        "name": "lit_factor_a", "signal": "momentum",
        "source": BASELINE_SOURCE,
        "diagnosis": {"ic_ir_train": 0.25, "ic_n_train": 50,
                      "column_perm_train": {"z": 4.0, "p": 0.0001},
                      "beta_exposure": 0.1,
                      "deflated_train": {"p": 0.001, "n_trials": 1,
                                         "sr_hat": 0.5, "skew": 0.0,
                                         "kurt": 3.0, "n_obs": 60}}})
    assert sub["accepted"] is True
    assert sub["entry"]["origin"] == "literature"
