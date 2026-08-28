# coding=utf-8
"""策略层 dev 区域硬切回归（2026-08-28 修复：test 窗窥视）。

背景：worker.run_request 曾把全面板直通 development/walk_forward/submit
——WF 末折即 test 窗、dev 试验与 submit 门数字全含 [sel_end, T)，
test_lock 形同虚设。修复 = 因子层 2026-08-18 同型修复（bridge.
_factor_walk_forward：默认边界 + 越界拒绝）的移植。

锁定：
1. dev 侧 env 物理截断到 sel_end：evaluate/wf/submit 的 sim 长度 =
   dev bar 数；响应带 dev_region 标注
2. WF 末折不越 sel_end（折铺在 dev 区内）
3. 显式 t1_date 越过 sel_end → 拒绝（不是静默放行）
4. test 的 t0_date 早于 sel_end → 拒绝（test 起点不得侵入 dev 区）
5. 无 sel_end（直调合成 env）→ 不切 + region_degraded 标注
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _strategy_fixtures import make_env  # noqa: E402
from dsh_factor_mining.worker import write_env_npz  # noqa: E402
from dsh_strategy_lab.worker import _dev_env, run_request  # noqa: E402

# 均值回复单资产策略（与 CLI 测试同款——确定性、有成交）
MR_SOURCE = '''
import numpy as np

def apply(state, env):
    out = []
    for t in range(env.T):
        w = {}
        if t >= 1:
            rets = env.c[t] / env.c[t - 1] - 1.0
            ok = np.isfinite(rets) & (env.c[t - 1] > 0)
            if ok.any():
                j = int(np.nanargmin(np.where(ok, rets, np.inf)))
                w[env.symbols[j]] = 1.0
        out.append(w)
    return out
'''

CASH_SOURCE = '''
def apply(state, env):
    return [dict() for _ in range(env.T)]
'''


def _npz(tmp_path, env) -> str:
    p = tmp_path / "env.npz"
    write_env_npz(str(p), env)
    return str(p)


def _req(npz, stage, source=MR_SOURCE, **params):
    return {"method": "strategy.evaluate", "npzPath": npz,
            "source": source, "params": {"stage": stage, **params}}


# ---- 1. dev 侧物理截断 ----

def test_dev_env_truncates_to_sel_end(tmp_path):
    """make_env T=60、dev_frac 2/3 → sel_end=dates[40]：dev env 40 bars。"""
    env = make_env(T=60, N=4)
    dev, region = _dev_env(env, {}, "development")
    assert dev.T == 40
    assert str(dev.dates[-1].date()) < str(env.calibration.sel_end)
    assert region["region_degraded"] is False
    assert region["n_bars"] == 40
    assert region["t1_date"] == str(env.calibration.sel_end)


def test_development_sim_ends_at_sel_end(tmp_path):
    npz = _npz(tmp_path, make_env(T=60, N=4))
    out = run_request(_req(npz, "development"))
    assert len(out["sim"]["equity"]) == 40
    assert out["dev_region"]["n_bars"] == 40


def test_submit_gets_truncated_env(tmp_path):
    npz = _npz(tmp_path, make_env(T=60, N=4))
    out = run_request({"method": "strategy.submit", "npzPath": npz,
                       "source": MR_SOURCE, "params": {}})
    assert out["dev_region"]["n_bars"] == 40
    if "sim" in out:
        assert len(out["sim"]["equity"]) == 40


# ---- 2. WF 折铺在 dev 区内 ----

def test_walk_forward_folds_within_dev(tmp_path):
    npz = _npz(tmp_path, make_env(T=60, N=4))
    out = run_request(_req(npz, "walk_forward"))
    last = out["folds"][-1]
    assert last["apply_t1_date"] < str(make_env(T=60, N=4).calibration.sel_end)
    assert len(out["sim"]["equity"]) == 40


# ---- 3. WF t1 越界拒绝 ----

def test_walk_forward_t1_overrun_rejected(tmp_path):
    env = make_env(T=60, N=4)
    npz = _npz(tmp_path, env)
    over = str(env.dates[55].date())   # sel_end=dates[40]，55 越界
    with pytest.raises(ValueError, match="窥视"):
        run_request(_req(npz, "walk_forward", t1_date=over))


def test_walk_forward_t1_within_sel_end_allowed(tmp_path):
    env = make_env(T=60, N=4)
    npz = _npz(tmp_path, env)
    ok_t1 = str(env.dates[30].date())
    out = run_request(_req(npz, "walk_forward", t1_date=ok_t1))
    assert len(out["sim"]["equity"]) == 30   # [0, 30) 含端排除（region_span 语义）


# ---- 4. test t0 守卫 ----

def test_test_region_t0_before_sel_end_rejected(tmp_path):
    env = make_env(T=60, N=4)
    npz = _npz(tmp_path, env)
    early = str(env.dates[10].date())
    with pytest.raises(ValueError, match="侵入 dev 区"):
        run_request(_req(npz, "test", t0_date=early))


def test_test_region_default_t0_is_sel_end(tmp_path):
    npz = _npz(tmp_path, make_env(T=60, N=4))
    out = run_request(_req(npz, "test", source=CASH_SOURCE))
    assert out["t0_date"] == str(make_env(T=60, N=4).calibration.sel_end)
    # [40, 60) → 20 bars
    assert len(out["sim"]["equity"]) == 20


# ---- 5. 无 sel_end → degraded 标注 ----

def test_no_sel_end_degraded(tmp_path):
    """直调合成 env 无分界 → 不切 + region_degraded（此配置 test 一次性
    语义不成立——生产面板必须配三区）。"""
    from dsh_factor_mining.factor.env import Calibration
    env = make_env(T=60, N=4)
    env2 = type(env)(env.o, env.h, env.l, env.c, env.v, env.dates,
                     env.symbols, listed=env.listed, amount=env.amount,
                     calibration=Calibration(dev_end=None, sel_end=None))
    dev, region = _dev_env(env2, {}, "development")
    assert dev.T == 60                       # 不切
    assert region["region_degraded"] is True
