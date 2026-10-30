# coding=utf-8
"""探索相似性防线测试（2026-09-04，analysis/exploration_similarity 04 方案落地）。

S4 authored 署名 / S1 拒绝即信息门（同构拦截·参数扫描放行·shadow 模式）/
S2 宣言分型校验（新信息源查 L1 增量·变体放行）。

Run: PYTHONPATH=src python tests/test_novelty_gate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError  # noqa: E402
from dsh_factor_mining.lane import current_lane  # noqa: E402
from dsh_factor_mining.state import (  # noqa: E402
    lane_explore_hashes_extend, read_json_list, source_fingerprint,
)

from test_bridge_smoke import _make_bridge, _poll_evaluate  # noqa: E402

# 测试源码行数刻意 ≥12 行（行对 shingle：单行改动伤 2 个 shingle，
# 行数不足时"仅常量变"的参数扫描会掉出 0.8 相似带，无法覆盖该分支）
SRC_A = """
import pandas as pd
import numpy as np

def factor(env):
    c = pd.DataFrame(env.c)
    v = pd.DataFrame(env.v)
    ret = c.pct_change(1)
    vol_c = c.pct_change(1).rolling(10).std()
    vol_v = v.pct_change(1).rolling(10).std()
    mom = c / c.shift(20) - 1.0
    crowd = v.rolling(5).mean() / (v.rolling(60).mean() + 1e-10)
    base = ret.abs().rolling(10).mean()
    sig = mom * crowd / (base + 1e-10)
    sig = sig - sig.rolling(3).mean()
    return (sig * vol_c / (vol_v + 1e-10)).values
"""
# B：与 A 常量全同、结构同（docstring/注释被指纹规范化剥离——只改 hash）
SRC_B = '''
"""docstring 变体——结构相同。"""
import pandas as pd
import numpy as np

def factor(env):
    # 注释行
    c = pd.DataFrame(env.c)
    v = pd.DataFrame(env.v)
    ret = c.pct_change(1)
    vol_c = c.pct_change(1).rolling(10).std()
    vol_v = v.pct_change(1).rolling(10).std()
    mom = c / c.shift(20) - 1.0
    crowd = v.rolling(5).mean() / (v.rolling(60).mean() + 1e-10)
    base = ret.abs().rolling(10).mean()
    sig = mom * crowd / (base + 1e-10)
    sig = sig - sig.rolling(3).mean()
    return (sig * vol_c / (vol_v + 1e-10)).values
'''
# B2：参数扫描（末行单常量 1e-10 → 1e-9：只伤 1 个 shingle，相似仍在 0.8 带）
SRC_B2 = SRC_A.replace("(vol_v + 1e-10)", "(vol_v + 1e-9)")
# C：引入新数据列 amount（L1 增量）
SRC_C = SRC_A.replace(
    "import numpy as np\n",
    "import numpy as np\n").replace(
    "    crowd = v.rolling(5).mean() / (v.rolling(60).mean() + 1e-10)\n",
    "    amt = pd.DataFrame(env.amount)\n"
    "    crowd = amt.rolling(5).mean() / (amt.rolling(60).mean() + 1e-10)\n")


def _gate_log(root: Path) -> list[dict]:
    p = Path(root) / "state" / "novelty_gate.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def _record(bridge: Bridge, new_information: str) -> dict:
    return bridge.dispatch("paths.append", {"layer": "trail", "entry": {
        "round": 1, "signal": "s", "attribution": "a", "next_hypothesis": "n",
        "new_information": new_information}})


@pytest.fixture()
def env_ctx(tmp_path, monkeypatch):
    """独立 stateRoot + 固定 lane（explore 注册与评估须同线）。"""
    monkeypatch.setenv("DSH_FACTOR_MINING_LANE", "test-novelty")
    monkeypatch.delenv("DSH_FACTOR_NOVELTY_GATE", raising=False)
    monkeypatch.delenv("DSH_FACTOR_DECL_GATE", raising=False)
    bridge, data_path = _make_bridge(tmp_path)  # 默认 T=1200 N=40：小于此被 MIN_POOL 评估门拦
    return bridge, tmp_path


def test_s4_authored_field(env_ctx):
    bridge, root = env_ctx
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_A}))
    te = json.loads((Path(root) / "state" / "trail_engine.json").read_text(encoding="utf-8"))
    assert te and te[-1].get("authored") is True

    # 注册 explore 台账后再评估新源 → origin=random / authored=False
    src_random = SRC_B2.replace("25", "33")
    lane_explore_hashes_extend(bridge.state_root, current_lane(),
                               [source_fingerprint(src_random)])
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": src_random}))
    te = json.loads((Path(root) / "state" / "trail_engine.json").read_text(encoding="utf-8"))
    assert te[-1].get("origin") == "random"
    assert te[-1].get("authored") is False


def test_s1_gate_block_sweep_shadow(env_ctx):
    bridge, root = env_ctx
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_A}))
    # 同 hash 重交：不走门（eval_cache/复评域），不报同构错
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_A}))
    # 门条件=先例 verdict=pass：合成面板判定随机且缓存命中会回写条目，
    # 故在重评之后白盒钉 verdict（mtime 变更触发索引重载）
    te_path = Path(root) / "state" / "trail_engine.json"
    te = json.loads(te_path.read_text(encoding="utf-8"))
    te[-1]["verdict"] = "pass"
    te_path.write_text(json.dumps(te, ensure_ascii=False), encoding="utf-8")

    # 同构重交（常量未变）→ 拒绝即信息
    with pytest.raises(BridgeError) as ei:
        bridge.dispatch("factor.evaluate", {"envId": "primary", "source": SRC_B})
    assert ei.value.code == -32003
    assert "同构重交拦截" in ei.value.message
    assert "先例" in ei.value.message and "IC_IR" in ei.value.message
    assert any(e.get("decision") == "block_enforce" for e in _gate_log(root))

    # 参数扫描（常量已变）→ 放行 + 遥测
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_B2}))
    assert any(e.get("decision") == "allow_parameter_sweep" for e in _gate_log(root))

    # shadow 模式：同构重交只记不拦（会真的评估——确认不 raise 即可）
    import os
    os.environ["DSH_FACTOR_NOVELTY_GATE"] = "shadow"
    try:
        _poll_evaluate(bridge, bridge.dispatch(
            "factor.evaluate", {"envId": "primary", "source": SRC_B}))
    finally:
        os.environ.pop("DSH_FACTOR_NOVELTY_GATE", None)
    assert any(e.get("decision") == "block_shadow" for e in _gate_log(root))


def test_s2_declaration_gate(env_ctx):
    bridge, root = env_ctx
    # 轮 1：评估 A（attrs=c,v），无「新」宣称记录 → 游标推进，A 成为先例
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_A}))
    r = _record(bridge, "量价基础探索（无新宣称）")
    assert r is not None

    # 轮 2：评估 B2（A 的参数扫描，attrs 仍 c,v）——宣称新信息源但 L1 无增量 → 拒收
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_B2}))
    with pytest.raises(BridgeError) as ei:
        _record(bridge, "首次引入 X 视角作为新信息源")
    assert ei.value.code == -32602
    assert "新信息源" in ei.value.message and "变体" in ei.value.message
    assert any(e.get("gate") == "declaration" and e.get("decision") == "reject_no_l1_increment"
               for e in _gate_log(root))

    # 改口变体宣言 → 放行（变体是合法深挖）
    r = _record(bridge, "对 A 的变体：换分母常数参数")
    assert r is not None

    # 轮 3：引入 amount 列（L1 增量）→ 宣称新信息源放行
    _poll_evaluate(bridge, bridge.dispatch(
        "factor.evaluate", {"envId": "primary", "source": SRC_C}))
    r = _record(bridge, "引入成交额 amount 作为新维度（新信息源）")
    assert r is not None
    assert any(e.get("gate") == "declaration" and e.get("decision") == "allow"
               for e in _gate_log(root))


def main():
    for fn in (test_s4_authored_field, test_s1_gate_block_sweep_shadow,
               test_s2_declaration_gate):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            class _MP:
                def __init__(self):
                    import os
                    self._env = {}
                def setenv(self, k, v):
                    import os
                    self._env[k] = os.environ.get(k)
                    os.environ[k] = v
                def delenv(self, k, raising=True):
                    import os
                    if k in os.environ:
                        self._env[k] = os.environ[k]
                        del os.environ[k]
            import os
            os.environ.setdefault("DSH_FACTOR_MINING_LANE", "test-novelty")
            bridge, data_path = _make_bridge(Path(td))
            fn((bridge, Path(td)))
        print(f"PASS {fn.__name__}")
    print("NOVELTY_GATE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
