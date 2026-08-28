# coding=utf-8
"""P5 CLI 端到端测试（规划 §8.8 事务项）：build-env → evaluate → wf →
submit 事务化（接受/程序性拒收/实质性拒收）→ 超时零写入 → trail/status。

全部走真实子进程（python -m dsh_strategy_lab …）——CLI 是唯一评估路径，
测试它的方式就是用它。
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

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

# 弱因子（常数）：被动 top-K = 前 K 个等权买入持有 → 已知劣后于 MR overlay
WEAK_FACTOR = '''
import numpy as np

def factor(env):
    return np.zeros((env.T, env.N))
'''

HANG_SOURCE = '''
def apply(state, env):
    while True:
        pass
'''


def _write_panel(tmp_path, T=80, N=6, seed=21):
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 0.012, size=(T, N))
    rets = np.zeros((T, N))
    rets[0] = noise[0]
    for t in range(1, T):
        rets[t] = -0.55 * rets[t - 1] + noise[t]
    c = 10.0 * np.cumprod(1 + rets, axis=0)
    o = np.vstack([c[:1], c[:-1]])
    rows = []
    dates = pd.bdate_range("2020-01-01", periods=T)
    for j in range(N):
        for t in range(T):
            rows.append({"symbol": f"E{j}", "date": dates[t],
                         "open": o[t, j], "high": max(o[t, j], c[t, j]) * 1.005,
                         "low": min(o[t, j], c[t, j]) / 1.005,
                         "close": c[t, j], "volume": 1e5,
                         "amount": 1e6})
    panel = tmp_path / "panel.parquet"
    pd.DataFrame(rows).to_parquet(panel)
    spec = {
        "id": "test", "layout": "long",
        "source": {"type": "parquet", "path": str(panel)},
        "mapping": {"symbol": "symbol", "date": "date", "open": "open",
                    "high": "high", "low": "low", "close": "close",
                    "volume": "volume", "amount": "amount"},
        "constraints": {"minSymbols": 3, "minDates": 40,
                        "requireFiniteOhlcv": "report"},
        "calibration": {"dev_end": str(dates[26].date()),
                        "sel_end": str(dates[53].date())},
    }
    spec_path = tmp_path / "env_spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path


def _make_factor_registry(tmp_path):
    from dsh_factor_mining.state import write_registry
    froot = tmp_path / "factor-root"
    froot.mkdir(exist_ok=True)
    from dsh_factor_mining.discipline import source_fingerprint
    fhash = source_fingerprint(WEAK_FACTOR)
    write_registry([{"name": "weak_zero", "source_hash": fhash,
                     "accepted": True, "source": WEAK_FACTOR}], root=froot)
    refs = [{"source_hash": fhash, "horizon": 20}]
    refs_path = tmp_path / "refs.json"
    refs_path.write_text(json.dumps(refs), encoding="utf-8")
    return froot, refs_path


def _cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "dsh_strategy_lab", *args],
        capture_output=True, text=True, cwd=str(cwd), timeout=600)


@pytest.fixture()
def lab(tmp_path):
    """面板 + 因子 registry + 源文件齐备的工作目录。"""
    spec = _write_panel(tmp_path)
    froot, refs = _make_factor_registry(tmp_path)
    src = tmp_path / "mr.py"
    src.write_text(MR_SOURCE, encoding="utf-8")
    return {"cwd": tmp_path, "spec": spec, "froot": froot,
            "refs": refs, "src": src}


def test_worker_ping():
    out = subprocess.run(
        [sys.executable, "-m", "dsh_strategy_lab.worker", "--ping"],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert json.loads(out.stdout)["worker"] == "strategy-lab-ready"


def test_full_pipeline_evaluate_submit_accept(lab):
    cwd, spec, refs, src = lab["cwd"], lab["spec"], lab["refs"], lab["src"]
    common = ["--factor-state-root", str(lab["froot"])]

    r = _cli("build-env", "--env-spec", str(spec),
             "--out", str(cwd / "env.npz"), *common, cwd=cwd)
    assert r.returncode == 0, r.stderr
    assert Path(cwd / "env.npz").exists()

    r = _cli("evaluate", "--source-file", str(src), "--env-npz",
             str(cwd / "env.npz"), "--stage", "development",
             "--factor-refs", str(refs), *common, cwd=cwd)
    assert r.returncode == 0, r.stderr
    resp = json.loads(r.stdout)
    assert resp["ok"] and resp["stage"] == "development"
    body = json.dumps(resp)
    assert '"equity": [' not in body            # equity 数组被投影掉（防截断）
    assert "daily_returns" not in body and "base_path" not in body
    assert "final_equity" in body               # 指标字段保留（标量）
    assert resp["result"]["audit"]["g0_pass"] is True
    assert resp["n_trials"] == 1                    # development 计试验

    r = _cli("evaluate", "--source-file", str(src), "--env-npz",
             str(cwd / "env.npz"), "--stage", "walk_forward",
             "--factor-refs", str(refs), *common, cwd=cwd)
    assert r.returncode == 0, r.stderr
    resp = json.loads(r.stdout)
    assert resp["result"]["n_folds"] == 5
    assert resp["n_trials"] == 2                    # wf 再计一次

    # 同五元组的 submit 按同键展开（wf stage 更新不新增）——先看 submit 前
    r = _cli("submit", "--source-file", str(src), "--env-npz",
             str(cwd / "env.npz"), "--factor-refs", str(refs),
             "--name", "mr_loser", *common, cwd=cwd)
    assert r.returncode == 0, r.stderr
    resp = json.loads(r.stdout)
    assert resp["accepted"] is True, resp
    assert resp["reject_kind"] is None
    # 事务落地：registry 接受条目 + trail 计试验（submit=wf 同键更新）
    reg = json.loads((cwd / ".strategy-lab" / "strategy_registry.json")
                     .read_text(encoding="utf-8"))
    assert reg[-1]["accepted"] is True and reg[-1]["name"] == "mr_loser"
    r = _cli("status", *common, cwd=cwd)
    assert r.returncode == 0
    st = json.loads(r.stdout)
    assert st["registry"]["accepted"] == 1
    assert st["n_trials"] == 2                      # wf 指纹同键 = 更新不新增
    r = _cli("trail", "--last", "5", *common, cwd=cwd)
    assert r.returncode == 0
    assert len(json.loads(r.stdout)["entries"]) == 2


def test_submit_procedural_reject_bad_refs_zero_trial(lab):
    cwd, src = lab["cwd"], lab["src"]
    bad_refs = cwd / "bad_refs.json"
    bad_refs.write_text(json.dumps(
        [{"source_hash": "f" * 16, "horizon": 20}]), encoding="utf-8")
    r = _cli("submit", "--source-file", str(src), "--env-npz",
             str(cwd / "env.npz"), "--factor-refs", str(bad_refs),
             "--factor-state-root", str(lab["froot"]), cwd=cwd)
    assert r.returncode == 1
    resp = json.loads(r.stdout)
    assert resp["reject_kind"] == "procedural"
    # 程序性拒收（无评估）：registry 记拒收条目供避坑，但不计试验
    reg = json.loads((cwd / ".strategy-lab" / "strategy_registry.json")
                     .read_text(encoding="utf-8"))
    assert reg[-1]["accepted"] is False
    assert reg[-1]["reject_kind"] == "procedural"
    r = _cli("status", "--factor-state-root", str(lab["froot"]), cwd=cwd)
    assert json.loads(r.stdout)["n_trials"] == 0


def test_timeout_zero_writes(lab):
    """超时 = 四要素错误 + 零写入（registry/trail 不动，不烧指纹）。"""
    cwd = lab["cwd"]
    hang = cwd / "hang.py"
    hang.write_text(HANG_SOURCE, encoding="utf-8")
    spec = lab["spec"]
    r = _cli("build-env", "--env-spec", str(spec), "--out",
             str(cwd / "env.npz"), cwd=cwd)
    assert r.returncode == 0
    r = _cli("evaluate", "--source-file", str(hang), "--env-npz",
             str(cwd / "env.npz"), "--timeout", "8", cwd=cwd)
    assert r.returncode == 2
    err = r.stderr
    for key in ("超时", "零写入", "向量化", "不换假设"):
        assert key in err, f"四要素缺 {key}：{err[:200]}"
    assert not (cwd / ".strategy-lab" / "strategy_registry.json").exists()
    r = _cli("status", cwd=cwd)
    assert json.loads(r.stdout)["n_trials"] == 0


def test_test_stage_one_time_consumption(lab):
    cwd, src, refs = lab["cwd"], lab["src"], lab["refs"]
    r = _cli("build-env", "--env-spec", str(lab["spec"]), "--out",
             str(cwd / "env.npz"), cwd=cwd)
    assert r.returncode == 0
    args = ["evaluate", "--source-file", str(src), "--env-npz",
            str(cwd / "env.npz"), "--stage", "test",
            "--factor-state-root", str(lab["froot"])]
    r = _cli(*args, cwd=cwd)
    assert r.returncode == 0
    lock = json.loads((cwd / ".strategy-lab" / "test_lock.json")
                      .read_text(encoding="utf-8"))
    assert lock["consumed"] is True
    r = _cli(*args, cwd=cwd)
    assert r.returncode == 2 and "一次性" in r.stderr or "禁止" in r.stderr
    # test 不计试验
    r = _cli("status", cwd=cwd)
    assert json.loads(r.stdout)["n_trials"] == 0
