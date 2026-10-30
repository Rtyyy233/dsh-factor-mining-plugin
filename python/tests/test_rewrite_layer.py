# coding=utf-8
"""LLM 重写层（效率编译层 2026-09-01）测试——全程假 LLM，零真实调用。

锁定契约（用户拍板 2026-09-01）：
- 仅严格等价（allclose rtol 1e-6）自动换用；APPROXIMATE/CANNOT_PRESERVE
  永不自动采用
- 安全扫描：import/危险调用拒绝
- 谎报 EXACT 被验证当场戳穿 → failed 入缓存，同类源码不再烧模型
- 透明换用链：cache ok → _run_worker 入口换用 → perf.engine ==
  "rewrite:llm-v1" + rewrite_provenance 溯源
"""
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining import bridge as bridge_mod
from dsh_factor_mining import rewrite as rw_mod
from dsh_factor_mining.bridge import BridgeError
from dsh_factor_mining.discipline import source_fingerprint

# 确定性慢源（t∝cells + 函数内 import → njit 不可编译 = 纯 C-verdict），
# 返回 c*0.0——与 FAKE_REWRITE 严格等价
SLOW_ORIG = (
    "def factor(env):\n"
    "    import time as _t\n"
    "    c = env.c\n"
    "    n = c.shape[0] * c.shape[1]\n"
    "    end = _t.process_time() + n / 40000.0\n"
    "    while _t.process_time() < end:\n"
    "        pass\n"
    "    return c * 0.0\n")

FAKE_OK = "def factor(env):\n    return env.c * 0.0\n"
FAKE_FALSE_EXACT = "def factor(env):\n    return env.c * 2.0 + 1.0\n"
FAKE_IMPORT = ("def factor(env):\n"
               "    import os\n"
               "    return env.c * 0.0\n")


def _reply(code, declare="EXACT"):
    return f"DECLARE: {declare}\n```python\n{code}\n```"


@pytest.fixture
def rewrite_bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_FACTOR_REWRITE", "1")
    monkeypatch.setenv("DSH_FACTOR_REWRITE_API_KEY", "sk-test-only")
    monkeypatch.setenv("DSH_FACTOR_REWRITE_MODEL", "fake-model")
    monkeypatch.setattr(bridge_mod, "effective_wall_timeout",
                        lambda base_s: float(base_s))
    from test_bridge_smoke import _make_bridge
    b, _ = _make_bridge(tmp_path)
    b.dispatch("data.load", {"envId": "primary"})
    b.worker_timeout_ms = 2000
    b._smoke_min_cells = 0
    return b


def _set_fake_llm(monkeypatch, code, declare="EXACT", calls=None):
    def _fake(cfg, user_msg):
        if calls is not None:
            calls.append(user_msg)
        return _reply(code, declare), {"completion_tokens": 123}
    monkeypatch.setattr(rw_mod, "call_llm", _fake)


def _wait_entry(b, fp, timeout_s=60.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with b._optimizer_lock:
            entry = (b._optimizer_cache or {}).get(fp)
        if isinstance(entry, dict) and entry.get("status") in ("ok", "failed"):
            return entry
        time.sleep(0.4)
    return None


def test_rewrite_full_loop_and_transparent_adoption(rewrite_bridge, monkeypatch):
    """拒绝 → 重写作业（假 LLM）→ 严格等价验证 → cache ok →
    原样重交 → 透明换用 → perf 带 rewrite 溯源。"""
    b = rewrite_bridge
    calls = []
    _set_fake_llm(monkeypatch, FAKE_OK, calls=calls)
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": SLOW_ORIG})
    assert ei.value.code == -32003
    assert "重写作业" in str(ei.value) or "重写层未配置" not in str(ei.value)

    fp = source_fingerprint(SLOW_ORIG)
    entry = _wait_entry(b, fp)
    assert entry is not None, "重写作业应在 60s 内产出 cache 条目"
    assert entry["status"] == "ok"
    assert entry["rewritten_source"] == FAKE_OK.strip()
    assert entry["model"] == "fake-model"
    assert "反馈" not in calls[0]      # 首次不带反馈
    out = b.dispatch("factor.check_causality",
                     {"envId": "primary", "source": SLOW_ORIG})
    assert isinstance(out, dict) and out.get("verdict") is not None

    diag = b.dispatch("factor.evaluate",
                      {"envId": "primary", "source": SLOW_ORIG,
                       "stage": "development", "async": False})
    perf = diag.get("perf", {})
    assert perf.get("engine") == "rewrite:llm-v1"
    assert perf["rewrite_provenance"]["rewritten_from"] == fp
    assert perf["rewrite_provenance"]["verifier"].startswith("allclose")
    # optimizer_cache.json 落盘（跨重启持久）
    disk = json.loads((Path(b.state_root) / "optimizer_cache.json")
                      .read_text(encoding="utf-8"))
    assert fp in disk and disk[fp]["status"] == "ok"


def test_rewrite_safety_reject_import(rewrite_bridge, monkeypatch):
    """LLM 返回 import os → 安全扫描拒 → 两轮全败 → failed 入缓存。"""
    b = rewrite_bridge
    _set_fake_llm(monkeypatch, FAKE_IMPORT)
    with pytest.raises(BridgeError):
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": SLOW_ORIG})
    fp = source_fingerprint(SLOW_ORIG)
    entry = _wait_entry(b, fp)
    assert entry is not None and entry["status"] == "failed"
    # 重交：拒绝消息带"已试未果"，不再排新作业
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": SLOW_ORIG})
    assert "已试未果" in str(ei.value)


def test_rewrite_false_exact_caught(rewrite_bridge, monkeypatch):
    """谎报 EXACT（返回不等价实现）→ 验证 allclose 戳穿 → failed。"""
    b = rewrite_bridge
    _set_fake_llm(monkeypatch, FAKE_FALSE_EXACT)
    with pytest.raises(BridgeError):
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": SLOW_ORIG})
    fp = source_fingerprint(SLOW_ORIG)
    entry = _wait_entry(b, fp)
    assert entry is not None and entry["status"] == "failed"


def test_rewrite_disabled_when_no_key(tmp_path, monkeypatch):
    """无 key：重写层静默降级——拒绝消息仍完整（含向量化指导）。"""
    monkeypatch.delenv("DSH_FACTOR_REWRITE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.setenv("DSH_FACTOR_REWRITE", "1")
    monkeypatch.setattr(bridge_mod, "effective_wall_timeout",
                        lambda base_s: float(base_s))
    from test_bridge_smoke import _make_bridge
    b, _ = _make_bridge(tmp_path)
    b.dispatch("data.load", {"envId": "primary"})
    b.worker_timeout_ms = 2000
    b._smoke_min_cells = 0
    with pytest.raises(BridgeError) as ei:
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": SLOW_ORIG})
    assert ei.value.code == -32003
    assert "未配置" in str(ei.value)
    time.sleep(1.0)
    assert not getattr(b, "_rewrite_inflight", set())


def test_rewrite_job_poll_returns_informational(rewrite_bridge, monkeypatch):
    """并发复审 F4：engine.rewrite 的 job_id 被轮询到时返回信息性摘要，
    不是伪 diagnosis。"""
    b = rewrite_bridge
    _set_fake_llm(monkeypatch, FAKE_OK)
    with pytest.raises(BridgeError):
        b.dispatch("factor.check_causality",
                   {"envId": "primary", "source": SLOW_ORIG})
    fp = source_fingerprint(SLOW_ORIG)
    assert _wait_entry(b, fp) is not None
    # 从 worker_runs 找重写作业的 job_id（agent 能从 status 看到同款信息）
    jid = None
    for jp in (Path(b.state_root) / "worker_runs").glob("*/job.json"):
        j = json.loads(jp.read_text(encoding="utf-8"))
        if j.get("method") == "engine.rewrite" and j.get("status") == "done":
            jid = j.get("job_id")
            break
    assert jid, "engine.rewrite 作业应有 done 墓志"
    r = b.dispatch("factor.evaluate", {"job_id": jid})
    assert "engine_rewrite" in r and "note" in r
    assert r["engine_rewrite"].get("status") == "ok"
    assert "ic_ir_train" not in r   # 绝不是伪装成评估对象


def test_glm_models_forbidden():
    """glm 系误配回落默认模型（用户铁律：glm 太贵绝不调用）。"""
    import os
    old = {k: os.environ.get(k) for k in
           ("DSH_FACTOR_REWRITE_MODEL", "DSH_FACTOR_REWRITE_API_KEY",
            "OPENCODE_GO_API_KEY", "DSH_FACTOR_REWRITE")}
    try:
        os.environ["DSH_FACTOR_REWRITE_MODEL"] = "glm-5.2"
        os.environ["DSH_FACTOR_REWRITE_API_KEY"] = "sk-x"
        os.environ.pop("OPENCODE_GO_API_KEY", None)
        cfg = rw_mod.rewrite_config()
        assert cfg["model"] == rw_mod.DEFAULT_MODEL
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
