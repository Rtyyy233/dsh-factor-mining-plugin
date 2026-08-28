# coding=utf-8
"""P1 并行/效率测试：CPU 测量、二维超时判据、OMP 环境、伸缩公式 +
真实子进程的饿死/密集双路径验收（strategy CLI e2e）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from dsh_factor_mining.procinfo import (
    DENSE_RATIO,
    STARVED_RATIO,
    cpu_seconds,
    effective_wall_timeout,
    omp_quiet_env,
    resolve_jobs,
    starvation_verdict,
)

SLEEP_SOURCE = '''
import time

def apply(state, env):
    time.sleep(60)
    return [dict() for _ in range(env.T)]
'''

BURN_SOURCE = '''
def apply(state, env):
    x = 0
    while True:
        x += 1
    return [dict() for _ in range(env.T)]
'''


# ---------------------------------------------------------------- 单元

@pytest.mark.parametrize("cpu,wall,klass", [
    (7.0, 10.0, "cpu_dense"),     # ratio 0.7 恰在阈值 → dense
    (9.5, 10.0, "cpu_dense"),
    (1.0, 10.0, "starved"),       # ratio 0.1
    (3.0, 10.0, "starved"),       # ratio 0.3 < 0.35
    (5.0, 10.0, "borderline"),    # 0.35 <= ratio < 0.7 → 保守按真慢
    (None, 10.0, "cpu_dense"),    # CPU 不可读 → 保守
])
def test_starvation_verdict_table(cpu, wall, klass):
    v = starvation_verdict(cpu, wall)
    assert v["class"] == klass
    assert v["ratio"] is None or v["ratio"] == round(float(cpu) / wall, 3)


def test_starvation_verdict_ratios_consistent():
    assert DENSE_RATIO > STARVED_RATIO >= 0
    v0 = starvation_verdict(0, 10.0)
    assert v0["class"] == "starved" and v0["cpu_s"] == 0.0


def test_cpu_seconds_self_measurable():
    import os

    t0 = cpu_seconds(os.getpid()) or 0.0
    burn = sum(i * i for i in range(3_000_000))  # ~0.2s CPU
    t1 = cpu_seconds(os.getpid()) or 0.0
    assert burn >= 0
    assert t1 - t0 > 0.05, f"CPU 时间未随计算增长: {t0} → {t1}"


def test_effective_wall_timeout_scaling(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    monkeypatch.delenv("DSH_FACTOR_JOBS", raising=False)
    monkeypatch.delenv("DSH_FACTOR_TIMEOUT", raising=False)
    # jobs=2（声明让出 3/4 核）→ ×4
    assert effective_wall_timeout(base_s=300.0, jobs=2) == 1200.0
    # jobs=8（满核）→ base
    assert effective_wall_timeout(base_s=300.0, jobs=8) == 300.0
    # jobs=1 在 32 核 → clamp 到 4×base
    monkeypatch.setattr("os.cpu_count", lambda: 32)
    assert effective_wall_timeout(base_s=300.0, jobs=1) == 1200.0
    # 永不低于 base
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert effective_wall_timeout(base_s=300.0, jobs=99) == 300.0


def test_jobs_knob_env(monkeypatch):
    monkeypatch.setenv("DSH_FACTOR_JOBS", "3")
    assert resolve_jobs() == 3
    monkeypatch.setenv("DSH_FACTOR_JOBS", "junk")
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    assert resolve_jobs() == 7  # 默认 cpu−1


def test_timeout_base_env(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 8)
    monkeypatch.setenv("DSH_FACTOR_TIMEOUT", "120")
    monkeypatch.delenv("DSH_FACTOR_JOBS", raising=False)
    assert effective_wall_timeout(jobs=8) == 120.0


def test_omp_quiet_env():
    env = omp_quiet_env()
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
              "NUMEXPR_NUM_THREADS"):
        assert env[k] == "1"
    # 不丢原环境（PATH 等保留）
    assert env.get("PATH") == __import__("os").environ.get("PATH")


# ---------------------------------------------------------------- e2e：strategy CLI 双路径

@pytest.fixture()
def _lab(tmp_path):
    from test_strategy_cli import _write_panel

    spec = _write_panel(tmp_path)
    cwd = tmp_path
    r = subprocess.run(
        [sys.executable, "-m", "dsh_strategy_lab", "build-env",
         "--env-spec", str(spec), "--out", str(cwd / "env.npz")],
        capture_output=True, text=True, cwd=str(cwd), timeout=300)
    assert r.returncode == 0, r.stderr
    return cwd


def _run_eval(cwd, source_text, timeout):
    src = cwd / "strategy.py"
    src.write_text(source_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "dsh_strategy_lab", "evaluate",
         "--source-file", str(src), "--env-npz", str(cwd / "env.npz"),
         "--stage", "development", "--timeout", str(timeout)],
        capture_output=True, text=True, cwd=str(cwd), timeout=120)


def test_e2e_starved_verdict_infra_failure(_lab):
    """sleep 策略（低 CPU）+ 小超时 → 饿死归因：机器超订话术、
    重试指引、零写入不烧指纹。"""
    cwd = _lab
    r = _run_eval(cwd, SLEEP_SOURCE, timeout=4)
    assert r.returncode != 0
    err = r.stderr + r.stdout
    assert "机器超订" in err and "重试同一策略" in err
    assert "不是策略实现慢" in err or "不是策略实现" in err
    # 零写入：没有 trail 条目产生
    trail = cwd / ".strategy-lab" / "strategy_trail.json"
    assert not trail.exists() or json.loads(trail.read_text(encoding="utf-8")) == []


def test_e2e_cpu_dense_verdict_four_elements(_lab):
    """纯 Python 烧 CPU 策略 + 小超时 → 真慢归因：四要素 +
    实测 CPU 数据标注。"""
    cwd = _lab
    r = _run_eval(cwd, BURN_SOURCE, timeout=4)
    assert r.returncode != 0
    err = r.stderr + r.stdout
    assert "修实现" in err and "零写入" in err
    assert "实测 CPU" in err  # 判据数据随错误披露
    assert "机器超订" not in err
