# coding=utf-8
"""推进治理总闸回归（2026-09-19）——空转/停摆双治。

事故背景（09-19 03:00-10:00 实录）：死线武装时注入器连续简单上限整体
旁路 + query 可重推 → 排干的会话分钟级空转到死线；对偶病是同键去重把
非可重推指令永久卡死。修复全在引擎侧（注入器零改动）：
  - R6 里程碑边沿触发一次即消费（drive_bk.milestone）
  - R6.5 drained（机械库存清空）→ drain 指令（随机优先论文次之）
  - _govern_decide 总闸：drain 限发、零增长卡住补推、有界收摊
  - 收摊态（drained/stalled）可被任何新落账自动解除（非 finalize/test 锁）
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import (  # noqa: E402
    Bridge, _govern_decide, _pick_strategy, _timeout_rotate_strategy,
)
from dsh_factor_mining.state import (  # noqa: E402
    MINING_CONFIG, lane_drive_bk, lane_drive_bk_update,
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


# ---------------------------------------------------------------------------
# _pick_strategy：drain 分支 + 里程碑边沿触发
# ---------------------------------------------------------------------------
def _base_kwargs(**kw):
    d = dict(stop_kind=None, streak=0, pending_rejected=None, frozen=False,
             plateau=False, pass_unadmitted=0, accepted_n=0, agent_rounds=5,
             n_trials=10, lit_search_count=0, family_marginal=None,
             inspiration_reset=None, random_available=True,
             drained=False, milestone_fired=None)
    d.update(kw)
    return d


def test_pick_strategy_drain_branch():
    s = _pick_strategy(**_base_kwargs(drained=True))
    assert s["type"] == "drain"
    assert "随机因子" in s["directive"]
    # 非排干态回落 continue
    assert _pick_strategy(**_base_kwargs())["type"] == "continue"
    # 有组合库存（pass 未入册 ≥3）时 drain 判据侧应为 False——引擎不重复
    # 推荐 compose 之外的供数换向
    assert _pick_strategy(**_base_kwargs(pass_unadmitted=3))["type"] == "compose"


def test_pick_strategy_milestone_edge_trigger():
    # 39 未消费 → query
    s = _pick_strategy(**_base_kwargs(accepted_n=39))
    assert s["type"] == "query"
    # 39 已消费 → 不再 query（排干或 continue，绝不复读同一里程碑）
    s2 = _pick_strategy(**_base_kwargs(accepted_n=39, milestone_fired=39))
    assert s2["type"] != "query"
    # 跨入 42 → 新里程碑再发
    assert _pick_strategy(**_base_kwargs(accepted_n=42,
                                         milestone_fired=39))["type"] == "query"


# ---------------------------------------------------------------------------
# _govern_decide：drain 生命周期 / 卡住补推生命周期 / 守卫
# ---------------------------------------------------------------------------
CFG = {"drain_max_emissions": 3, "drain_repush_spacing_s": 900,
       "stuck_after_s": 1800}


def test_govern_growth_resets():
    bk = {"nudge": 2, "drain": 2, "growth_trials": 1, "growth_ts": 0.0}
    g = _govern_decide(bk, now=9999.0, growth=True, async_running=False,
                       chosen_type="continue", lane_trials=10,
                       cfg=CFG)
    assert g["action"] == "pass" and g.get("reset") is True


def test_govern_drain_lifecycle():
    t0 = 10000.0
    # 首发计数 1
    g = _govern_decide({}, now=t0, growth=False, async_running=False,
                       chosen_type="drain", lane_trials=10, cfg=CFG)
    assert g["action"] == "drain_emit" and g["count"] == 1
    bk = {"drain": 1, "last_key": g["key"], "last_ts": t0}
    # 间隔窗内 → 原键续供（注入器去重静默）
    g = _govern_decide(bk, now=t0 + 600, growth=False, async_running=False,
                       chosen_type="drain", lane_trials=10, cfg=CFG)
    assert g["action"] == "drain_hold" and g["key"] == bk["last_key"]
    # 过窗 → 计数 2；再过窗 → 3；第 4 次 → 收摊
    bk = {"drain": 1, "last_key": "drain:1:10", "last_ts": t0}
    g = _govern_decide(bk, now=t0 + 901, growth=False, async_running=False,
                       chosen_type="drain", lane_trials=10, cfg=CFG)
    assert g["action"] == "drain_emit" and g["count"] == 2
    # 预算耗尽但未到超时窗：原键续供静默（不再收摊）
    g = _govern_decide({"drain": 3, "last_key": "drain:3:10", "last_ts": t0},
                       now=t0 + 901, growth=False, async_running=False,
                       chosen_type="drain", lane_trials=10, cfg=CFG)
    assert g["action"] == "drain_hold" and g["key"] == "drain:3:10"
    # 预算耗尽 + 超时 → 换向接管（rotate，无终态）
    g = _govern_decide({"drain": 3, "last_key": "drain:3:10", "last_ts": t0,
                        "growth_ts": 0.0},
                       now=t0 + 99999, growth=False, async_running=False,
                       chosen_type="drain", lane_trials=10, cfg=CFG)
    assert g["action"] == "rotate_emit" and g["count"] == 1
    assert g["key"] == "rotate-timeout:1:10"


def test_govern_timeout_rotate_lifecycle():
    t0 = 10000.0
    # 零增长但未过窗 → pass
    g = _govern_decide({"growth_ts": t0}, now=t0 + 1700, growth=False,
                       async_running=False, chosen_type="continue",
                       lane_trials=10, cfg=CFG)
    assert g["action"] == "pass"
    # 过窗 → 换向 1
    g = _govern_decide({"growth_ts": t0}, now=t0 + 1801, growth=False,
                       async_running=False, chosen_type="continue",
                       lane_trials=10, cfg=CFG)
    assert g["action"] == "rotate_emit" and g["count"] == 1
    assert g["key"] == "rotate-timeout:1:10"
    # 间隔窗内 → 原键续供静默
    bk = {"growth_ts": t0, "timeout": 1, "last_key": "rotate-timeout:1:10",
          "last_ts": t0 + 1801}
    g = _govern_decide(bk, now=t0 + 1801 + 600, growth=False,
                       async_running=False, chosen_type="continue",
                       lane_trials=10, cfg=CFG)
    assert g["action"] == "rotate_hold" and g["key"] == "rotate-timeout:1:10"
    # 过窗 → 换向 2
    g = _govern_decide({"growth_ts": t0, "timeout": 1,
                        "last_key": "rotate-timeout:1:10",
                        "last_ts": t0 + 1801},
                       now=t0 + 1801 + 1801, growth=False, async_running=False,
                       chosen_type="rotate", lane_trials=10, cfg=CFG)
    assert g["action"] == "rotate_emit" and g["count"] == 2
    # 计数无上限（无终态：预定任务死线前永不静默）
    g = _govern_decide({"growth_ts": 0.0, "timeout": 50,
                        "last_key": "rotate-timeout:50:10", "last_ts": 0.0},
                       now=999999.0, growth=False, async_running=False,
                       chosen_type="continue", lane_trials=10, cfg=CFG)
    assert g["action"] == "rotate_emit" and g["count"] == 51
    # 在途评估守卫：过窗也不换向（长评估防误伤）
    g = _govern_decide({"growth_ts": t0}, now=t0 + 99999, growth=False,
                       async_running=True, chosen_type="continue",
                       lane_trials=10, cfg=CFG)
    assert g["action"] == "pass"


def test_timeout_rotate_strategy_shape():
    s = _timeout_rotate_strategy(2, 7, 1800)
    assert s["type"] == "rotate"
    assert s["key"] == "rotate-timeout:2:7"
    assert "factor_query_paths" in s["directive"]
    assert "回报进度" in s["directive"]  # 误报无害化
    assert "换一个方向" in s["directive"]
    assert "factor_random_generate" in s["directive"]  # 随机因子兜底（用户五点之二）


# ---------------------------------------------------------------------------
# 记账助手
# ---------------------------------------------------------------------------
def test_drive_bk_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        bk = lane_drive_bk_update(d, "glm", nudge=1, drain=2,
                                  last_key="drain:2:5:10", last_ts=1.0)
        assert bk["nudge"] == 1 and bk["drain"] == 2
        from dsh_factor_mining.state import read_mining_state
        st = read_mining_state(d)
        again = lane_drive_bk(st, "glm")
        assert again["last_key"] == "drain:2:5:10"
        # 另一线互不污染
        assert lane_drive_bk(st, "glm-farm-etf") == {}


# ---------------------------------------------------------------------------
# Bridge 集成：drain 指令真实出现在 loop 响应 + 里程碑消费落账
# ---------------------------------------------------------------------------
def test_loop_drain_and_milestone_integration(tmp_path):
    b = _make_bridge(tmp_path)
    loop = b._loop_directive()
    # 冷启动空 trail：无 pending、deepen 空（trail 空不是"不可算"）、
    # 无组合库存 → drained → drain 指令，governor 遥测在场
    assert loop["strategy"]["type"] in ("drain",), loop["strategy"]["type"]
    assert loop["governor"]["drained"] is True
    assert loop["governor"]["drain_emitted"] == 1
    assert loop["state"] == "running"
    # 再查一次（间隔窗内）：同键续供——注入器去重静默而非重发
    loop2 = b._loop_directive()
    assert loop2["strategy"]["type"] == "drain"
    assert loop2["strategy"]["key"] == loop["strategy"]["key"]
    assert loop2["governor"]["drain_emitted"] == 1
    # 里程碑消费路径：query 挂出即记账（accepted=0 时不触发——用记账直验）
    from dsh_factor_mining.state import read_mining_state
    (tmp_path / "b2").mkdir()
    b2 = _make_bridge(tmp_path / "b2")
    from dsh_factor_mining.state import lane_drive_bk_update
    # 模拟 accepted=3 未消费：直接调纯函数已被上面覆盖；这里验证
    # loop 集成中 milestone 记账不因异常断链（空轨迹下 drive_bk 可写）
    lb = b2._loop_directive()
    assert lb["governor"]["milestone_consumed"] is None  # 从未发过 query
    st = read_mining_state(str(tmp_path / "b2" / "state"))
    assert "drive_bk" in ((st.get("lanes") or {}).get(
        lb.get("lane") or "glm") or {})


def test_loop_recovery_on_growth(tmp_path):
    """超时换向接管与增长复位（无终态：永不 may_stop 静默）。"""
    b = _make_bridge(tmp_path)
    _ = b._loop_directive()
    lane = b._loop_directive().get("lane") or "glm"
    # drain 预算耗尽 + 超时（growth_ts 拨回远古）→ 换向接管，仍 running
    lane_drive_bk_update(b.state_root, lane, drain=3,
                         last_key="drain:3:0:0", last_ts=0.0, growth_ts=0.0)
    loop = b._loop_directive()
    assert loop["state"] == "running"          # 不再 may_stop 收摊
    assert loop["strategy"]["type"] == "rotate"
    assert loop["strategy"]["key"].startswith("rotate-timeout:1:")
    assert loop["governor"]["timeout_rotates"] == 1
    # 增长复位：快照前移 → 计数清零 → drain 重新可用
    lane_drive_bk_update(b.state_root, lane, growth_trials=-1,
                         growth_ts=0.0, timeout=0, drain=0)
    loop2 = b._loop_directive()
    assert loop2["state"] == "running"
    assert loop2["strategy"]["type"] == "drain"
    assert loop2["governor"]["drain_emitted"] == 1


def test_waiting_loop_narrative_does_not_disarm_governor(tmp_path):
    """生产复犯回归（09-19 实录）：等待环的一句话每轮写一条 trail（叙事
    回合涨、引擎试验不涨）——旧增长定义 (回合数,全局试验数) 被它喂成
    "永远在增长"，drain/nudge 从未触发（生产 drive_bk drain=0/nudge=0）。
    修正后：叙事不算增长，卡住判窗照常走完 → nudge 接管 → 同键静默。"""
    import json as _json
    b = _make_bridge(tmp_path)
    loop = b._loop_directive()
    lane = loop.get("lane") or "glm"
    # 模拟等待环：连写多条换措辞的 next_hypothesis（叙事轨迹），零引擎试验
    p = Path(b.state_root) / "trail.json"
    entries = (_json.loads(p.read_text(encoding="utf-8"))
               if p.exists() else [])
    for i in range(3):
        entries.append({"ts": f"2026-09-19T16:0{i}:00", "lane": lane,
                        "next_hypothesis": f"审计闭账维持，等待环 v3 不变（第{i}轮）"})
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    # 引擎试验数不变（叙事不算增长）→ 判窗走完后 nudge 接管
    lane_drive_bk_update(b.state_root, lane, growth_ts=time.time() - 2400)
    loop2 = b._loop_directive()
    assert loop2["strategy"]["type"] == "rotate", loop2["strategy"]["type"]
    assert loop2["strategy"]["key"].startswith("rotate-timeout:1:")
    assert loop2["governor"]["timeout_rotates"] == 1
    # 叙事回合继续涨（再写两条换措辞声明）→ 间隔窗内维持同键（注入器
    # 去重静默），绝不回到可重推的 continue
    for i in range(2):
        entries.append({"ts": f"2026-09-19T16:1{i}:00", "lane": lane,
                        "next_hypothesis": f"等待环 v3 静默保持（续{i}），死线 10:00"})
    p.write_text(_json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    loop3 = b._loop_directive()
    assert loop3["strategy"]["type"] == "rotate"
    assert loop3["strategy"]["key"] == loop2["strategy"]["key"]
    assert loop3["governor"]["timeout_rotates"] == 1


if __name__ == "__main__":
    fns = [test_pick_strategy_drain_branch,
           test_pick_strategy_milestone_edge_trigger,
           test_govern_growth_resets, test_govern_drain_lifecycle,
           test_govern_timeout_rotate_lifecycle,
           test_timeout_rotate_strategy_shape,
           test_drive_bk_roundtrip,
           test_loop_drain_and_milestone_integration,
           test_loop_recovery_on_growth,
           test_waiting_loop_narrative_does_not_disarm_governor]
    failures = []
    for fn in fns:
        try:
            fn(Path(tempfile.mkdtemp()) if "tmp_path" in fn.__code__.co_varnames else fn())
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e!r}")
    if failures:
        raise SystemExit(1)
    print("LOOP_GOVERNOR_TESTS PASS")
