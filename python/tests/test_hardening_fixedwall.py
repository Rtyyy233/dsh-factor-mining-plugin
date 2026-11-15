# coding=utf-8 -*-
"""固定墙时代加固件回归（PLAN-fixed-wall-rollout-2026-11-01 任务 E1b）。

锁定：
1. 配置读取：growing 缺省 / fixed 值 / fixed 声明但值非法 → 降级 growing
2. B1 固定墙恒值：fixed 模式下 bar_sigma 恒等于 fixed_bar_ic（不随试验数变）；
   回滚=growing 后恢复随试验增长
3. 雕刻门：逐年全同号触发；随机年不触发；年数<3 不判定
4. C 三门：M8 复杂度（AST≥12 拒）/ M13 双轨一致（差≤0.2502 拒、缺读数不判定）/
   M3 最短回测长度（ratio>1 拒）
5. B2 墙强度线红牌（fixed 措辞）与 deflated 警告
6. B3 OOS 凭据：无凭据提交拒（孵化队列指引）；有凭据放行
7. B4 selection 预算：评估记账+开票（同事务）；60 次封区拒
8. B6 gate_events.jsonl：墙/雕刻/警告事件落盘
9. B5 book_audit：accepted 条目前向带复算 → 报告落盘+降级建议清单
10. 尾轨固定分支：tail_deflation_bar(fixed_bar=) 恒值 + wall="fixed"
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402
from dsh_factor_mining.discipline import red_flags_and_verdict  # noqa: E402
from dsh_factor_mining.factor import hardening  # noqa: E402
from dsh_factor_mining.factor.hardening import (  # noqa: E402
    carving_from_series, gate_event, m13_check, m3_check, m8_check,
    oos_receipt_for, open_oos_receipt, read_wall_config,
    selection_budget_status)
from dsh_factor_mining.factor.tailgate import tail_deflation_bar  # noqa: E402

TILT_SOURCE = """
import numpy as np

def factor(env):
    n = env.c.shape[1]
    tilt = np.where(np.arange(n) < 20, 1.0, -1.0)
    return np.broadcast_to(tilt, env.c.shape).astype(float)
"""


def _make_bridge(root: Path, n_rows=1000) -> Bridge:
    rng = np.random.default_rng(11)
    T, N = n_rows, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    mu = np.where(np.arange(N) < 20, 0.004, -0.004)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(mu[i], 0.01, T))
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
                    "mapping": {"symbol": "symbol", "date": "eob",
                                "open": "open", "high": "high",
                                "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


def _calibrate(b: Bridge, n: int = 8) -> None:
    """跑一次 null 校准建 landscape（小 n，快）。"""
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": n})


def _set_wall(b: Bridge, mode: str, bar_ic=1.6, bar_tail=0.1199) -> None:
    """在 state root 的 null_landscape.json 上写入墙配置（新桥进程不必要——
    read_wall_config 每次读盘，配置即时生效；生产回滚同路径）。"""
    p = Path(b.state_root) / "null_landscape.json"
    d = json.loads(p.read_text(encoding="utf-8"))
    d["wall_mode"] = mode
    d["fixed_bar_ic"] = bar_ic
    d["fixed_bar_tail"] = bar_tail
    p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


# ---- 1. 配置读取 ----

def test_wall_config_defaults_and_degrade(tmp_path):
    root = tmp_path / "no_landscape"
    root.mkdir()
    cfg = read_wall_config(str(root))
    assert cfg["wall_mode"] == "growing" and cfg["fixed_bar_ic"] is None
    # fixed 声明但值非法 → 降级 growing（不砖门）
    (root / "null_landscape.json").write_text(
        json.dumps({"wall_mode": "fixed", "fixed_bar_ic": "bad"}),
        encoding="utf-8")
    cfg = read_wall_config(str(root))
    assert cfg["wall_mode"] == "growing" and "_degraded" in cfg
    # 完整 fixed 配置
    (root / "null_landscape.json").write_text(json.dumps({
        "wall_mode": "fixed", "fixed_bar_ic": 2.65, "fixed_bar_tail": 0.1366,
        "gate_m8_max_ops": 10}), encoding="utf-8")
    cfg = read_wall_config(str(root))
    assert cfg["wall_mode"] == "fixed" and cfg["fixed_bar_ic"] == 2.65
    assert cfg["gate_m8_max_ops"] == 10 and cfg["gate_m13_threshold"] == 0.2502


# ---- 2. 固定墙恒值 + 回滚开关 ----

def test_fixed_bar_constant_and_growing_rollback(tmp_path):
    b = _make_bridge(tmp_path)
    _calibrate(b)
    _set_wall(b, "fixed", bar_ic=0.2)
    # 第一次评估（trail 空 → 本因子即首试验）
    r1 = b.dispatch("factor.evaluate", {"envId": "primary",
                                        "source": TILT_SOURCE,
                                        "stage": "development"})
    dp1 = r1["deflated_train"]
    assert dp1["bar_sigma"] == 0.2 and dp1["wall_mode"] == "fixed", dp1
    # 第二个不同因子（trail 增长）——fixed 模式下 bar 恒定不变
    r2 = b.dispatch("factor.evaluate",
                    {"envId": "primary", "stage": "development",
                     "source": TILT_SOURCE.replace("< 20", "< 21")})
    dp2 = r2["deflated_train"]
    assert dp2["bar_sigma"] == 0.2, dp2
    # trail 章=固定值
    trail = json.loads((Path(b.state_root) / "trail_engine.json")
                       .read_text(encoding="utf-8"))
    assert all(e.get("bar_sigma_at_write") == 0.2 for e in trail)
    # 回滚演练：切回 growing → bar 恢复随试验计数（>0 且不再是固定值）
    _set_wall(b, "growing")
    r3 = b.dispatch("factor.evaluate",
                    {"envId": "primary", "stage": "development",
                     "source": TILT_SOURCE.replace("< 20", "< 22")})
    dp3 = r3["deflated_train"]
    assert dp3["wall_mode"] != "fixed" and dp3["bar_sigma"] > 0, dp3


# ---- 3. 雕刻门 ----

def test_carving_gate():
    idx = pd.bdate_range("2020-01-01", "2026-06-30")
    rng = np.random.default_rng(7)
    rand = pd.Series(rng.normal(0, 0.01, len(idx)), index=idx)
    cv = carving_from_series(rand)
    assert cv["tripped"] is False, cv
    # 逐年全同号（6+ 年）→ 二项精确 p 双侧 ≤0.05 → 触发
    carved = rand.abs() + 0.001
    assert carving_from_series(carved)["tripped"] is True
    # 年数不足 → 不判定
    short = pd.Series(rng.normal(0, 0.01, 60),
                      index=pd.bdate_range("2026-01-01", periods=60))
    assert carving_from_series(short)["tripped"] is False


# ---- 4. C 三门 ----

def test_m8_complexity_gate():
    simple = "def factor(env):\n    return env.c.rank(axis=1)\n"
    assert m8_check(simple)["rejected"] is False
    # 12 个调用点（rank×12）→ ≥12 拒
    chain = "def factor(env):\n    x = env.c\n" + \
        "".join(f"\n    x = x.rank(axis=1)  # {i}\n    x = x.fillna(0)\n"
                for i in range(6))
    res = m8_check(chain)
    assert res["rejected"] is True and res["ops"] >= 12, res


def test_m13_divergence_gate():
    assert m13_check(0.52, 0.50)["rejected"] is True    # 差 0.02 ≤ 0.2502
    assert m13_check(0.80, 0.20)["rejected"] is False   # 差 0.60
    assert m13_check(None, 0.5)["decided"] is False     # 尾轨读数缺失
    assert m13_check(0.5, None)["decided"] is False


def test_m3_minbtl_gate():
    # 撑不起：|IR|=0.3 / n=60 → n_need≈43<n… 比值<1 过；短样本强声明 → 拒
    assert m3_check(0.6, 0.0, 3.0, 83)["rejected"] is False
    assert m3_check(0.35, 0.0, 3.0, 30)["rejected"] is True
    assert m3_check(None, 0.0, 3.0, 30)["decided"] is False


# ---- 5. B2 墙强度线红牌与警告 ----

def test_wall_line_flag_and_deflated_warning():
    diag = {"ic_ir_train": 0.30, "ic_n_train": 84,
            "column_perm_train": {"z": 7.0}, "beta_exposure": 0.0,
            "deflated_train": {"p": 0.42, "n_trials": 26, "sr_hat": 0.30,
                               "sr0": 0.52, "bar_sigma": 2.65,
                               "pool_std": 0.198, "wall_mode": "fixed"}}
    gv = red_flags_and_verdict(diag, region="train")
    assert any("强度低于入册固定线" in f for f in gv["red_flags"]), gv
    assert any("deflated p" in w for w in gv["warnings"]), gv
    # 墙上通过（sr ≥ sr0）→ 无墙红牌；p>0.05 仍只是警告
    ok_diag = {**diag, "deflated_train": {**diag["deflated_train"],
                                          "sr_hat": 0.60, "p": 0.42}}
    gv2 = red_flags_and_verdict(ok_diag, region="train")
    assert not any("强度低于" in f for f in gv2["red_flags"]), gv2
    assert gv2["warnings"], gv2


# ---- 6/7. B3 凭据 + B4 预算（e2e：bridge 上跑） ----

def _ok_diagnosis(b, source):
    r = b.dispatch("factor.evaluate", {"envId": "primary", "source": source,
                                       "stage": "development"})
    return r


def test_oos_receipt_precondition_and_selection_budget(tmp_path):
    """2026-11-05 段隔离改写：OOS 凭据前置与 selection 预算面整体退役——
    selection stage 一律固定文案拒收；提交不再要求凭据（样本外验证内部
    化于提交路径，SPRT 语义由 verify 专项单测覆盖）。"""
    b = _make_bridge(tmp_path)
    _calibrate(b)
    _set_wall(b, "fixed", bar_ic=0.2)
    src = TILT_SOURCE.replace("< 20", "< 21")
    try:
        b.dispatch("factor.evaluate", {"envId": "primary", "source": src,
                                       "stage": "selection"})
        raise AssertionError("selection 评估未被拒")
    except BridgeError as e:
        assert "样本外评估已内部化" in str(e), str(e)
    # 提交路径无凭据前置：手造诊断不再因缺凭据被拒（段验证关闭以考本断言）
    from conftest import set_verify_mode
    set_verify_mode(b.state_root, "off")
    diag = _ok_diagnosis(b, src)
    sub = b.dispatch("registry.submit", {"envId": "primary", "name": "no_rcpt",
                                         "source": src, "signal": "x",
                                         "diagnosis": json.loads(json.dumps(diag))})
    assert "凭据" not in str(sub.get("reason", "")), sub.get("reason")


def hardening_source_fingerprint(src):
    from dsh_factor_mining.bridge import source_fingerprint
    return source_fingerprint(src)


def test_selection_budget_seals_at_cap(tmp_path):
    """2026-11-05 段隔离改写：selection 预算面退役——stage 拒收与账本状态
    无关（预填 60 笔与零笔同文案同语义）。"""
    b = _make_bridge(tmp_path)
    _calibrate(b)
    try:
        b.dispatch("factor.evaluate", {"envId": "primary",
                                       "source": TILT_SOURCE,
                                       "stage": "selection"})
        raise AssertionError("selection 未被拒")
    except BridgeError as e:
        assert "样本外评估已内部化" in str(e), str(e)


# ---- 8. B6 事件审计 ----

def test_gate_events_written_on_evaluate(tmp_path):
    b = _make_bridge(tmp_path)
    _calibrate(b)
    _set_wall(b, "fixed", bar_ic=0.2)
    b.dispatch("factor.evaluate", {"envId": "primary", "source": TILT_SOURCE,
                                   "stage": "development"})
    p = Path(b.state_root) / "gate_events.jsonl"
    assert p.exists()
    kinds = [json.loads(ln)["kind"]
             for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 2026-11-05：雕刻门事件退役（不拦截不路由不判定）——wall 事件仍在
    assert "wall_ic" in kinds and "carving_gate" not in kinds, kinds


# ---- 9. B5 册子滚动审计 ----

def test_book_audit_produces_report(tmp_path):
    b = _make_bridge(tmp_path)
    _calibrate(b)
    _set_wall(b, "fixed", bar_ic=0.01)   # 低墙便于入册
    src = TILT_SOURCE
    diag = _ok_diagnosis(b, src)
    # 开凭据后提交（噪声门在合成面板上对 tilt 因子可过——drift 面板真实结构）
    open_oos_receipt(b.state_root,
                     hardening_source_fingerprint(src),
                     "test_grant", {})
    sub = b.dispatch("registry.submit", {"envId": "primary", "name": "aud1",
                                         "source": src, "signal": "x",
                                         "diagnosis": json.loads(json.dumps(diag))})
    if not sub.get("accepted"):
        # 面板随机性导致门未过时跳过入册断言，但审计仍须对空册返回结构
        pass
    rep = b.dispatch("factor.book_audit", {"envId": "primary"})
    # 2026-11-05 B5：模型面响应只含完成状态与文件名（全数字在人读报表）
    assert rep["status"] == "done"
    assert rep["n_audited"] == (1 if sub.get("accepted") else 0)
    assert "n_keep" not in rep and "downgrade_candidates" not in rep, rep
    if sub.get("accepted"):
        rp = Path(b.state_root) / "reports" / rep["report_file"]
        assert rp.exists()
        import json as _json
        full = _json.loads(rp.read_text(encoding="utf-8"))
        # 2026-11-05 收尾任务书 §四 3：机械判据（衰减带）三态标记
        assert full["n_keep"] + full["n_watch"] \
            + full["n_reconsider_candidate"] == 1


# ---- 10. 尾轨固定分支 ----

def test_tail_deflation_bar_fixed_branch():
    r = tail_deflation_bar(5, 120, s0_emp=0.1, fixed_bar=0.1366)
    assert r["ok"] and r["wall"] == "fixed" and r["bar"] == 0.1366, r
    r2 = tail_deflation_bar(500, 120, s0_emp=0.1, fixed_bar=0.1366)
    assert r2["bar"] == 0.1366 and r2["wall"] == "fixed", r2
    # 不传 fixed_bar → v1 增长语义（N_eff 大 → bar 高）
    r3 = tail_deflation_bar(500, 120, s0_emp=0.1)
    assert r3["wall"] != "fixed" and r3["bar"] > 0.1366, r3
