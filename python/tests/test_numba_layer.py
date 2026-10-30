# coding=utf-8
"""自动 njit 层（效率编译层 2026-09-01）测试。

spike 实证（tmp/numba-spike/spike-report.md）：纯标量循环 njit 36-797x、
等价 4/4、编译失败无害快速失败。此处锁定：
- _njit_accelerate 单元：编译加速、等价、不可编译回退、numba 缺失降级
- 端到端：烟测门判 njit 可救 → evaluate 的 perf.engine == "njit"
"""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import bridge as bridge_mod
from dsh_factor_mining import worker as worker_mod
from dsh_factor_mining.bridge import BridgeError
from dsh_factor_mining.worker import _njit_accelerate

# 纯标量双层循环（spike F2 型——事故主力形态），内层 1500 次浮点累加
# 制造"解释慢/编译快"的确定性差距；函数内 import numpy = 平台因子惯例
# （顺带验证 njit_prepare 的白名单剥离）
SCALAR_LOOP = (
    "def factor(env):\n"
    "    import numpy as np\n"
    "    c = env.c\n"
    "    T, N = c.shape\n"
    "    out = np.empty_like(c)\n"
    "    for t in range(T):\n"
    "        for j in range(N):\n"
    "            x = c[t, j]\n"
    "            for k in range(1500):\n"
    "                x = x * 1.0000001 + 0.0000001\n"
    "            out[t, j] = x\n"
    "    return out\n")

VEC_EQ = (
    "def factor(env):\n"
    "    import numpy as np\n"
    "    c = env.c\n"
    "    x = c.copy()\n"
    "    for k in range(1500):\n"
    "        x = x * 1.0000001 + 0.0000001\n"
    "    return x\n")


def _compile_ns(src):
    ns = {"np": np, "pd": __import__("pandas")}
    exec(compile(src, "<t>", "exec"), ns)
    return ns["factor"]


@pytest.fixture(autouse=True)
def _clear_memo():
    worker_mod._NUMBA_MEMO.clear()
    yield
    worker_mod._NUMBA_MEMO.clear()


def _tiny_env():
    from _strategy_fixtures import make_env
    return make_env(T=60, N=4)


def test_njit_accelerate_equivalent_and_fast():
    env = _tiny_env()
    fn = _compile_ns(SCALAR_LOOP)
    t0 = time.perf_counter()
    ref = fn(env)
    t_interp = time.perf_counter() - t0
    acc = _njit_accelerate(fn, SCALAR_LOOP)
    t0 = time.perf_counter()          # 首调含编译
    got = acc(env)
    t_first = time.perf_counter() - t0
    t0 = time.perf_counter()
    acc(env)
    t_compiled = time.perf_counter() - t0
    assert np.allclose(ref, got, rtol=1e-9, atol=1e-12)
    assert t_compiled < max(t_interp * 0.5, 1.0), (
        f"编译版应显著快于解释版: interp={t_interp:.3f}s "
        f"compiled={t_compiled:.3f}s first={t_first:.2f}s")


def test_njit_accelerate_fallback_on_uncompilable():
    """pandas 不可编译 → 静默回退解释版，结果正确且 memo 永久降级。"""
    src = ("def factor(env):\n"
           "    import pandas as pd\n"
           "    return pd.DataFrame(env.c).shift(1).to_numpy()\n")
    env = _tiny_env()
    fn = _compile_ns(src)
    ref = fn(env)
    acc = _njit_accelerate(fn, src)
    got = acc(env)
    assert np.allclose(ref, got, equal_nan=True)
    fp = worker_mod.source_fingerprint(src)
    assert worker_mod._NUMBA_MEMO[fp]["mode"] == "interp"


def test_njit_accelerate_degrades_without_numba(monkeypatch):
    """numba 缺失/装坏：包装函数原样返回（降级不报错）。"""
    monkeypatch.setitem(sys.modules, "numba", None)
    fn = _compile_ns(SCALAR_LOOP)
    assert _njit_accelerate(fn, SCALAR_LOOP) is fn


@pytest.fixture
def njit_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_FACTOR_REWRITE", "0")
    monkeypatch.setattr(bridge_mod, "effective_wall_timeout",
                        lambda base_s: float(base_s))
    from test_bridge_smoke import _make_bridge
    b, _ = _make_bridge(tmp_path)
    b.dispatch("data.load", {"envId": "primary"})
    # eff(causality)=3.0 / eff(evaluate)=1.5：解释版投影超预算、
    # 编译版投影进预算 → engage 区间（对编译耗时波动留裕量）
    b.worker_timeout_ms = 3000
    b._smoke_min_cells = 0
    return b


def test_gate_engages_njit_end_to_end(njit_bridge, monkeypatch):
    """端到端：解释版投影超预算、编译版进预算 → 门判 njit、评估走编译版、
    perf.engine=="njit"（透传链：gate→params→worker→perf）。

    烟测原始数据合成注入（决策逻辑/worker 执行/遥测全真）：解释版
    est_full=9.4s×causality 8 遍=75s>30s（拒绝区）、编译版 0.09s（进
    预算）→ 必判 njit，与机器速度无关。"""
    b = njit_bridge
    b.worker_timeout_ms = 30000       # 真预算（覆盖 worker 启动+numba 编译）
    real_worker = b._run_worker

    def _wrapped_worker(method, source, params, env, **kw):
        if method == "factor.smoke":
            return {"t_small_s": 1.0, "small_cells": 10240,
                    "t_smoke_s": 4.0, "smoke_cells": 20480,
                    "full_cells": 48000,
                    "njit": {"available": True, "compiled": True,
                             "equiv": True, "compile_s": 1.0,
                             "t_small_s": 0.01, "t_smoke_s": 0.04}}
        return real_worker(method, source, params, env, **kw)

    monkeypatch.setattr(b, "_run_worker", _wrapped_worker)
    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": SCALAR_LOOP,
                       "stage": "development", "async": False})
    assert diag.get("perf", {}).get("engine") == "njit"
    assert diag["perf"]["smoke"]["verdict"] == "njit"
    # 数值正确性：与向量化等价实现（逐元素独立递推，浮点同序）的
    # 诊断核心量对照
    diag2 = b.dispatch("factor.evaluate",
                       {"envId": "primary", "source": VEC_EQ,
                        "stage": "development", "async": False})
    assert diag2.get("ic_ir_train") is not None
    for k in ("ic_ir_train", "ic_mean_train"):
        assert diag[k] == pytest.approx(diag2[k], abs=1e-12)


def test_gate_engages_njit_async_path(njit_bridge, monkeypatch):
    """F7：异步路径（默认 async 提交 + job_id 轮询）同样启用 njit——
    作业线程内 gate→item params→worker→perf 的透传链。"""
    from test_bridge_smoke import _poll_evaluate
    b = njit_bridge
    b.worker_timeout_ms = 30000
    real_worker = b._run_worker

    def _wrapped_worker(method, source, params, env, **kw):
        if method == "factor.smoke":
            return {"t_small_s": 1.0, "small_cells": 10240,
                    "t_smoke_s": 4.0, "smoke_cells": 20480,
                    "full_cells": 48000,
                    "njit": {"available": True, "compiled": True,
                             "equiv": True, "compile_s": 1.0,
                             "t_small_s": 0.01, "t_smoke_s": 0.04}}
        return real_worker(method, source, params, env, **kw)

    monkeypatch.setattr(b, "_run_worker", _wrapped_worker)
    submit = b.dispatch("factor.evaluate",
                        {"envId": "primary", "source": SCALAR_LOOP,
                         "stage": "development"})       # 默认异步
    diag = _poll_evaluate(b, submit)
    assert diag.get("perf", {}).get("engine") == "njit"
