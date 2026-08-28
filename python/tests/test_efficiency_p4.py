# coding=utf-8
"""P4 效率四层测试：AST 硬拒（A 类五项）/ B 类警告 / 机器校准 /
perf CPU 口径 / trail cpu_s + registry cpu_rel。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from dsh_factor_mining.discipline import scan_inefficiency

# ---- A 类五项样例（每份只含一种反模式 + 合法向量化主体） ----

SRC_ITERROWS = '''
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return c.rolling(5).mean().values
'''
# 覆盖：iterrows 调用替换进主体
SRC_ITERROWS_FULL = SRC_ITERROWS.replace(
    "return c.rolling(5).mean().values",
    "out = []\n    for _, row in c.iterrows():\n        out.append(row)\n    return c.rolling(5).mean().values")

SRC_ITERTUPLES = SRC_ITERROWS.replace(
    "return c.rolling(5).mean().values",
    "s = 0.0\n    for row in c.itertuples():\n        s += float(row[1])\n    return c.rolling(5).mean().values")

SRC_APPLYMAP = SRC_ITERROWS.replace(
    "return c.rolling(5).mean().values",
    "v = c.applymap(lambda x: x)\n    return c.rolling(5).mean().values")

SRC_APPLY_LAMBDA = SRC_ITERROWS.replace(
    "return c.rolling(5).mean().values",
    "v = c.apply(lambda col: col.rank())\n    return c.rolling(5).mean().values")

SRC_LOOP_CONCAT = '''
import numpy as np

def factor(env):
    out = None
    for t in range(env.T):
        part = env.c[t] * 1.0
        out = np.concatenate([out, part]) if out is not None else part
    return np.ascontiguousarray(out.reshape(env.T, env.N))
'''

GOOD_LOOP = '''
import numpy as np
import pandas as pd

def factor(env):
    acc = np.zeros_like(env.c)
    for w in (3, 5, 10):        # 小常数窗口集迭代：循环是对的（因果 rolling）
        acc += np.nan_to_num(pd.DataFrame(env.c).rolling(w).mean().values)
    return acc / 3.0
'''

GOOD_COLLECT = '''
import numpy as np
import pandas as pd

def factor(env):
    parts = []
    for w in (3, 5, 10):
        parts.append(np.nan_to_num(pd.DataFrame(env.c).rolling(w).mean().values))   # list.append 收集：正确写法
    return np.mean(parts, axis=0)
'''

NESTED_FOR = '''
import numpy as np

def factor(env):
    out = np.zeros_like(env.c)
    for t in range(env.T):
        for j in range(env.N):
            out[t, j] = env.c[t, j]
    return out
'''


# ---------------------------------------------------------------- scan 单元

@pytest.mark.parametrize("src", [
    SRC_ITERROWS_FULL,
    SRC_ITERTUPLES,
    SRC_APPLYMAP,
    SRC_APPLY_LAMBDA,
    SRC_LOOP_CONCAT,
], ids=["iterrows", "itertuples", "applymap", "apply-lambda", "loop-concat"])
def test_class_a_detected_and_hard(src):
    out = scan_inefficiency(src)
    assert out["hard_reject"] is True, out
    assert out["class_a"], out
    # 替换模板内嵌在 pattern 文案里（可直接抄的修法：改用/先收集/预分配）
    assert any(("改" in h["pattern"]) or ("先" in h["pattern"]) for h in out["class_a"])
    assert out["hits"][0]["line"] >= 1  # AST 行号（非 0）


def test_good_sources_pass():
    for src in (GOOD_LOOP, GOOD_COLLECT):
        out = scan_inefficiency(src)
        assert out["ok"] is True and out["hard_reject"] is False, (src, out)


def test_nested_for_is_warning_only():
    out = scan_inefficiency(NESTED_FOR)
    assert out["hard_reject"] is False
    assert any("嵌套 for" in h["pattern"] for h in out["class_b"])


def test_syntax_error_fallback_no_hard_reject():
    out = scan_inefficiency("def factor(env)\n  return None")  # 语法错误
    assert out["hard_reject"] is False  # 回退正则只警告，不越权硬拒


def test_scan_shape_backward_compat():
    out = scan_inefficiency("import numpy as np\n\ndef factor(env):\n    return env.c\n")
    assert out["ok"] is True and out["hits"] == []
    assert "advice" not in out  # 干净源不带 advice


# ---------------------------------------------------------------- 机器校准

def test_calib_creates_caches_and_invalidates(tmp_path, monkeypatch):
    from dsh_factor_mining.factor import calib

    calls = []

    def _fake_workload():
        calls.append(1)
        _ = sum(i * i for i in range(400_000))  # >计时分辨率的微小 CPU

    monkeypatch.setattr(calib, "_reference_workload", _fake_workload)
    r1 = calib.ref_cpu_seconds(tmp_path)
    assert 0.0 < r1 < 0.5  # 假负载近零耗时（缓存条件 ref_cpu_s > 0）
    f = tmp_path / "machine-calib.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["ref_cpu_s"] == r1 and data["cpu_count"] >= 1
    # 缓存：第二次不重测
    calib.ref_cpu_seconds(tmp_path)
    assert len(calls) == 3  # 首测 3 次，缓存命中 0 次
    # 版本失效 → 重测
    data["engine_version"] = "stale"
    f.write_text(json.dumps(data), encoding="utf-8")
    calib.ref_cpu_seconds(tmp_path)
    assert len(calls) == 6


def test_calib_read_only_view(tmp_path):
    from dsh_factor_mining.factor import calib

    assert calib.read_calib(tmp_path) is None
    # 真负载一次（秒级）——校准本身也是被测对象
    ref = calib.ref_cpu_seconds(tmp_path)
    assert ref is None or ref > 0
    if ref:
        assert calib.read_calib(tmp_path)["ref_cpu_s"] == ref


# ---------------------------------------------------------------- bridge 集成：A 类硬拒零 trial

def _bridge(tmp_path):
    from tests.test_timeout_guidance import _make_bridge

    return _make_bridge(tmp_path)


def test_class_a_rejected_at_causality_gate_zero_trial(tmp_path):
    b = _bridge(tmp_path)
    with pytest.raises(Exception) as ei:
        b.dispatch("factor.evaluate", {"envId": "primary",
                                       "source": SRC_ITERROWS_FULL,
                                       "stage": "development"})
    msg = str(getattr(ei.value, "message", ei.value))
    assert "A 类" in msg and "iterrows" in msg
    assert "不计入 trial" in msg
    assert "零计算消耗" in msg
    # 零 trial：trail_engine 无条目
    te = tmp_path / "state" / "trail_engine.json"
    assert not te.exists() or json.loads(te.read_text(encoding="utf-8")) == []


def test_class_a_rejected_in_explicit_check(tmp_path):
    b = _bridge(tmp_path)
    with pytest.raises(Exception) as ei:
        b.dispatch("factor.check_causality", {"envId": "primary",
                                              "source": SRC_LOOP_CONCAT})
    msg = str(getattr(ei.value, "message", ei.value))
    assert "A 类" in msg and "concat" in msg


def test_class_a_rejected_in_batch_member(tmp_path):
    b = _bridge(tmp_path)
    with pytest.raises(Exception) as ei:
        b.dispatch("factor.evaluate_batch",
                   {"envId": "primary",
                    "sources": {"good": "import numpy as np\n\ndef factor(env):\n    return env.c\n",
                                "bad": SRC_APPLY_LAMBDA}})
    msg = str(getattr(ei.value, "message", ei.value))
    assert "A 类" in msg and "apply" in msg


def test_good_loop_evaluates_normally(tmp_path):
    b = _bridge(tmp_path)
    diag = b.dispatch("factor.evaluate", {"envId": "primary",
                                          "source": GOOD_LOOP,
                                          "stage": "development"})
    assert "error" not in diag or not diag.get("error")
    # perf CPU 口径字段在场
    assert "cpu_s" in diag.get("perf", {}) and diag["perf"]["basis"] == "cpu_s"


def test_trail_entry_has_cpu_s(tmp_path):
    b = _bridge(tmp_path)
    b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD_LOOP,
                                   "stage": "development"})
    entries = json.loads((tmp_path / "state" / "trail_engine.json").read_text(encoding="utf-8"))
    assert entries and isinstance(entries[-1].get("cpu_s"), (int, float))


def test_perf_cpu_basis_sleep_not_flagged(tmp_path):
    """sleep 因子 CPU≈0 → perf ok（口径是 CPU 不是墙钟——这正是 P4 目的）。"""
    b = _bridge(tmp_path)
    sleepy = "import time\nimport numpy as np\n\ndef factor(env):\n    time.sleep(0.3)\n    return env.c * 1.0\n"
    diag = b.dispatch("factor.evaluate", {"envId": "primary", "source": sleepy,
                                          "stage": "development"})
    assert diag["perf"]["verdict"] == "ok", diag["perf"]
    assert diag["perf"]["cpu_s"] < 0.2
