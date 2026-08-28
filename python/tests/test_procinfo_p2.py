# coding=utf-8
"""P2 OS 级硬限额测试：Job Object（Windows）/ RLIMIT（POSIX）。

真实子进程验收：CPU 超额被 OS 确定性击杀（不依赖父进程轮询）、
内存超额报 MemoryError、父进程句柄关闭兜底杀孤儿（KILL_ON_JOB_CLOSE）。
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from dsh_factor_mining.procinfo import (
    attach_worker_limits,
    detach_worker_limits,
    worker_mem_bytes,
)

WIN = sys.platform == "win32"


def _spawn(code: str):
    return subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)


@pytest.mark.skipif(not WIN, reason="Job Object 仅 Windows")
def test_job_cpu_limit_kills_burner():
    proc = _spawn("x=0\nwhile True: x+=1\n")
    job = attach_worker_limits(proc, cpu_s=1.5, mem_bytes=None)
    assert job is not None, "Job Object 挂载失败（应优雅降级但测试环境应可用）"
    t0 = time.monotonic()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        detach_worker_limits(job)
        pytest.fail("CPU 限额未在 20s 内击杀烧 CPU 子进程")
    elapsed = time.monotonic() - t0
    assert proc.returncode != 0 or elapsed <= 20
    detach_worker_limits(job)


@pytest.mark.skipif(not WIN, reason="Job Object 仅 Windows")
def test_job_memory_limit_memoryerror():
    # 子进程申请 512MB（限额 256MB）→ Windows 下分配失败报 MemoryError
    code = (
        "try:\n"
        "    chunks = []\n"
        "    for _ in range(8):\n"
        "        chunks.append(bytearray(64 * 1024 * 1024))\n"
        "    print('ALLOCATED_ALL')\n"
        "except MemoryError:\n"
        "    print('MEMORY_ERROR')\n"
    )
    proc = _spawn(code)
    job = attach_worker_limits(proc, cpu_s=None, mem_bytes=256 * 1024 * 1024)
    assert job is not None
    out, _ = proc.communicate(timeout=60)
    detach_worker_limits(job)
    assert "MEMORY_ERROR" in out, f"内存限额未生效: {out!r}"


@pytest.mark.skipif(not WIN, reason="Job Object 仅 Windows")
def test_job_kill_on_close_orphans():
    """父进程侧句柄关闭（模拟父崩溃/GC）→ job 内进程被 OS 兜底击杀。"""
    proc = _spawn("import time; time.sleep(60)\n")
    job = attach_worker_limits(proc, cpu_s=None, mem_bytes=None)
    assert job is not None
    time.sleep(1.0)
    assert proc.poll() is None, "子进程应仍存活（限额未触发）"
    detach_worker_limits(job)  # 关句柄 = 模拟父进程崩溃后的句柄回收
    deadline = time.monotonic() + 10
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert proc.poll() is not None, "KILL_ON_JOB_CLOSE 未兜底击杀孤儿进程"


@pytest.mark.skipif(WIN, reason="POSIX RLIMIT 路径")
def test_posix_rlimit_cpu():
    from dsh_factor_mining.procinfo import posix_limit_preexec

    pre = posix_limit_preexec(cpu_s=1, mem_bytes=None)
    assert pre is not None
    proc = subprocess.Popen([sys.executable, "-c", "x=0\nwhile True: x+=1"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            preexec_fn=pre)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("RLIMIT_CPU 未击杀")
    assert proc.returncode != 0


def test_worker_mem_bytes_default_and_env(monkeypatch):
    assert worker_mem_bytes() == 4096 * 1024 * 1024
    monkeypatch.setenv("DSH_FACTOR_WORKER_MEM_MB", "1024")
    assert worker_mem_bytes() == 1024 * 1024 * 1024
    monkeypatch.setenv("DSH_FACTOR_WORKER_MEM_MB", "0")
    assert worker_mem_bytes() is None  # 0 = 关闭


def test_attach_limits_degrades_gracefully_on_dead_pid():
    """挂到已死 pid → 返回 None（降级），不抛异常。"""
    proc = _spawn("import sys; sys.exit(0)")
    proc.wait(timeout=15)
    job = attach_worker_limits(proc, cpu_s=1.0, mem_bytes=None)
    assert job is None
    detach_worker_limits(None)  # no-op 不抛
