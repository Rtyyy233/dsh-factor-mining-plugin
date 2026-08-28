# coding=utf-8
"""进程 CPU 时间测量 + 二维超时判据 + worker 并行环境（并行 P1）。

固定墙钟超时把「实现慢」与「机器忙」混为一谈——两者的正确动作相反
（修实现 vs 降并行）。本模块给出可区分的测量与预算：

- :func:`cpu_seconds`：另一进程的累计 CPU 秒（Windows GetProcessTimes /
  POSIX /proc/<pid>/stat；须在杀进程前读——进程退出后句柄即失效）
- :func:`starvation_verdict`：cpu 密集（ratio ≥ 0.7，真慢）/ 饿死
  （< 0.35，机器超订）/ 边界（保守按真慢，附数据）
- :func:`effective_wall_timeout`：墙钟上限按声明份额伸缩（D3）——
  jobs 即会话声明的 worker 数；饿死检测兜底声明不实
- :func:`omp_quiet_env`：worker 单线程 BLAS——多进程各自开满 OMP 线程时
  自旋等待空烧 CPU，会把「机器忙」伪装成「实现慢」，判据与吞吐俱毁

策略层复用同一套原语（dsh_strategy_lab 导入本模块）。
"""
from __future__ import annotations

import os

#: CPU/墙钟比值 ≥ 此值判 cpu_dense（真慢：修实现）
DENSE_RATIO = 0.70
#: CPU/墙钟比值 < 此值判 starved（机器超订：降并行/重试）
STARVED_RATIO = 0.35

DEFAULT_BASE_TIMEOUT_S = 300.0


def cpu_seconds(pid: int) -> float | None:
    """进程累计 CPU 秒（kernel+user）。读不到返回 None（调用方按 0 处理
    会误判饿死——按 None 交由调用方保守处理）。"""
    pid = int(pid)
    if pid <= 0:
        return None
    if os.name == "nt":
        return _win_cpu_seconds(pid)
    return _posix_cpu_seconds(pid)


def _win_cpu_seconds(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        creation, exit_, k, u = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                        ctypes.byref(k), ctypes.byref(u)):
            return None

        def ft_s(ft: FILETIME) -> float:
            return (((ft.hi << 32) | ft.lo)) / 1e7  # 100ns → s

        return ft_s(k) + ft_s(u)
    finally:
        kernel32.CloseHandle(h)


def _posix_cpu_seconds(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as f:
            text = f.read()
        # comm 字段可含空格（括号包裹）——用最后一个 ')' 锚定，
        # 其后依次是 state,ppid,...,utime(第12),stime(第13)
        tail = text.rsplit(")", 1)[1].split()
        if len(tail) < 13:
            return None
        tick = os.sysconf("SC_CLK_TCK")
        return (int(tail[11]) + int(tail[12])) / tick
    except Exception:
        return None


def starvation_verdict(cpu_s: float | None, wall_s: float,
                       dense_ratio: float = DENSE_RATIO,
                       starved_ratio: float = STARVED_RATIO) -> dict:
    """二维判据（D1）：CPU 密集 → 真慢（修实现）；CPU 饿死 → infra
    （重试+降并行）；边界 → 保守按真慢但附数据。cpu 读不到（None）时
    无法归因——保守按真慢（不冤枉机器，也不放跑慢实现）。"""
    cpu = float(cpu_s) if isinstance(cpu_s, (int, float)) and cpu_s >= 0 else 0.0
    wall = float(wall_s) if wall_s and wall_s > 0 else 0.0
    if wall <= 0:
        return {"class": "borderline", "cpu_s": cpu, "wall_s": wall, "ratio": None,
                "note": "墙钟非正，无法归因"}
    if cpu_s is None:
        return {"class": "cpu_dense", "cpu_s": None, "wall_s": round(wall, 3),
                "ratio": None,
                "note": "CPU 时间不可读，保守按计算密集处理"}
    ratio = cpu / wall
    if ratio >= dense_ratio:
        klass = "cpu_dense"
        note = "CPU 占比高——实现确实慢"
    elif ratio < starved_ratio:
        klass = "starved"
        note = "墙钟内几乎没分到 CPU——机器超订（并行会话挤占），不是实现问题"
    else:
        klass = "borderline"
        note = "CPU 占比居中，保守按计算密集处理（数据附上供人工判断）"
    return {"class": klass, "cpu_s": round(cpu, 3), "wall_s": round(wall, 3),
            "ratio": round(ratio, 3), "note": note}


def resolve_jobs() -> int:
    """会话声明的 worker 数（DSH_FACTOR_JOBS，默认 cpu−1）。"""
    raw = os.environ.get("DSH_FACTOR_JOBS", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, (os.cpu_count() or 2) - 1)


def effective_wall_timeout(base_s: float | None = None, jobs: int | None = None) -> float:
    """墙钟上限按声明份额伸缩（D3）：effective = base × cpu/jobs，
    clamp 到 [base, 4×base]。声明满核（jobs≈cpu）≈ base；
    声明让出一半核（jobs=cpu/2）→ ×2。仅给墙钟上限——慢代码的判定
    标准是 CPU 秒（绝对），不随此伸缩。"""
    base = float(base_s) if base_s else _base_timeout_from_env()
    j = int(jobs) if jobs else resolve_jobs()
    cpu = os.cpu_count() or 1
    scaled = base * cpu / max(1, j)
    return round(min(base * 4.0, max(base, scaled)), 3)


def _base_timeout_from_env() -> float:
    raw = os.environ.get("DSH_FACTOR_TIMEOUT", "").strip()
    if raw:
        try:
            return max(5.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_BASE_TIMEOUT_S


def omp_quiet_env() -> dict:
    """worker 进程环境：BLAS/OpenMP 单线程（多进程并行时防自旋空烧 CPU）。"""
    env = dict(os.environ)
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
              "NUMEXPR_NUM_THREADS"):
        env[k] = "1"
    return env


# ---------------------------------------------------------------------------
# OS 级硬限额（P2）：父进程轮询式超时在整机超订时父进程自己也会被饿死，
# 轮询不可靠——限额下沉到 OS（Windows Job Object / POSIX RLIMIT），
# 不依赖父进程被调度，到限额确定性击杀。
# ---------------------------------------------------------------------------

#: worker 内存限额（MB；0 = 关闭）。初值保守：面板 + evaluate 峰值远小于此，
#: 只拦「膨胀因子把整机拖进 swap 饿死所有并行会话」的死亡螺旋。
DEFAULT_WORKER_MEM_MB = 4096


def worker_mem_bytes() -> int | None:
    raw = os.environ.get("DSH_FACTOR_WORKER_MEM_MB", "").strip()
    if raw:
        try:
            mb = int(float(raw))
            return mb * 1024 * 1024 if mb > 0 else None
        except ValueError:
            pass
    return DEFAULT_WORKER_MEM_MB * 1024 * 1024


def _win_apply_job(proc, cpu_s: float | None, mem_bytes: int | None):
    """Windows：把子进程挂进 Job Object（CPU 上限 + 内存上限 +
    KILL_ON_JOB_CLOSE 兜底父崩溃孤儿）。返回 job 句柄——调用方须在
    子进程结束后 close_job()；句柄意外关闭（父崩溃/GC）时 OS 直接
    杀掉 job 内全部进程（这正是兜底语义）。失败返回 None（优雅降级
    到父进程侧超时，互斥性不受影响）。"""
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOperationCount", "WriteOperationCount",
                     "OtherOperationCount", "ReadTransferCount",
                     "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_ulonglong),
            ("PerJobUserTimeLimit", ctypes.c_ulonglong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    try:
        kernel32 = ctypes.windll.kernel32
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED_LIMITS()
        # CPU 上限用 JOB_TIME 而非 PROCESS_TIME：嵌套 job 环境（进程已被
        # 宿主放进 job 时，如 IDE/harness 会话）对 PROCESS_TIME 的 assign
        # 报 ERROR_NOT_ENOUGH_QUOTA（实测 2026-08-27），JOB_TIME 可挂；
        # 本 job 单进程，两者语义等价（job 内累计 user 时间到顶即全杀）
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if cpu_s and cpu_s > 0:
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_JOB_TIME
            # 100ns 单位（user 时间；kernel 占比可忽略）
            info.BasicLimitInformation.PerJobUserTimeLimit = int(cpu_s * 1e7)
        if mem_bytes and mem_bytes > 0:
            info.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(mem_bytes)
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE,
                                 False, proc.pid)
        if not h:
            kernel32.CloseHandle(job)
            return None
        try:
            # Win8+ 允许嵌套 assign；失败（旧系统/权限）→ 降级
            if not kernel32.AssignProcessToJobObject(job, h):
                kernel32.CloseHandle(job)
                return None
        finally:
            kernel32.CloseHandle(h)
        return job
    except Exception:
        return None


def _win_close_job(job) -> None:
    if job:
        try:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(job)
        except Exception:
            pass


def posix_limit_preexec(cpu_s: float | None = None,
                        mem_bytes: int | None = None):
    """POSIX：preexec_fn（fork 后 exec 前）设 RLIMIT_CPU / RLIMIT_AS。
    RLIMIT_AS 对 numpy 大页预留偶有误伤——内存限额可经
    DSH_FACTOR_WORKER_MEM_MB=0 关闭。返回 callable 或 None。"""
    if os.name == "nt":
        return None

    def _apply():
        import resource

        if cpu_s and cpu_s > 0:
            cpu = int(max(1, round(cpu_s)))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
        if mem_bytes and mem_bytes > 0:
            resource.setrlimit(resource.RLIMIT_AS, (int(mem_bytes),) * 2)

    return _apply


def attach_worker_limits(proc, cpu_s: float | None = None,
                         mem_bytes: int | None = None):
    """给刚 spawn 的 worker 子进程挂 OS 级限额（跨平台入口）。

    Windows 返回 job 句柄（结束后传 detach_worker_limits 关闭）；
    POSIX 返回 None（限额经 preexec_fn 在 spawn 前设置——用
    posix_limit_preexec 传入 Popen）。失败一律优雅降级：父进程侧
    墙钟超时仍在，互斥性与零写入语义不受影响。"""
    if os.name == "nt":
        return _win_apply_job(proc, cpu_s, mem_bytes)
    return None


def detach_worker_limits(job) -> None:
    """子进程结束后释放 job 句柄（job 内已无进程，关闭无副作用；
    句柄被意外关闭时 KILL_ON_JOB_CLOSE = 杀 job 内残余——兜底语义）。"""
    if os.name == "nt":
        _win_close_job(job)
