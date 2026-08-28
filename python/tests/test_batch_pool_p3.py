# coding=utf-8
"""P3 批次进程池测试：并行=顺序等价、部分失败隔离、真实加速比。

worker.run_request 直接调用（并行路径在 worker 内部用 ProcessPoolExecutor
派生子进程——测试即真实多进程执行）。
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from dsh_factor_mining.worker import load_env_npz, run_request, write_env_npz


def _env(T=160, N=40, seed=7):
    import pandas as pd

    from dsh_factor_mining.factor.env import Calibration, FactorEnv

    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.01, size=(T, N))
    c = 10.0 * np.cumprod(1 + rets, axis=0)
    dates = pd.bdate_range("2020-01-01", periods=T)
    cal = Calibration(dev_end=str(dates[110].date()), sel_end=str(dates[140].date()))
    return FactorEnv(c, c, c, c, np.full((T, N), 1e5), dates,
                     [f"E{j}" for j in range(N)], calibration=cal)


def _src(seed, delay=0.0):
    return (f"""
import time
import numpy as np

def factor(env):
    time.sleep({delay})
    rng = np.random.default_rng({seed})
    r = env.c / np.roll(env.c, 1, axis=0) - 1.0
    r[0] = 0.0
    return np.nan_to_num(r, nan=0.0) + rng.normal(0, 1e-9, size=r.shape)
""")


@pytest.fixture()
def batch_env(tmp_path):
    env = _env()
    npz = tmp_path / "env.npz"
    write_env_npz(str(npz), env)
    return str(npz)


def _run_batch(npz, sources, jobs):
    import os

    from dsh_factor_mining import worker as wmod

    old = os.environ.get("DSH_FACTOR_JOBS")
    os.environ["DSH_FACTOR_JOBS"] = str(jobs)
    try:
        req = {"npzPath": npz, "method": "factor.evaluate_batch",
               "source": "", "params": {"sources": sources,
                                        "state_root": None}}
        return wmod.run_request(req)
    finally:
        if old is None:
            os.environ.pop("DSH_FACTOR_JOBS", None)
        else:
            os.environ["DSH_FACTOR_JOBS"] = old


def _key_diag(diag):
    """可比较的关键数字（剔除时变/不可复现字段）。"""
    out = {}
    for k in ("ic_ir_train", "ic_mean_train", "ic_n_train"):
        v = diag.get(k)
        out[k] = round(float(v), 10) if isinstance(v, (int, float)) else v
    return out


def test_parallel_batch_equals_sequential(batch_env):
    srcs = {f"f{i}": _src(100 + i) for i in range(4)}
    seq = _run_batch(batch_env, srcs, jobs=1)
    par = _run_batch(batch_env, srcs, jobs=4)
    assert set(seq["factors"]) == set(par["factors"])
    for n in srcs:
        assert _key_diag(par["factors"][n]) == _key_diag(seq["factors"][n]), n
    # 家族口径一致（bar_sigma/deflated 由收齐后的同一段代码算）
    for k in ("M", "rho_bar"):
        if k in seq.get("batch", {}):
            a, b = seq["batch"][k], par["batch"][k]
            if isinstance(a, float):
                assert abs(a - b) < 1e-9, (k, a, b)
            else:
                assert a == b, (k, a, b)


def test_parallel_batch_partial_failure_isolation(batch_env):
    srcs = {
        "good1": _src(1),
        "bad_compile": "def not_factor(env):\n    return None\n",   # 缺 factor()
        "bad_runtime": "def factor(env):\n    return 1 / 0\n",       # 运行时炸
        "good2": _src(2),
    }
    out = _run_batch(batch_env, srcs, jobs=3)
    fs = out["factors"]
    assert "error" not in fs["good1"] and "error" not in fs["good2"]
    assert "error" in fs["bad_compile"]
    assert "error" in fs["bad_runtime"]
    # 家族口径只算有效成员
    assert out["batch"]["M"] == 2
    assert out["batch"]["M_requested"] == 4


def test_sequential_batch_partial_failure_too(batch_env):
    """jobs=1 顺序路径同隔离（行为一致性）。"""
    srcs = {"good": _src(3), "bad": "def factor(env):\n    raise ValueError('x')\n"}
    out = _run_batch(batch_env, srcs, jobs=1)
    assert "error" not in out["factors"]["good"]
    assert "error" in out["factors"]["bad"]
    assert out["batch"]["M"] == 1


def test_parallel_batch_wall_speedup(batch_env):
    """6 成员 × 2.2s sleep：并行（jobs=4）墙钟显著小于顺序。

    sleep 不吃 CPU，对机器负载鲁棒；断言阈值放宽到 0.75 防抖。"""
    srcs = {f"s{i}": _src(200 + i, delay=2.2) for i in range(6)}
    t0 = time.monotonic()
    par = _run_batch(batch_env, srcs, jobs=4)
    t_par = time.monotonic() - t0
    t0 = time.monotonic()
    seq = _run_batch(batch_env, srcs, jobs=1)
    t_seq = time.monotonic() - t0
    assert par["batch"]["M"] == 6 and seq["batch"]["M"] == 6
    assert t_par < t_seq * 0.75, f"并行未达加速: par={t_par:.1f}s seq={t_seq:.1f}s"


def test_batch_all_failed_structured(batch_env):
    srcs = {"b1": "def factor(env):\n    raise ValueError('a')\n",
            "b2": "def factor(env):\n    raise ValueError('b')\n"}
    out = _run_batch(batch_env, srcs, jobs=2)
    assert out["batch"]["M"] == 0
    assert all("error" in d for d in out["factors"].values())
