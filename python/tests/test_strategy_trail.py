# coding=utf-8
"""P3 账本测试（规划 §8.7）：指纹稳定性 / trail 键 dedup / 计-与不计试验 /
test 一次性消费 / registry 备份 / 因子 ref 交叉核对。"""
import json

import pytest

from dsh_strategy_lab.fingerprint import (
    fingerprint_key,
    normalize_factor_refs,
    strategy_fingerprint,
)
from dsh_strategy_lab.state import (
    atomic_write_json,
    check_factor_refs,
    check_test_lock,
    consume_test_lock,
    path_for,
    read_registry,
    write_registry_with_backup,
)
from dsh_strategy_lab.trail import COUNTED_STAGES, append_trail, n_trials, strategy_ledger


def _fp(source="def apply(state, env):\n    return [{} for _ in range(env.T)]\n",
        params=None, refs=None, wf=None, cost="v1:comm2.5bps"):
    return strategy_fingerprint(refs or [{"source_hash": "a" * 16, "horizon": 20}],
                                source, params or {"k": 1},
                                wf or {"n_folds": 5, "embargo_bars": 5}, cost)


def test_fingerprint_stability_and_sensitivity():
    a = _fp()
    assert _fp() == a                       # 同组件同 hash（确定性）
    assert a["key"] == fingerprint_key(a)
    assert _fp(source="def apply(...): pass")["key"] != a["key"]
    assert _fp(params={"k": 2})["key"] != a["key"]
    assert _fp(wf={"n_folds": 4, "embargo_bars": 5})["key"] != a["key"]
    assert _fp(cost="v1:comm3bps")["key"] != a["key"]
    assert _fp(refs=[{"source_hash": "b" * 16, "horizon": 20}])["key"] != a["key"]
    # 引用顺序无关（同组引用 = 同指纹）
    r1 = [{"source_hash": "a", "horizon": 5}, {"source_hash": "b", "horizon": 10}]
    r2 = list(reversed(r1))
    assert normalize_factor_refs(r1) == normalize_factor_refs(r2)
    assert strategy_fingerprint(r1, "s", {}, {}, "c")["key"] == \
        strategy_fingerprint(r2, "s", {}, {}, "c")["key"]


def test_trail_dedup_update_not_append(tmp_path):
    fp = _fp()
    e1 = append_trail({"stage": "development", "fingerprint": fp,
                       "metrics": {"sharpe": 1.0}}, root=tmp_path)
    append_trail({"stage": "walk_forward", "fingerprint": fp,
                  "metrics": {"sharpe": 0.9}}, root=tmp_path)
    assert n_trials(tmp_path) == 2            # dev + wf 各计一次
    e2 = append_trail({"stage": "development", "fingerprint": fp,
                       "metrics": {"sharpe": 1.2}}, root=tmp_path)
    assert n_trials(tmp_path) == 2            # 重评同键 = 更新不新增
    assert e2["metrics"]["sharpe"] == 1.2 and e2["ts"] == e1["ts"]
    assert e2["last_ts"] >= e2["ts"]
    # 同指纹不同 stage 是不同键（wf 重评也不放大基数）
    append_trail({"stage": "walk_forward", "fingerprint": fp,
                  "metrics": {"sharpe": 0.8}}, root=tmp_path)
    assert n_trials(tmp_path) == 2


def test_trail_rejects_probe_stages(tmp_path):
    """探针不记 trail（不计试验）：placebo/噪声面板/扰动/截断审计
    不是 stage——传进来 = 编程错误，fail-closed。"""
    with pytest.raises(ValueError, match="探针"):
        append_trail({"stage": "placebo", "fingerprint": _fp()}, root=tmp_path)
    with pytest.raises(ValueError, match="fingerprint"):
        append_trail({"stage": "development"}, root=tmp_path)


def test_test_region_never_in_ledger(tmp_path):
    fp = _fp()
    append_trail({"stage": "test", "fingerprint": fp}, root=tmp_path)
    assert n_trials(tmp_path) == 0                      # 永不进 deflation
    assert all(e["stage"] in COUNTED_STAGES for e in strategy_ledger(tmp_path))
    # test 一次性消费锁
    lock = consume_test_lock(fp["key"], root=tmp_path)
    assert lock["consumed"] is True
    assert check_test_lock(tmp_path)["consumed"] is True
    with pytest.raises(RuntimeError, match="一次性|最终消耗品|禁止反复"):
        consume_test_lock(fp["key"], root=tmp_path)


def test_registry_write_with_backup(tmp_path):
    write_registry_with_backup(
        [{"name": "s1", "accepted": True, "reject_kind": None}],
        root=tmp_path)
    assert read_registry(tmp_path)[0]["name"] == "s1"
    write_registry_with_backup(
        [{"name": "s1", "accepted": False, "reject_kind": "substantive"}],
        root=tmp_path)
    reg = read_registry(tmp_path)
    assert reg[0]["reject_kind"] == "substantive"
    baks = list(tmp_path.glob("strategy_registry.*.bak"))
    assert len(baks) == 1                       # 改动前备份（铁律）
    assert json.loads(baks[0].read_text(encoding="utf-8"))[0]["accepted"] is True


def test_check_factor_refs(tmp_path, monkeypatch):
    # 造一个因子 registry（生产因子状态只读——这里只是测试夹具）
    from dsh_factor_mining.state import write_registry as f_write_registry
    froot = tmp_path / ".factor-mining"
    froot.mkdir()
    f_write_registry(
        [{"name": "f_ok", "source_hash": "a" * 16, "accepted": True},
         {"name": "f_rej", "source_hash": "b" * 16, "accepted": False}],
        root=froot)
    refs_ok = [{"source_hash": "a" * 16, "horizon": 20}]
    out = check_factor_refs(refs_ok, factor_state_root=froot)
    assert out["ok"] is True
    out = check_factor_refs([{"source_hash": "b" * 16, "horizon": 20}],
                            factor_state_root=froot)
    assert out["ok"] is False and len(out["not_accepted"]) == 1
    out = check_factor_refs([{"source_hash": "c" * 16, "horizon": 20}],
                            factor_state_root=froot)
    assert out["ok"] is False and len(out["missing"]) == 1
    out = check_factor_refs([], factor_state_root=froot)
    assert out["ok"] is False and "factor_refs 为空" in out["note"]


def test_atomic_write_no_partial_file(tmp_path):
    """原子性：payload 不可序列化时原文件不动（不留半截/清空）。"""
    p = path_for("trail", root=tmp_path)
    atomic_write_json(p, [{"a": 1}])
    with pytest.raises(TypeError):
        atomic_write_json(p, [{"b": object()}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"a": 1}]
    assert not list(tmp_path.glob("*.tmp"))
