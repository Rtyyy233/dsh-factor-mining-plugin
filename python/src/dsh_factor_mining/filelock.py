# coding=utf-8
"""跨进程状态写锁（建议锁）+ PID 存活检测。

并行化地基（2026-08-27 规划 P0）：tmp+os.replace 只保证单次写不产生
半截文件，不防丢更新——两个进程同时「读全量→追加→重写」会互相覆盖
丢条目（丢的恰是 deflation 记账依据）。本模块提供 stateRoot 级独占写锁：

- Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``（建议锁：只约束走
  本代码的进程；外部进程直改状态文件不受保护——已知限制，见规划书）
- 锁文件 ``stateRoot/.write.lock``；持锁者 pid+ts 写入文件供超时诊断
- 同进程可重入（bridge/CLI 单线程，防 offer→save 之类的误嵌套自锁死）
- 策略层复用同一原语（dsh_strategy_lab 导入本模块，锁住自己的 root）

规则：**锁内读-改-写，锁外只读**（读走原子替换的快照语义，无需锁）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_LOCK_TIMEOUT_S = 10.0

# 进程内重入计数（单线程假设：bridge/CLI 均无并发线程写状态）
_HELD: dict[str, int] = {}


class LockTimeoutError(RuntimeError):
    """写锁获取超时——可行动错误（谁持锁、怎么处理）。"""


def pid_alive(pid: int) -> bool:
    """PID 是否存活（跨平台；word-boundary 匹配防 PID 5 误配内存列）。"""
    pid = int(pid)
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            # 字节模式 + 容错解码：tasklist 输出是本地码页（GBK 等），
            # 强制 UTF-8 环境下 text=True 会抛 UnicodeDecodeError；
            # PID 匹配只依赖 ASCII 数字，容错解码不影响判定
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                               capture_output=True, timeout=10)
            return re.search(rf"(?<!\d){pid}(?!\d)",
                             r.stdout.decode("utf-8", errors="replace")) is not None
        except Exception:
            return False  # 查询失败按存活处理（保守：不误回收活锁）
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_holder(lock_path: Path) -> dict:
    """读持锁者登记（诊断用；锁外读，损坏容错）。

    登记写 sidecar（lock_path + '.holder'）而非锁文件本身——Windows
    msvcrt 字节锁是强制锁，锁内区域连读都被拒，写锁文件会让自己
    的超时诊断读不到（实测 PermissionError）。"""
    try:
        sidecar = lock_path.parent / (lock_path.name + ".holder")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _try_lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if sys.platform == "win32":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def state_write_lock(state_root, timeout_s: float = DEFAULT_LOCK_TIMEOUT_S):
    """stateRoot 级独占写锁。超时抛 LockTimeoutError（带持锁者信息）。"""
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    key = str(root.resolve())
    if _HELD.get(key, 0) > 0:
        _HELD[key] += 1
        try:
            yield
        finally:
            _HELD[key] -= 1
        return
    lock_path = root / ".write.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    deadline = time.monotonic() + float(timeout_s)
    try:
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = _read_holder(lock_path)
                    hpid = holder.get("pid")
                    hint = (f"持锁者 PID={hpid}（{holder.get('ts', '?')}）"
                            if hpid else "持锁者未知")
                    raise LockTimeoutError(
                        f"stateRoot '{key}' 写锁等待超过 {timeout_s:.0f}s——{hint}。"
                        "另一进程正长时间持有写锁：检查是否有僵死 bridge/CLI 进程"
                        f"（确认已崩溃可手动删除 {lock_path} 后重试）；"
                        "正常并发写入是毫秒级，不应触顶。")
                time.sleep(0.05)
        # 持锁者登记（锁内写 sidecar，无竞争；供他人超时诊断——不写锁文件
        # 本身：msvcrt 强制锁下锁内区域连读都被拒）
        try:
            sidecar = lock_path.parent / (lock_path.name + ".holder")
            sidecar.write_text(json.dumps(
                {"pid": os.getpid(),
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        except OSError:
            pass  # 登记失败不影响互斥
        _HELD[key] = 1
        try:
            yield
        finally:
            _HELD[key] = 0
            _unlock(fd)
    finally:
        os.close(fd)
