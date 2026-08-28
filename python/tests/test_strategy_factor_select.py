# coding=utf-8
"""factor-select 记录命令回归（2026-08-28 用户决策：策略层要有 select
的记录——快照落 .strategy-lab/factor_select.json，决策可留痕）。

锁定：
1. 真子进程 CLI：跑一轮比较 → 快照存在且含 select 数字（sel_ic_ir /
   sel_net_annual）与 discipline 注记
2. --battery：附衰减剖面 + 稳健性量具（half_life / yearly_consistency /
   rolling_pos_frac / boot_z 在场且为数）
3. --choose 留痕 + --choose-only 后补（不重跑比较）
4. 快照缺 --factor-state-root → 可行动报错
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 反转因子（因果：t 行只用 ≤t 数据；-1×20 日动量）
MR_SOURCE = '''
import numpy as np
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    rets = (c / c.shift(20) - 1.0).values
    return np.where(np.isfinite(rets), -rets, np.nan)
'''


def _factor_state_root(tmp_path):
    """因子层状态根：面板 + config + data.load + accepted registry 条目。"""
    T, N = 900, 30
    rng = np.random.default_rng(21)
    noise = rng.normal(0, 0.012, size=(T, N))
    rets = np.zeros((T, N))
    rets[0] = noise[0]
    for t in range(1, T):
        rets[t] = -0.55 * rets[t - 1] + noise[t]
    c = 10.0 * np.cumprod(1 + rets, axis=0)
    o = np.vstack([c[:1], c[:-1]])
    dates = pd.bdate_range("2020-01-01", periods=T)
    rows = []
    for j in range(N):
        for t in range(T):
            rows.append({"symbol": f"E{j}", "date": dates[t],
                         "open": o[t, j], "high": max(o[t, j], c[t, j]) * 1.005,
                         "low": min(o[t, j], c[t, j]) / 1.005,
                         "close": c[t, j], "volume": 1e5, "amount": 1e6})
    panel = tmp_path / "panel.parquet"
    pd.DataFrame(rows).to_parquet(panel)

    from dsh_factor_mining.bridge import Bridge
    from dsh_factor_mining.discipline import source_fingerprint
    from dsh_factor_mining.state import write_registry
    froot = tmp_path / "factor-root"
    fb = Bridge(state_root=str(froot), execution_mode="in_process")
    fb.dispatch("config.save", {"config": {"version": 1, "environments": {
        "etf": {"source": {"type": "parquet", "path": str(panel)},
                "layout": "long",
                "mapping": {"symbol": "symbol", "date": "date", "open": "open",
                            "high": "high", "low": "low", "close": "close",
                            "volume": "volume", "amount": "amount"},
                "calibration": {"dev_end": str(dates[500].date()),
                                "sel_end": str(dates[700].date())}}}}})
    fb.dispatch("data.load", {"envId": "etf"})
    write_registry([{"name": "mr_feature", "accepted": True,
                     "source": MR_SOURCE,
                     "source_hash": source_fingerprint(MR_SOURCE),
                     "ic_ir_train": 0.5,
                     "tracks": {"ic": {"accepted": True},
                                "tail": {"accepted": False}}}],
                   root=str(froot))
    return froot


def _cli(*args, cwd):
    # PYTHONPATH 前插 src：editable 安装指向 main 检出，未合并的新码要
    # 显式优先（合入 main 后此行无害）
    env = {**os.environ,
           "PYTHONPATH": str(ROOT / "src") + os.pathsep
           + os.environ.get("PYTHONPATH", "")}
    return subprocess.run([sys.executable, "-m", "dsh_strategy_lab", *args],
                          capture_output=True, text=True, cwd=str(cwd),
                          env=env, timeout=600)


def test_factor_select_snapshot_with_battery(tmp_path):
    froot = _factor_state_root(tmp_path)
    sroot = tmp_path / "strategy-root"
    r = _cli("factor-select", "--factor-state-root", str(froot),
             "--state-root", str(sroot), "--battery",
             cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["n_candidates"] == 1
    snap = json.loads((sroot / "factor_select.json").read_text(encoding="utf-8"))
    e = snap["entries"][0]
    assert e["name"] == "mr_feature" and e["track"] == "ic"
    assert isinstance(e["sel_ic_ir"], (int, float))
    assert e["sel_net_annual"] is not None
    bat = e["battery"]
    assert set(bat["decay_ic"].keys()) == {"1", "5", "10", "20", "60"}
    for k in ("yearly_consistency", "rolling_pos_frac", "boot_z"):
        assert isinstance(bat[k], (int, float)), (k, bat)
    assert "discipline" in snap and "不因 select 数字回头改因子" in snap["discipline"]


def test_choose_and_choose_only(tmp_path):
    froot = _factor_state_root(tmp_path)
    sroot = tmp_path / "strategy-root"
    r = _cli("factor-select", "--factor-state-root", str(froot),
             "--state-root", str(sroot), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    snap = json.loads((sroot / "factor_select.json").read_text(encoding="utf-8"))
    assert snap["chosen"] == []

    r2 = _cli("factor-select", "--state-root", str(sroot), "--choose-only",
              "--choose", "mr_feature", cwd=tmp_path)
    assert r2.returncode == 0, r2.stderr
    snap2 = json.loads((sroot / "factor_select.json").read_text(encoding="utf-8"))
    assert snap2["chosen"] == ["mr_feature"]
    assert snap2["chosen_ts"]
    assert snap2["ts"] == snap["ts"]   # 比较轮次未重跑


def test_missing_factor_state_root_rejected(tmp_path):
    r = _cli("factor-select", "--state-root", str(tmp_path / "s"),
             cwd=tmp_path)
    assert r.returncode == 2
    assert "--factor-state-root" in r.stderr
