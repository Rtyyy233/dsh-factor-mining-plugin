# coding=utf-8
"""C1/C2（修正案 A，PLAN-worker-timeout-guidance §6，2026-08-28）：

C1 evaluate 结果缓存——worker.run_request 直接调用（与 test_batch_pool_p3
同模式）：同源二次评估零重算、numpy 标量读回同型、口径/env 指纹不同
不命中、composite 的成分命中（(K+1)× 全量评估降到 1× 新鲜计算）。
C2 重调用异步化 + run 目录收尸——bridge 级（复用 test_bridge_smoke 的
合成数据桥）：async 提交 → job_id 轮询 → 结果取回；成功 run 目录留
out.json/job.json、env.npz 已删；有界清理；status 暴露 workerRuns。
"""
from __future__ import annotations

import json
import os
import time
import types
from pathlib import Path

import numpy as np
import pytest

from dsh_factor_mining.bridge import BridgeError
from dsh_factor_mining.worker import run_request, write_env_npz


# ---- 夹具（照抄 test_batch_pool_p3 的合成 env）----

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


def _src(seed, win=1):
    return (f"""
import numpy as np

def factor(env):
    prev = np.roll(env.c, {win}, axis=0)
    prev[:{win}] = np.nan
    return np.nan_to_num(env.c / prev - 1.0, nan=0.0)
""")


# ---- C1 单元：cached_evaluate 的命中/归一/口径隔离 ----

def test_cached_evaluate_hit_and_roundtrip(tmp_path, monkeypatch):
    from dsh_factor_mining.factor import eval_cache as ec

    calls = []

    def fake(F, env, **kw):
        calls.append(kw)
        return {"ic_ir_train": 0.5, "n": np.int64(7), "f": np.float64(0.25),
                "b": np.bool_(True), "ic": [np.float64(0.1), np.float64(0.2)]}

    env = types.SimpleNamespace(calibration=types.SimpleNamespace(dev_end="2024-01-01"))
    kw = dict(state_root=str(tmp_path), source_hash="h1", env_fp="fp1",
              _evaluate=fake)
    r1 = ec.cached_evaluate(None, env, **kw)
    r2 = ec.cached_evaluate(None, env, **kw)
    assert len(calls) == 1                                  # 二次零重算
    assert r2["eval_cache"] == "hit" and r2["ic_ir_train"] == 0.5
    # numpy 标量归一：读回与新鲜结果在 JSON 层同型
    assert isinstance(r2["n"], int) and not isinstance(r2["n"], bool)
    assert isinstance(r2["f"], float) and isinstance(r2["b"], bool)
    assert r2["ic"] == [0.1, 0.2]
    # 口径不同（horizon）→ 不命中
    ec.cached_evaluate(None, env, horizon=5, **kw)
    assert len(calls) == 2
    # env 指纹不同（换数据）→ 不命中
    ec.cached_evaluate(None, env, env_fp="fp2", **{k: v for k, v in kw.items()
                                                   if k != "env_fp"})
    assert len(calls) == 3
    # 无 state_root → 纯直通（不读不写缓存）
    r5 = ec.cached_evaluate(None, env, state_root=None, source_hash=None,
                            _evaluate=fake)
    assert len(calls) == 4 and "eval_cache" not in r5


# ---- C1 worker 级：factor.evaluate 二次调用走缓存 ----

def test_worker_evaluate_cache_hit(tmp_path, monkeypatch):
    from dsh_factor_mining.factor import eval_cache as ecmod

    env = _env()
    npz = tmp_path / "env.npz"
    write_env_npz(str(npz), env)
    real = ecmod.evaluate
    calls = []

    def counting(F, env_, **kw):
        calls.append(1)
        return real(F, env_, **kw)

    monkeypatch.setattr(ecmod, "evaluate", counting)
    req = {"npzPath": str(npz), "method": "factor.evaluate",
           "source": _src(1),
           "params": {"stage": "development",
                      "state_root": str(tmp_path / "state"),
                      "source_hash": "hashA", "env_fp": "fpA"}}
    r1 = run_request(json.loads(json.dumps(req)))
    r2 = run_request(json.loads(json.dumps(req)))
    assert len(calls) == 1                                  # evaluate 本体只算一次
    assert r2.get("eval_cache") == "hit"
    assert r1["ic_ir_train"] == r2["ic_ir_train"]
    assert (tmp_path / "state" / "eval_cache.json").exists()
    assert "perf" in r2                                     # W2 遥测不因命中丢失


# ---- C1 worker 级：composite 成分命中（(K+1)× → 1×）----

def test_worker_composite_parts_cache(tmp_path, monkeypatch):
    from dsh_factor_mining.factor import eval_cache as ecmod

    env = _env()
    npz = tmp_path / "env.npz"
    write_env_npz(str(npz), env)
    st = str(tmp_path / "state")
    real = ecmod.evaluate
    calls = []

    def counting(F, env_, **kw):
        calls.append(1)
        return real(F, env_, **kw)

    monkeypatch.setattr(ecmod, "evaluate", counting)
    part_a, part_b = _src(11, win=1), _src(12, win=5)

    def comp_req(src, h):
        return {"npzPath": str(npz), "method": "factor.evaluate_composite",
                "source": src,
                "params": {"ingredients": {"a": part_a, "b": part_b},
                           "state_root": st, "source_hash": h, "env_fp": "fpX"}}

    r1 = run_request(comp_req(_src(21, win=10), "comp1"))
    assert len(calls) == 3                                  # 合成 + 两成分
    r2 = run_request(comp_req(_src(22, win=20), "comp2"))
    assert len(calls) == 4                                  # 只有新合成本体新鲜算
    assert r2["parts"]["a"].get("eval_cache") == "hit"
    assert r2["parts"]["b"].get("eval_cache") == "hit"
    assert "eval_cache" not in r2["composite"]
    # 洋葱增量数字与第一次同源成分一致（缓存值 = 新鲜值）
    assert r1["parts"]["a"]["ic_ir_train"] == r2["parts"]["a"]["ic_ir_train"]


# ---- C2：run 目录有界保留 + status 暴露 ----

def test_bridge_evaluate_precheck_zero_subprocess(tmp_path, monkeypatch):
    """任务书验收 #1：同 source 二次 evaluate——桥级预检命中，零子进程
    （个股面板每次省 npz 序列化 + 进程 spawn + env 重载的几十秒）。"""
    from test_bridge_smoke import _make_bridge

    # N=40：评估管线有最小池门（N=20 会 error——错误结果不缓存是正确
    # 行为，但这里要验证真实数字的缓存往返）
    bridge, _ = _make_bridge(tmp_path, T=400, N=40, seed=5)
    bridge.dispatch("data.load", {"envId": "primary"})
    calls = []
    real = bridge._run_worker

    def counting(method, source, params, env, **kw):
        calls.append(method)
        return real(method, source, params, env, **kw)

    monkeypatch.setattr(bridge, "_run_worker", counting)
    p = {"envId": "primary", "source": _src(41, win=5),
         "stage": "development", "async": False}   # 层 0c：同步语义测试走逃生阀
    r1 = bridge.dispatch("factor.evaluate", dict(p))
    assert not r1.get("error"), r1.get("error")             # 真实数字，非 error 结果
    # 命中语义：bar_sigma（E[max|X|] 门参数）随评估后的入池动作一次性
    # 漂移 → 第 2 次仍 miss（键含门参数=统计完整性），第 3 次起稳定命中
    r2 = bridge.dispatch("factor.evaluate", dict(p))
    r3 = bridge.dispatch("factor.evaluate", dict(p))
    n_after_3 = len(calls)
    r4 = bridge.dispatch("factor.evaluate", dict(p))
    assert len(calls) == n_after_3                           # 稳态零子进程
    assert r3.get("eval_cache") == "hit" and r4.get("eval_cache") == "hit"
    assert r1["ic_ir_train"] == r3["ic_ir_train"]           # 缓存值=新鲜值


def test_prune_and_recent_runs(tmp_path):
    from dsh_factor_mining.bridge import Bridge

    br = Bridge(state_root=str(tmp_path / "state"))
    root = tmp_path / "state" / "worker_runs"
    root.mkdir(parents=True)
    for i in range(25):
        d = root / f"run{i:02d}"
        d.mkdir()
        jp = d / "job.json"
        jp.write_text(json.dumps({
            "job_id": f"j{i:02d}", "method": "factor.evaluate_batch",
            "status": "done", "submitted_at": f"2026-08-28T00:{i:02d}:00"}),
            encoding="utf-8")
        os.utime(jp, (2000 + i, 2000 + i))                  # mtime 决定保留序
    br._prune_kept_runs()
    remaining = sorted(p.name for p in root.iterdir())
    assert len(remaining) == 20 and "run00" not in remaining and "run24" in remaining
    runs = br._recent_runs()
    assert runs and runs[0]["jobId"] == "j24"
    st = br._status({})
    assert any(r["jobId"] == "j24" for r in st["workerRuns"])


# ---- C2 bridge 级：async 提交 → job_id 轮询 → 收尸 ----

def test_bridge_async_composite_roundtrip(tmp_path):
    from test_bridge_smoke import _make_bridge

    bridge, _ = _make_bridge(tmp_path, T=400, N=40, seed=9)  # N=40：过最小池门
    bridge.dispatch("data.load", {"envId": "primary"})
    comp = _src(31, win=10)
    sub = bridge.dispatch("factor.evaluate_composite",
                          {"envId": "primary", "source": comp,
                           "ingredients": {"a": _src(32, win=1),
                                           "b": _src(33, win=5)},
                           "async": True})
    jid = sub["job_id"]
    assert sub["status"] == "submitted"
    got = {"status": "running"}
    for _ in range(120):                                    # ≤120s 轮询
        got = bridge.dispatch("factor.evaluate_composite",
                              {"job_id": jid})              # 纯取回：无需 env/source
        if got.get("status") != "running":
            break
        time.sleep(1.0)
    assert got.get("status") != "running", "异步作业 120s 未完成"
    assert "composite" in got and "onion" in got and "diagnosis" in got
    assert not got["composite"].get("error"), got["composite"].get("error")
    # 收尸三要素：out.json 在、env.npz 删、job.json done；status 可见
    d, j = bridge._find_run(jid)
    assert (d / "out.json").exists() and not (d / "env.npz").exists()
    assert j["status"] == "done" and j["mode"] == "async"
    st = bridge.dispatch("status", {})
    assert any(r["jobId"] == jid and r["status"] == "done" for r in st["workerRuns"])
    # 轻方法目录零残留（smoke 断言的语义不回归）：残留目录必带 job.json
    leftovers = [p for p in Path(bridge.state_root, "worker_runs").iterdir()
                 if p.is_dir()]
    assert all((p / "job.json").exists() for p in leftovers)


# ---- C2 延伸（2026-08-29 生产事故）：audit 计时 / 异步 / 消息分流 ----

def test_audit_perf_attached(tmp_path):
    """audit 结果必须带 fn 计时——超时时 agent 靠它分清「因子慢」与
    「审计机器重」（当日生产：evaluate 6.5s vs audit 315s CPU，agent
    被旧消息误导重写两遍源码）。"""
    env = _env()
    npz = tmp_path / "env.npz"
    write_env_npz(str(npz), env)
    req = {"npzPath": str(npz), "method": "factor.audit", "source": _src(51, win=3),
           "params": {"state_root": None}}
    out = run_request(req)
    assert out["verdict"] in ("PASS", "FAIL"), out.get("verdict")
    perf = out["perf"]
    assert perf["factor_calls"] >= 1                    # 因果探针可能多次调用 fn
    assert perf["factor_cpu_s"] >= 0.0
    assert "列置换" in perf["machinery_note"]


def test_audit_async_roundtrip(tmp_path):
    from test_bridge_smoke import _make_bridge

    bridge, _ = _make_bridge(tmp_path, T=400, N=40, seed=11)
    bridge.dispatch("data.load", {"envId": "primary"})
    sub = bridge.dispatch("factor.audit",
                          {"envId": "primary", "source": _src(52, win=3),
                           "async": True})
    jid = sub["job_id"]
    got = {"status": "running"}
    for _ in range(120):
        got = bridge.dispatch("factor.audit", {"job_id": jid})
        if got.get("status") != "running":
            break
        time.sleep(1.0)
    assert got.get("status") != "running", "异步 audit 120s 未完成"
    assert got["verdict"] in ("PASS", "FAIL")           # 裸结果（audit 同步路径同形）
    assert got["perf"]["factor_cpu_s"] >= 0.0
    d, j = bridge._find_run(jid)
    assert (d / "out.json").exists() and j["status"] == "done"


def test_timeout_message_method_aware(tmp_path, monkeypatch):
    """超时根因按方法分流：evaluate 保持「向量化因子」；audit 类改说
    「机器成本，勿重写因子」——2026-08-29 生产事故的直接教训。"""
    import subprocess as sp
    from test_timeout_guidance import _make_bridge as _make_tb

    b = _make_tb(tmp_path, execution_mode="worker")

    class _FakeProc:
        pid = 424242
        returncode = None

        def communicate(self, timeout=None):
            raise sp.TimeoutExpired(cmd=["worker"], timeout=300)

    monkeypatch.setattr(sp, "Popen", lambda *a, **k: _FakeProc())
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.audit",
                   {"envId": "primary", "source": _src(53, win=3)})
    msg = ei.value.message
    assert ei.value.code == -32005
    assert "factor.audit" in msg
    assert "根因分流" in msg and "勿重写因子" in msg
    assert "per-symbol Python 循环" not in msg   # evaluate 专属误导不再出现
    assert "可立即重试" in msg and "零写入" in msg       # 四要素仍在


# ---- 方案 C（2026-08-29 用户拍板）：submit 五门并行 ----

def test_run_gates_parallel_concurrency_and_exception_order(tmp_path):
    """五门并发：墙钟 = max(门) 而非求和（个股面板顺序总和必超客户端
    360s 期限）；全部完成后才返回（异常由调用方按原判定序合并——助手
    只负责并发，不吞不改）。"""
    from dsh_factor_mining.bridge import Bridge

    br = Bridge(state_root=str(tmp_path / "state"))
    events = []

    def mk(name, dur, exc=None):
        def gate():
            events.append((name, "start"))
            time.sleep(dur)
            events.append((name, "end"))
            if exc:
                raise exc
            return name
        return gate

    t0 = time.monotonic()
    futs = br._run_gates_parallel({
        "noise": mk("noise", 0.5),
        "dayperm": mk("dayperm", 0.5),
        "flatness": mk("flatness", 0.5),
        "spread": mk("spread", 0.5),
        "placebo": mk("placebo", 0.5, exc=RuntimeError("infra")),
    })
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, elapsed                  # 串行 2.5s；并行 ≈0.5s
    assert futs["noise"].result() == "noise"       # 好门结果保真
    with pytest.raises(RuntimeError):
        futs["placebo"].result()                   # 坏门异常保真
    assert sum(1 for _, k in events if k == "end") == 5   # 全部完成才返回
