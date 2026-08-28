# coding=utf-8
"""源码外挂库测试（2026-08-29 需求）：trail 按 source_hash 可回溯源码全文，
账本本体不膨胀。
"""
from __future__ import annotations

import json

import pytest

from dsh_factor_mining.discipline import source_fingerprint
from dsh_factor_mining.state import (
    factor_source_path,
    factor_source_stats,
    store_factor_source,
)

SRC = '''
import numpy as np
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(5) - 1.0).values
'''


def _bridge(tmp_path):
    from tests.test_timeout_guidance import _make_bridge

    return _make_bridge(tmp_path), tmp_path / "state"  # bridge 的 state root 在 state/ 子目录


# ---------------------------------------------------------------- 原语

def test_store_idempotent_and_roundtrip(tmp_path):
    r1 = store_factor_source(SRC, tmp_path)
    r2 = store_factor_source(SRC, tmp_path)
    assert r1["dedup"] is False and r2["dedup"] is True
    assert r1["hash"] == source_fingerprint(SRC)
    # 分片路径 + 全文回读一致
    p = factor_source_path(r1["hash"], tmp_path)
    assert p.parent.name == r1["hash"][:2]
    assert p.read_text(encoding="utf-8") == SRC
    s = factor_source_stats(tmp_path)
    assert s["count"] == 1 and s["bytes"] == len(SRC.encode("utf-8"))


def test_store_empty_source_safe(tmp_path):
    r = store_factor_source("", tmp_path)
    assert r["hash"] == source_fingerprint("")
    assert factor_source_path(r["hash"], tmp_path).read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------- 评估闭环

def test_evaluate_stores_source_and_trail_links(tmp_path):
    b, root = _bridge(tmp_path)
    diag = b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC,
                                          "stage": "development"})
    assert "error" not in diag or not diag.get("error")
    entries = json.loads((root / "trail_engine.json")
                         .read_text(encoding="utf-8"))
    h = entries[-1]["source_hash"]
    # trail 键 = 源码库键：可回溯全文
    assert factor_source_path(h, root).read_text(encoding="utf-8") == SRC
    # 账本本体不膨胀：trail 文件里不出现源码正文
    assert "def factor" not in (root / "trail_engine.json").read_text(encoding="utf-8")


def test_batch_stores_each_member_source(tmp_path):
    """batch 通道逐成员入库。注：因果门失败的成员在评估前就把整个
    batch 请求拒掉（既有 all-or-nothing 语义），故不构造坏成员。"""
    b, root = _bridge(tmp_path)
    other = SRC.replace("shift(5)", "shift(10)")
    b.dispatch("factor.evaluate_batch", {"envId": "primary",
                                         "sources": {"a": SRC, "b": other}})
    assert factor_source_path(source_fingerprint(SRC), root).exists()
    assert factor_source_path(source_fingerprint(other), root).exists()
    assert factor_source_stats(root)["count"] == 2


def test_class_a_rejected_stores_nothing(tmp_path):
    """A 类硬拒发生在评估前（不计 trial）——源码同样不入库（无账可挂）。"""
    b, root = _bridge(tmp_path)
    bad_src = SRC.replace("return (c / c.shift(5) - 1.0).values",
                          "v = c.applymap(lambda x: x)\n    return (c / c.shift(5) - 1.0).values")
    with pytest.raises(Exception):
        b.dispatch("factor.evaluate", {"envId": "primary", "source": bad_src,
                                       "stage": "development"})
    assert factor_source_stats(root)["count"] == 0


def test_trail_summary_discloses_store(tmp_path):
    b, root = _bridge(tmp_path)
    b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC,
                                   "stage": "development"})
    s = b.dispatch("state.trail_summary", {})
    assert s["source_store"]["count"] == 1
    assert "sources" in s["source_store"]["dir"]


def test_reset_mining_clears_sources_with_backup(tmp_path):
    b, root = _bridge(tmp_path)
    b.dispatch("factor.evaluate", {"envId": "primary", "source": SRC,
                                   "stage": "development"})
    assert factor_source_stats(root)["count"] == 1
    b.dispatch("state.reset", {"scope": "mining"})
    assert factor_source_stats(root)["count"] == 0
    # 备份在（sources 目录随挖掘轨迹备份清除，防旁路全史）
    backups = list((root / "backups").glob("*/sources"))
    assert backups, "reset 应把 sources/ 一并备份"


def test_explored_with_source_stores_fulltext(tmp_path):
    b, root = _bridge(tmp_path)
    b.dispatch("paths.append", {
        "layer": "explored",
        "entry": {
            "exploration": "test_双均线交叉无效",
            "evidence": "IC=0.01, p=0.9（20 轮）",
            "root_cause": "均线交叉在 ETF 日线上无预测力",
            "source": SRC,
        },
    })
    assert factor_source_path(source_fingerprint(SRC), root).exists()
