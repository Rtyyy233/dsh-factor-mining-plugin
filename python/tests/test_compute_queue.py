# coding=utf-8
"""计算资源利用层 0-3 回归 — 2026-08-31 计算利用 PLAN。

锁定行为：
- 层 1：权重槽 + 真队列（槽满入队不拒绝，Q 满才拒绝）+ job.json 入队
  即落盘（R5）
- 层 2：在途去重（同参数复用同一 job_id，不重复计算）
- 层 3：lane 公平出队
- R6：启动收尸（死进程的 queued/running → failed）
- 层 0b：env.npz 指纹缓存（第二次评估不再重写 npz）
- 层 0c：worker 模式 evaluate 默认异步（in_process 恒同步；缓存命中
  同步直接返回 diagnosis——R2 契约分叉）
- R1 配套：worker 墙钟默认 600s（env 可覆盖）

队列机制用 monkeypatch 假 worker（不 spawn 子进程，毫秒级可控完成）；
evaluate 异步契约用真实 worker e2e（合成面板）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.lane import reset_request_lane, set_request_lane  # noqa: E402


def _make_bridge(root: Path, mode: str = "in_process") -> Bridge:
    rng = np.random.default_rng(4)
    T, N = 1200, 40
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = root / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(root / "state"), execution_mode=mode)
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    return b


class _FakeWorkerController:
    """替换 _run_worker：可控的假作业（毫秒级完成/手动放行）。"""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.calls: list[dict] = []
        self.block = True   # True：作业挂起直到 release()
        self.result_override: dict | None = None  # 测试注入批信封等真实形状
        self._gates: list = []
        self._orig = bridge._run_worker

    def install(self):
        self.bridge._run_worker = self._fake  # type: ignore[assignment]
        return self

    def _fake(self, method, source, params, env, run_id=None, run_meta=None,
              async_mode=False, run_dir=None):
        rec = {"run_id": run_id, "method": method, "source": source,
               "lane": (run_meta or {}).get("lane"),
               "started": time.monotonic()}
        self.calls.append(rec)
        if self.block:
            gate = rec["gate"] = __import__("threading").Event()
            self._gates.append(gate)
            gate.wait(timeout=30)
        out = Path(run_dir) / "out.json"
        res = (self.result_override if self.result_override is not None
               else {"fake": True, "method": method, "run_id": run_id})
        # request.json 与真 _run_worker 同款（异步 batch 轮询侧从它还原
        # sources/horizon 做后处理——R24）
        if run_dir is not None:
            (Path(run_dir) / "request.json").write_text(json.dumps(
                {"method": method, "source": source, "params": params},
                ensure_ascii=False), encoding="utf-8")
        out.write_text(json.dumps(
            {"ok": True, "result": res}), encoding="utf-8")
        # 与真 _run_worker 同款收尾：job.json 落 done 墓志（否则轮询方
        # 永远看到 running）；run_dir=None 的内部同步调用无墓志可写
        if run_id and run_dir is not None:
            try:
                self.bridge._finalize_kept_run(Path(run_dir), run_id, method)
            except Exception:
                pass
        return {"fake": True, "run_id": run_id}

    def release_all(self):
        for g in self._gates:
            g.set()

    def restore(self):
        self.bridge._run_worker = self._orig  # type: ignore[assignment]


def _submit(bridge, source, lane=None, method="factor.evaluate"):
    params = {"envId": "primary", "source": source, "stage": "development"}
    p = {**params, **({"lane": lane} if lane else {})}
    tok = set_request_lane(lane)
    try:
        return bridge._async_submit(method, source, p, None, "primary",
                                    extra_meta={"stage": "development"})
    finally:
        reset_request_lane(tok)


# ---- 层 1：权重槽 + 队列 ----

def test_slots_and_queue_not_reject(tmp_path):
    b = _make_bridge(tmp_path)
    b._slot_capacity = 2          # 2 个 evaluate 当量
    b._queue_capacity = 5
    ctl = _FakeWorkerController(b).install()
    try:
        r1 = _submit(b, "s1")     # 权重 1 → 立即启动
        r2 = _submit(b, "s2")     # 权重 1 → 立即启动（槽满）
        r3 = _submit(b, "s3")     # → 入队
        assert r1["status"] == "submitted"
        assert r2["status"] == "submitted"
        assert r3["status"] == "queued" and r3["queue_position"] == 1
        # job.json 入队即落盘（R5）：轮询不扑空，状态可见
        jp = Path(b.state_root) / "worker_runs" / r3["job_id"] / "job.json"
        assert jp.exists()
        assert json.loads(jp.read_text(encoding="utf-8"))["status"] == "queued"
        # 轮询 queued 返回位置
        poll = b._async_result(r3["job_id"])
        assert poll["status"] == "queued" and poll["queue_position"] == 1
        # 释放一个槽 → 队列被泵起
        ctl.release_all()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(ctl.calls) >= 3:
                break
            time.sleep(0.05)
        assert len(ctl.calls) == 3, f"队列未泵起: {[c['run_id'] for c in ctl.calls]}"
        started = {c["run_id"] for c in ctl.calls}
        assert r3["job_id"] in started
    finally:
        ctl.release_all()
        ctl.restore()


def test_queue_capacity_rejects(tmp_path):
    b = _make_bridge(tmp_path)
    b._slot_capacity = 1
    b._queue_capacity = 1
    ctl = _FakeWorkerController(b).install()
    try:
        _submit(b, "s1")                    # 启动
        _submit(b, "s2")                    # 入队（Q=1 满）
        import pytest
        from dsh_factor_mining.bridge import BridgeError
        with pytest.raises(BridgeError) as ei:
            _submit(b, "s3")
        assert "队列已满" in str(ei.value)
    finally:
        ctl.release_all()
        ctl.restore()


def test_batch_weight_reserves_more_slots(tmp_path):
    b = _make_bridge(tmp_path)
    b._slot_capacity = 4
    ctl = _FakeWorkerController(b).install()
    try:
        _submit(b, "e1")                                   # w=1 启动 (1/4)
        _submit(b, "e2")                                   # w=1 启动 (2/4)
        r = _submit(b, "b1", method="factor.evaluate_batch")  # w=4 > 剩 2 → 排队
        assert r["status"] == "queued"
        # evaluate 小作业仍能启动（防大作业头部阻塞）
        r2 = _submit(b, "e3")                              # w=1 → (3/4) 启动
        assert r2["status"] == "submitted"
    finally:
        ctl.release_all()
        ctl.restore()


# ---- 层 2：在途去重 ----

def test_inflight_dedup_same_source_reuses_job(tmp_path):
    b = _make_bridge(tmp_path)
    b._slot_capacity = 4
    ctl = _FakeWorkerController(b).install()
    try:
        r1 = _submit(b, "same-source")
        r2 = _submit(b, "same-source")     # 同参数在途 → 复用
        assert r2.get("dedup") is True
        assert r2["job_id"] == r1["job_id"]
        # 不同 source → 新作业
        r3 = _submit(b, "other-source")
        assert r3["job_id"] != r1["job_id"]
        # 完成后去重表释放（重跑走新作业/eval_cache）
        ctl.release_all()
        _wait_done(b, r1["job_id"])
        assert not b._async_dedup, f"去重表未清空: {b._async_dedup}"
    finally:
        ctl.release_all()
        ctl.restore()


def _wait_done(b, job_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jp = Path(b.state_root) / "worker_runs" / job_id / "job.json"
        try:
            if json.loads(jp.read_text(encoding="utf-8"))["status"] in ("done", "failed"):
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


# ---- 层 3：lane 公平出队 ----

def test_lane_fair_pump(tmp_path):
    b = _make_bridge(tmp_path)
    b._slot_capacity = 1
    ctl = _FakeWorkerController(b).install()
    try:
        _submit(b, "a1", lane="laneA")       # 启动（laneA 最近）
        _submit(b, "a2", lane="laneA")       # 队列
        _submit(b, "b1", lane="laneB")       # 队列
        ctl.release_all()                    # a1 完成 → pump：应先启动 laneB
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(ctl.calls) < 2:
            time.sleep(0.05)
        assert len(ctl.calls) >= 2
        second = ctl.calls[1]
        assert second["lane"] == "laneB", \
            f"公平出队失效：第二个启动的是 {second['lane']}"
    finally:
        ctl.release_all()
        ctl.restore()


# ---- R6：启动收尸 ----

def test_reap_orphan_jobs_on_boot(tmp_path):
    b = _make_bridge(tmp_path)
    wr = Path(b.state_root) / "worker_runs"
    dead_pid = _find_dead_pid()
    for name, status in (("job-x1", "running"), ("job-x2", "queued"),
                         ("job-x3", "done")):
        d = wr / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps({
            "job_id": name, "method": "factor.evaluate", "status": status,
            "pid": dead_pid}, ensure_ascii=False), encoding="utf-8")
    # 同 pid 的活进程作业不收（本进程）
    d = wr / "job-x4"
    d.mkdir(exist_ok=True)
    (d / "job.json").write_text(json.dumps({
        "job_id": "job-x4", "method": "factor.evaluate",
        "status": "running", "pid": _pid()}, ensure_ascii=False),
        encoding="utf-8")

    b2 = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b2._reap_orphan_jobs()
    def st(name):
        return json.loads((wr / name / "job.json").read_text(encoding="utf-8"))["status"]
    assert st("job-x1") == "failed" and "收尸" in json.loads(
        (wr / "job-x1" / "job.json").read_text(encoding="utf-8"))["error"]
    assert st("job-x2") == "failed"
    assert st("job-x3") == "done"      # 终态不动
    assert st("job-x4") == "running"   # 活进程不动


def _pid() -> int:
    import os
    return os.getpid()


def _find_dead_pid() -> int:
    import subprocess
    # 起一个立刻退出的进程拿 pid
    p = subprocess.run(["python", "-c", "pass"], capture_output=True)
    # 用一个大概率已回收的 pid：启动子进程它已退出——pid 复用风险测试可忽略
    return 999999   # Windows pid 空间大，999999 存活概率≈0


# ---- 层 0b：env.npz 指纹缓存 ----

def test_env_npz_cached_by_fingerprint(tmp_path):
    b = _make_bridge(tmp_path)   # in_process：_cached_env_npz 仍被 _run_worker 用
    # in_process 不走 _run_worker；直接调用缓存方法验证
    env = b.envs.get("primary")
    assert env is not None
    we = Path(b.state_root) / "worker_env"
    p1 = b._cached_env_npz(env, {"envId": "primary"})
    assert p1.exists() and p1.parent == we
    mtime1 = p1.stat().st_mtime_ns
    time.sleep(0.02)
    p2 = b._cached_env_npz(env, {"envId": "primary"})
    assert p2 == p1 and p2.stat().st_mtime_ns == mtime1, "缓存未命中（重写了 npz）"
    # 同目录只有一个指纹缓存对（无积累）
    npzs = [f for f in we.iterdir() if f.suffix == ".npz"
            and not f.name.startswith(("ephemeral-", ".tmp-"))]
    assert len(npzs) == 1


# ---- 层 0c：evaluate 异步契约（真实 worker e2e） ----

SRC = "\ndef factor(env):\n    import pandas as pd\n" \
      "    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"


def test_worker_mode_evaluate_defaults_to_async(tmp_path):
    b = _make_bridge(tmp_path, mode="worker")
    r = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC,
                                       "stage": "development"})
    assert r.get("job_id") and r["status"] in ("submitted", "queued"), r
    # 轮询到 diagnosis
    deadline = time.monotonic() + 120
    diag = None
    while time.monotonic() < deadline:
        p = b.dispatch("factor.evaluate", {"envId": "primary",
                                           "job_id": r["job_id"]})
        if p.get("status") in ("running", "queued", "submitted"):
            time.sleep(0.5)
            continue
        diag = p
        break
    assert diag is not None and "ic_ir_train" in diag, "异步取回未得到诊断"
    assert "loop" in diag, "异步取回缺 loop 指令"
    # 缓存命中（R2 契约分叉）：C1 键含 n_trials——真实首评后键已轮换，
    # 这里用 stub 固定命中，验证「命中 → 同步直接返回 diagnosis」的分支
    import dsh_factor_mining.bridge as B
    orig_read = B.cache_read
    canned = {"ic_ir_train": diag["ic_ir_train"], "ic_mean_train": 0.0,
              "verdict": "pass", "red_flags": [], "horizon": diag.get("horizon"),
              "ic_series_train": [0.1] * 8}
    B.cache_read = lambda root, ck: canned
    try:
        again = b.dispatch("factor.evaluate",
                           {"envId": "primary", "source": SRC,
                            "stage": "development"})
    finally:
        B.cache_read = orig_read
    assert "ic_ir_train" in again and "job_id" not in again, \
        f"缓存命中未走同步路径: {str(again)[:120]}"
    assert again.get("eval_cache") == "hit"


def test_explicit_sync_escape_hatch(tmp_path):
    b = _make_bridge(tmp_path, mode="worker")
    r = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC,
                                       "stage": "development",
                                       "async": False})
    assert "ic_ir_train" in r and "job_id" not in r, "async:false 逃生阀失效"


def test_in_process_evaluate_stays_sync(tmp_path):
    b = _make_bridge(tmp_path, mode="in_process")
    r = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC,
                                       "stage": "development"})
    assert "ic_ir_train" in r and "job_id" not in r


# ---- R1 配套：worker 墙钟默认 600s ----

def test_worker_timeout_default_600s():
    import os
    b = Bridge.__new__(Bridge)   # 不跑 __init__（免建目录/收尸）
    assert b.__class__.__name__ == "Bridge"
    # 直接验证解析逻辑：默认 600_000；env 覆盖
    os.environ.pop("DSH_FACTOR_MINER_WORKER_TIMEOUT_MS", None)
    from dsh_factor_mining.bridge import Bridge as B2
    # 用构造函数验证（临时 root）
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bb = B2(state_root=d)
        assert bb.worker_timeout_ms == 600_000, bb.worker_timeout_ms
    os.environ["DSH_FACTOR_MINER_WORKER_TIMEOUT_MS"] = "123000"
    try:
        with tempfile.TemporaryDirectory() as d:
            bb = B2(state_root=d)
            assert bb.worker_timeout_ms == 123_000
    finally:
        del os.environ["DSH_FACTOR_MINER_WORKER_TIMEOUT_MS"]


# ---- 全量排查 2026-08-31：batch 去重键 + horizon 透传 ----

def test_batch_dedup_key_uses_sources(tmp_path):
    """batch 的 source 恒空——键必须含 sources 指纹，否则第二个 batch
    并入第一个的作业（拿到错误结果）。"""
    b = _make_bridge(tmp_path)
    b._slot_capacity = 4
    ctl = _FakeWorkerController(b).install()
    try:
        r1 = _submit(b, "", method="factor.evaluate_batch")
        # 手动注入 sources（_submit 不带 sources 参数）
        # 通过再次直接调用带 sources 的提交验证键区分
        params1 = {"envId": "primary", "sources": {"f1": "def factor(env): return env.c"}}
        params2 = {"envId": "primary", "sources": {"f2": "def factor(env): return env.v",
                                                   "f3": "def factor(env): return env.o"}}
        from dsh_factor_mining.lane import set_request_lane, reset_request_lane
        tok = set_request_lane(None)
        try:
            s1 = b._async_submit("factor.evaluate_batch", "", params1, None, "primary")
            s2 = b._async_submit("factor.evaluate_batch", "", params2, None, "primary")
        finally:
            reset_request_lane(tok)
        assert s1["job_id"] != s2["job_id"], \
            f"不同 sources 的 batch 被去重成同一作业: {s1['job_id']}"
        # 同 sources 重提交 → 去重
        tok = set_request_lane(None)
        try:
            s3 = b._async_submit("factor.evaluate_batch", "", dict(params1), None, "primary")
        finally:
            reset_request_lane(tok)
        assert s3.get("dedup") is True and s3["job_id"] == s1["job_id"]
    finally:
        ctl.release_all()
        ctl.restore()


def test_horizon_param_passthrough(tmp_path):
    """horizon 申报/scan 经 dispatch 可达（工具层已暴露）。"""
    b = _make_bridge(tmp_path, mode="in_process")
    # 单 horizon 菜单：scan 退化为单评估（菜单语义）
    r = b.dispatch("factor_evaluate".replace("_", "."), {}) if False else b.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC,
                            "stage": "development", "horizon": "scan"})
    assert "ic_ir_train" in r, f"scan 退化单评估失败: {str(r)[:120]}"


# ---- R22：composite 去重键并成分指纹 ----

def test_composite_dedup_key_includes_ingredients(tmp_path):
    """同 overlay 不同成分表 = 不同作业（R22，2026-08-31 复审）。

    成分表只在 params['ingredients']，不在 source——键里不并成分指纹的话，
    固定 overlay 扫成分的工作流里第二个 composite 会并入第一个的在途
    作业、拿到错误成分的结果。"""
    b = _make_bridge(tmp_path)
    b._slot_capacity = 4
    ctl = _FakeWorkerController(b).install()
    try:
        src = "def factor(env):\n    import numpy as np\n    return np.ones((env.T, env.N))"
        base = {"envId": "primary", "source": src, "async": True}
        s1 = b._async_submit("factor.evaluate_composite", src,
                             {**base, "ingredients": {"a": SRC}}, None, "primary")
        s2 = b._async_submit("factor.evaluate_composite", src,
                             {**base, "ingredients": {"b": "def factor(env):\n    return env.v"}}, None, "primary")
        assert s1["job_id"] != s2["job_id"], \
            f"不同成分的 composite 被去重成同一作业: {s1['job_id']}"
        # 同成分重提交 → 去重并入（原有语义保持）
        s3 = b._async_submit("factor.evaluate_composite", src,
                             {**base, "ingredients": {"a": SRC}}, None, "primary")
        assert s3.get("dedup") is True and s3["job_id"] == s1["job_id"]
    finally:
        ctl.release_all()
        ctl.restore()


# ---- R23：done 作业重复轮询的 wrap 幂等 ----

def test_poll_wrap_idempotent(tmp_path):
    """重复轮询不再重复 wrap（R23，2026-08-31 复审）。

    _wrap_diagnosis 带 trail 写入副作用（条目按 (hash, stage, horizon)
    去重更新 + 挪到队尾）：同线已有更新试验时，重轮询旧作业会把旧条目
    挪到队尾 → 伪断链 + arc 误归零 + latest5 失真。幂等化后：快照
    diagnosis.json 直接返回，trail 顺序不动。"""
    b = _make_bridge(tmp_path, mode="worker")
    ctl = _FakeWorkerController(b).install()
    ctl.block = False   # 作业立即完成（wrap 副作用是桥代码，真实执行）
    # 因果门 stub：门走真实 worker 链路（假 worker 无 run_dir 的内部调用
    # 会炸）——本测试只锁定 wrap 幂等，不测因果拒绝（另有专职测试）
    b._enforce_causality = lambda source, env, env_id=None: {"verdict": "causal"}
    try:
        def _poll_till_done(job_id):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                r = b.dispatch("factor.evaluate", {"envId": "primary",
                                                   "job_id": job_id})
                if r.get("job_id") is None and r.get("status") is None:
                    return r
                time.sleep(0.05)
            raise AssertionError("作业未在 15s 内完成")

        # 行内因果源（只用当行）——因果门（R11 起随作业线程）真实执行，
        # 前视源（如 env.c[-1]）会被正确拒绝使作业 failed
        src1 = "def factor(env):\n    return env.c"
        src2 = "def factor(env):\n    return env.v"
        j1 = b.dispatch("factor.evaluate", {"envId": "primary",
                                            "source": src1, "stage": "development"})
        j2 = b.dispatch("factor.evaluate", {"envId": "primary",
                                            "source": src2, "stage": "development"})
        d1_first = _poll_till_done(j1["job_id"])
        d2 = _poll_till_done(j2["job_id"])
        trail_p = Path(b.state_root) / "trail_engine.json"
        entries = json.loads(trail_p.read_text(encoding="utf-8"))
        order_before = [e["source_hash"] for e in entries]

        # 重轮询旧作业：必须返回快照（内容一致）且 trail 顺序不动
        d1_again = _poll_till_done(j1["job_id"])
        assert d1_again == d1_first, "重轮询返回的 diagnosis 与首次不一致"
        entries_after = json.loads(trail_p.read_text(encoding="utf-8"))
        order_after = [e["source_hash"] for e in entries_after]
        assert order_after == order_before, (
            f"重轮询挪动了 trail 条目顺序: {order_before} -> {order_after}")
        # 快照文件是幂等标记
        diag = (Path(b.state_root) / "worker_runs" / j1["job_id"]
                / "diagnosis.json")
        assert diag.exists()
        assert json.loads(diag.read_text(encoding="utf-8")) == d1_first
    finally:
        ctl.release_all()
        ctl.restore()


# ---- R24/R25：异步 batch 的因果门 + 成员计账 + horizon 校验 ----

def _poll_till_done(bridge, job_id, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = bridge.dispatch("factor.evaluate", {"envId": "primary",
                                                "job_id": job_id})
        if r.get("job_id") is None and r.get("status") is None:
            return r
        time.sleep(0.05)
    raise AssertionError("作业未在时限内完成")


def test_batch_async_member_accounting(tmp_path):
    """异步 batch 成员照常 A1 入账（R24/R25 复审：此前 async 分支绕过
    全部后处理——成员不入 trail + 轮询把批信封当单因子 wrap 出一条空
    hash 幻影条目 + deflated 全员 None）。"""
    b = _make_bridge(tmp_path, mode="worker")
    ctl = _FakeWorkerController(b).install()
    ctl.block = False
    b._enforce_causality = lambda s, e, eid=None: {"verdict": "causal"}
    f1 = "def factor(env):\n    return env.c"
    f2 = "def factor(env):\n    return env.v"
    ctl.result_override = {
        "factors": {"m1": {"ic_ir_train": 0.2, "horizon": 10},
                    "m2": {"ic_ir_train": 0.3, "horizon": 10}},
        "batch": {"bar_sigma": 0.5}}
    try:
        sub = b.dispatch("factor.evaluate_batch", {
            "envId": "primary", "sources": {"m1": f1, "m2": f2},
            "async": True})
        assert sub.get("job_id"), f"未返回作业: {sub}"
        result = _poll_till_done(b, sub["job_id"])
        # 成员附 _meta/receipt（此前 async 分支全缺）
        m1 = result["factors"]["m1"]
        assert m1.get("_meta", {}).get("receipt"), "成员缺 _meta/receipt"
        # A1：成员真实 hash 入 trail；空 hash 幻影条目消失
        from dsh_factor_mining.discipline import source_fingerprint
        entries = json.loads((Path(b.state_root) / "trail_engine.json")
                             .read_text(encoding="utf-8"))
        hashes = {e.get("source_hash") for e in entries}
        assert source_fingerprint(f1) in hashes, "成员 m1 未入 trail"
        assert source_fingerprint(f2) in hashes, "成员 m2 未入 trail"
        assert source_fingerprint("") not in hashes, "空 hash 幻影条目仍在"
        # 幂等：二次轮询 = 快照（不重复入账）
        again = _poll_till_done(b, sub["job_id"])
        assert again == result
    finally:
        ctl.release_all()
        ctl.restore()


def test_batch_async_causality_gate_and_error_code(tmp_path):
    """异步 batch 逐成员过因果门（R24/R25：此前 async 完全绕过）+ 失败
    轮询按原始错误码重现（R27：域错误 -32003 不再统一改报 infra
    -32005，infra 台账不被重复轮询刷屏）。"""
    b = _make_bridge(tmp_path, mode="worker")
    ctl = _FakeWorkerController(b).install()
    ctl.block = False
    from dsh_factor_mining.bridge import BridgeError

    def _gate(s, e, eid=None):
        if "LEAKMARK" in s:
            raise BridgeError(-32003, "FUTURE_LEAK: 未来函数（右侧数据）")
        return {"verdict": "causal"}
    b._enforce_causality = _gate
    try:
        sub = b.dispatch("factor.evaluate_batch", {
            "envId": "primary",
            "sources": {"bad": "LEAKMARK def factor(env):\n    return env.c",
                        "ok": "def factor(env):\n    return env.v"},
            "async": True})
        deadline = time.monotonic() + 20
        code = None
        while time.monotonic() < deadline:
            try:
                b.dispatch("factor.evaluate", {"envId": "primary",
                                               "job_id": sub["job_id"]})
            except BridgeError as e:
                code = e.code
                assert "FUTURE_LEAK" in str(e)
                break
            time.sleep(0.05)
        assert code == -32003, f"错误码未按原始重现: {code}"
        # R27：域错误不进 infra 台账（-32005 专属）
        tf = Path(b.state_root) / "tool_failures.json"
        assert not tf.exists() or not json.loads(
            tf.read_text(encoding="utf-8")), "域错误误入 infra 台账"
    finally:
        ctl.release_all()
        ctl.restore()


def test_batch_async_horizon_validated_at_submit(tmp_path):
    """异步 batch 的 horizon 申报校验在提交时生效（R24：此前 async 分支
    跳过校验——菜单外值静默回落主口径）。"""
    from dsh_factor_mining.bridge import BridgeError
    b = _make_bridge(tmp_path, mode="worker")
    ctl = _FakeWorkerController(b).install()
    ctl.block = False
    try:
        with pytest.raises(BridgeError) as ei:
            b.dispatch("factor.evaluate_batch", {
                "envId": "primary",
                "sources": {"m1": "def factor(env):\n    return env.c"},
                "horizon": 999, "async": True})
        assert ei.value.code == -32602
        root = Path(b.state_root) / "worker_runs"
        jobs = [p for p in root.iterdir() if (p / "job.json").exists()] \
            if root.exists() else []
        assert not jobs, "被拒提交不应产生作业"
    finally:
        ctl.release_all()
        ctl.restore()


# ---- R26：重评不迁移条目 lane、不触发断链 ----

def test_reeval_keeps_lane_and_arc(tmp_path):
    """B 线重评 A 线试验：条目归首创线、A 的 arc 预算不被外线动作误归零
    （隔离评审可复现实证：此前 _same_trial 替换把条目迁到 B，A 续写时
    own_tail 回退到异族旧条目 → 伪断链 + arc 归零）。"""
    from dsh_factor_mining.bridge import (_construction_fingerprint,
                                          source_fingerprint)
    from dsh_factor_mining.state import (arc_rounds_bump,
                                         lane_arc_rounds)
    b = _make_bridge(tmp_path)

    # 构造指纹是行对 shingle 的 MinHash：单行差异在多行源里只动一个
    # shingle，Jaccard 远超族阈值 0.25——shift(20) vs shift(30) 同族
    def fam(n):
        return ("def factor(env):\n"
                "    c = env.c\n"
                "    v = env.v\n"
                "    m = c / v\n"
                f"    return m / c.shift({n}) - 1.0\n")

    def append(lane, src):
        tok = set_request_lane(lane)
        try:
            b._append_engine_trail_batch("primary", [(
                source_fingerprint(src), "development",
                {"ic_ir_train": 0.1, "horizon": 10,
                 "_construction_fp": _construction_fingerprint(src)}, None)])
        finally:
            reset_request_lane(tok)

    for _ in range(3):
        arc_rounds_bump(b.state_root, lane="laneA")
    append("laneA", fam(20))     # A 首评
    append("laneB", fam(20))     # B 重评同试验
    append("laneA", fam(30))     # A 续写同族
    entries = json.loads((Path(b.state_root) / "trail_engine.json")
                         .read_text(encoding="utf-8"))
    x1 = next(e for e in entries
              if e["source_hash"] == source_fingerprint(fam(20)))
    assert x1["lane"] == "laneA", "重评把条目迁移到了重评者的线"
    st = json.loads((Path(b.state_root) / "mining_state.json")
                    .read_text(encoding="utf-8")) \
        if (Path(b.state_root) / "mining_state.json").exists() else {}
    assert lane_arc_rounds(st, "laneA") == 3, "A 线 arc 被外线重评误归零"


# ---- R30：G2 统计量绑定环境口径 tail_k ----

def test_g2_statistic_bound_to_tail_k(tmp_path):
    """G2 噪声门的 spread 统计量用环境 k_frac（R30：k_frac 可配化后此门
    漏绑恒用默认 0.2——门在检验另一个口径的统计量）。"""
    b = _make_bridge(tmp_path)
    env = b.envs["primary"]
    env.calibration.k_frac = 0.05
    import dsh_factor_mining.factor.tail as tail_mod
    captured = {}
    orig = tail_mod.spread_ir_statistic

    def spy(F, fwd, pit, ss, k_frac=0.2, **kw):
        captured["k_frac"] = k_frac
        return orig(F, fwd, pit, ss, k_frac=k_frac, **kw)

    tail_mod.spread_ir_statistic = spy
    try:
        b._run_factor("factor.noise_test",
                      "def factor(env):\n    return env.c",
                      {"m": 10, "base_seed": 1, "statistic": "spread"}, env)
        assert captured.get("k_frac") == 0.05, \
            f"G2 统计量未绑 tail_k: {captured}"
    finally:
        tail_mod.spread_ir_statistic = orig


def test_phase_visible_through_job_lifecycle(tmp_path):
    """R32：job.json 的 phase 字段全程可见——因果门期 causality_check、
    评估期 evaluating（轨迹审计发现 _run_worker 重写曾把门阶段戳抹掉）。"""
    import threading
    b = _make_bridge(tmp_path, mode="worker")
    ctl = _FakeWorkerController(b).install()
    ctl.block = False
    seen_phases = []
    orig_gate_holder = {}

    def _gate(s, e, eid=None):
        jp = Path(b.state_root) / "worker_runs" / orig_gate_holder["job_id"] / "job.json"
        try:
            seen_phases.append(json.loads(jp.read_text(encoding="utf-8")).get("phase"))
        except Exception:
            seen_phases.append(None)
        return {"verdict": "causal"}
    b._enforce_causality = _gate
    try:
        sub = b.dispatch("factor.evaluate", {
            "envId": "primary", "source": "def factor(env):\n    return env.c",
            "stage": "development"})
        orig_gate_holder["job_id"] = sub["job_id"]
        _poll_till_done(b, sub["job_id"])
        jp = Path(b.state_root) / "worker_runs" / sub["job_id"] / "job.json"
        final = json.loads(jp.read_text(encoding="utf-8"))
        assert "causality_check" in seen_phases, f"门期 phase 不可见: {seen_phases}"
        assert final.get("phase") == "evaluating", \
            f"完成态 phase 丢失: {final.get('phase')}"
    finally:
        ctl.release_all()
        ctl.restore()
