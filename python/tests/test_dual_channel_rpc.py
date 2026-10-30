# coding=utf-8
"""2026-09-18 双通道事故回归锁（INC-20260918，三连 900s 超时）。

根因一：faba176 读写分离重构后，handle_one 的错误分支把 _rpc_error 的
已序列化字符串交给 emit 再 json.dumps——错误响应双重编码成 JSON 字符串
字面量，TS 端按 id 配对 pending 失败，一切报错请求变成客户端 900s 超时
（成功响应走 dict 分支不受影响：轻请求秒回、报错请求全挂的分裂症状）。

根因二：worker 子进程继承桥的 stdin（JSON-RPC 管道），CPython 3.14
解释器初始化在该句柄上的 stdio 探测永久阻塞（零 CPU 冻结）——烟测门
60s 误判「死循环」，平凡全向量化因子也被拒，于是每个因子请求都以
错误收场、再被根因一变成 900s 挂起。

测试：
1. 集成（真实 seam：起真桥进程，stdio 上的 JSON-RPC）——错误响应行
   必须是 json.loads 后带匹配 id 的 dict，且能被后续请求继续配对；
2. 静态锁——_run_worker 的 Popen 必须显式 stdin=DEVNULL。
"""
import ast
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _drain_until(proc, out_lines, predicate, timeout_s=60.0):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        while out_lines:
            ln = out_lines.pop(0)
            if not ln.strip():
                continue
            try:
                msg = json.loads(ln)
            except Exception:
                continue
            if predicate(msg):
                return msg
        if proc.poll() is not None:
            raise AssertionError(f"bridge 提前退出 rc={proc.returncode}")
        time.sleep(0.05)
    raise AssertionError("等待响应超时")


@pytest.fixture()
def live_bridge(tmp_path):
    env = {"PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8",
           "PATH": ""}
    proc = subprocess.Popen(
        [sys.executable, "-m", "dsh_factor_mining.bridge",
         "--state-root", str(tmp_path / "root")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        cwd=str(tmp_path), env=env)
    lines = []

    def rd():
        for ln in proc.stdout:
            lines.append(ln.rstrip("\n"))

    threading.Thread(target=rd, daemon=True).start()
    try:
        yield proc, lines
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)


def test_error_responses_are_id_pairable_dicts(live_bridge):
    """错误响应必须是 dict 且 id 匹配——双编码回归的直接断言。

    未知方法（-32601）与域参数错误（-32602）分别覆盖写道与 dispatch
    内的 BridgeError 路径；请求 1 成功后再发错误请求，验证通道未被
    错误破坏（事故形态：错误后一切请求假死）。
    """
    proc, lines = live_bridge

    def is_ready(m):
        return isinstance(m, dict) and m.get("method") == "ready"

    _drain_until(proc, lines, is_ready)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    # 成功请求先走一遍（status 走读快道）
    send({"jsonrpc": "2.0", "id": 1, "method": "status", "params": {}})
    r1 = _drain_until(proc, lines, lambda m: m.get("id") == 1)
    assert isinstance(r1, dict) and r1.get("id") == 1 and "result" in r1

    # 未知方法 → -32601（写道，BridgeError 分支）
    send({"jsonrpc": "2.0", "id": 2, "method": "no.such.method", "params": {}})
    r2 = _drain_until(proc, lines, lambda m: m.get("id") == 2)
    assert isinstance(r2, dict) and r2.get("id") == 2, (
        "错误响应不是可按 id 配对的 dict——疑似双重编码回归"
        f"（实际类型 {type(r2).__name__}）")
    assert r2.get("error", {}).get("code") == -32601

    # 域错误（参数缺失）→ BridgeError -3260x 分支
    send({"jsonrpc": "2.0", "id": 3, "method": "factor.check_causality",
          "params": {}})
    r3 = _drain_until(proc, lines, lambda m: m.get("id") == 3)
    assert isinstance(r3, dict) and r3.get("id") == 3
    assert "error" in r3 and r3["error"].get("code") < 0

    # 错误之后通道仍然活着
    send({"jsonrpc": "2.0", "id": 4, "method": "status", "params": {}})
    r4 = _drain_until(proc, lines, lambda m: m.get("id") == 4)
    assert isinstance(r4, dict) and "result" in r4


def test_worker_spawn_stdin_devnull_static():
    """_run_worker 的 Popen 必须显式 stdin=DEVNULL（静态锁）。

    worker 继承桥的 stdin 会在 CPython 3.14 解释器初始化期冻结
    （py-spy 栈：Py_Initialize→fflush/lseek→ZwQueryInformationFile，
    零 CPU 永久阻塞）——烟测门因此把一切因子误判为死循环。静态断言
    防止未来重构无声去掉该参数。
    """
    src = (SRC / "dsh_factor_mining" / "bridge.py").read_text(encoding="utf-8")
    found = False
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        # 找 subprocess.Popen 调用
        f = node.func
        is_popen = (isinstance(f, ast.Attribute) and f.attr == "Popen")
        if not is_popen:
            continue
        for kw in node.keywords:
            if kw.arg == "stdin":
                val = kw.value
                assert (isinstance(val, ast.Attribute)
                        and val.attr == "DEVNULL"), (
                    "worker Popen 的 stdin 必须是 subprocess.DEVNULL——"
                    "继承桥的 RPC 管道会在 CPython 3.14 初始化期冻结"
                    "（INC-20260918 根因二）")
                found = True
    assert found, "未在 bridge.py 中找到带显式 stdin= 的 subprocess.Popen"
