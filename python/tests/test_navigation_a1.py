# coding=utf-8
"""A1 逃逸距离遥测 + B2 不相似示例选择的单元测试（2026-10-14 任务书）。

词法红线：提取器只做词法匹配（正则/词边界），不做语义理解。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.navigation import (  # noqa: E402
    dissimilar_filter, escape_distance, extract_blocks, first_reaction_segment,
    jaccard_distance, jaccard_overlap,
)

SRC = """
# tree: x | expression: ts_mean[5](add(cs_rank(v), absx(v)))
def factor(env):
    import pandas as pd
    from dsh_factor_mining.factor.ops import absx, add, cs_rank, ts_mean
    return (ts_mean(add(cs_rank(pd.DataFrame(env.v)), absx(pd.DataFrame(env.v))), 5)).values
"""


# ---- 提取器 ----

def test_extract_blocks_code_side():
    b = extract_blocks(SRC, code=True)
    assert b == {"ts_mean", "add", "cs_rank", "absx", "volume"}, b


def test_extract_blocks_text_side():
    b = extract_blocks("用 ts_rank 对 close 做时序分位，再乘 volume；"
                       "高位价 high 也要", code=False)
    assert b == {"ts_rank", "close", "volume", "high"}, b


def test_extract_blocks_empty_and_noise():
    assert extract_blocks("", code=True) == set()
    assert extract_blocks(None) == set()
    # 散文里的单字母（o/h/l/c/v）不匹配——文本侧只认全名/别名
    assert extract_blocks("c 位很好 v 量不大", code=False) == set()
    # 词边界：ts_meanx 不是 ts_mean
    assert extract_blocks("ts_meanx 是别的") == set()


def test_extract_blocks_param_variants_same_pattern():
    # 参数不同不影响构造块集合（模式抽象的词法基础）
    a = extract_blocks("ts_mean(env.c, 5)", code=True)
    b = extract_blocks("ts_mean(env.c, 250)", code=True)
    assert a == b == {"ts_mean", "close"}


# ---- 距离 ----

def test_jaccard_distance_null_on_empty():
    assert jaccard_distance(set(), {"a"}) is None
    assert jaccard_distance({"a"}, set()) is None
    assert jaccard_distance(set(), set()) is None


def test_jaccard_distance_values():
    assert jaccard_distance({"a"}, {"a"}) == 0.0
    assert jaccard_distance({"a"}, {"b"}) == 1.0
    d = jaccard_distance({"a", "b"}, {"b", "c"})
    assert abs(d - 2 / 3) < 1e-9


def test_escape_distance_full_cycle():
    entry = ("## H-1 registered 反转假设\n"
             "ts:2026-09-10T10:00:00\n"
             "ref:abc123def456\n"
             "第一反应：动量延续 ts_momentum(close)，预期半月内衰减\n")
    src = "def factor(env):\n    return ts_momentum(env.c, 20)\n"
    d = escape_distance(entry, src)
    assert d == 0.0      # 第一反应块集与最终源码块集一致=未逃逸
    # 最终换成完全不同的构造 → 距离拉满
    src2 = "def factor(env):\n    return ts_skew(env.volume, 60)\n"
    d2 = escape_distance(entry, src2)
    assert d2 == 1.0


def test_escape_distance_null_when_no_first_reaction():
    # 无正文段落（只有元行）→ None，不记 0
    entry = "## H-1 registered x\nref:abc123def456\n"
    assert escape_distance(entry, SRC) is None
    assert escape_distance("", SRC) is None


def test_first_reaction_marker_priority_and_fallback():
    marked = ("## H-1 open t\n"
              "开场白段落\n\n"
              "第一反应：从 vol 入手的惯性思路\n\n"
              "后面的正式推理 close 反转\n")
    assert "vol" in first_reaction_segment(marked)
    # 无标记 → 首段约定
    seg = first_reaction_segment("## H-1 open t\n预期:close 反转\n\n第二段")
    assert seg == "预期:close 反转"


# ---- B2 过滤器 ----

def _c(src):
    return {"source": src}


def test_dissimilar_filter_prunes_overlap_half():
    clones = [_c("ts_mean(env.c, 5)") for _ in range(4)]   # 独立对象
    distinct = [_c("ts_skew(env.volume, 60)")]
    cands = clones + distinct
    out = dissimilar_filter(cands, ["ts_mean(env.c, 10)"], k=1, prune=0.5)
    # 5 候选剔除重合度最高的前 50%（4 个克隆中 2 个先被剔除）
    assert len(out) == 3
    assert distinct[0] in out
    kept_clones = [c for c in out if c in clones]
    assert len(kept_clones) == 2


def test_dissimilar_filter_passthrough_cases():
    cands = [_c("ts_mean(env.c)")]
    assert dissimilar_filter(cands, [], k=5) == cands       # 无近期参照
    assert dissimilar_filter([], ["x"], k=5) == []           # 无候选
    # prune=0 关闭过滤
    out = dissimilar_filter([_c("a(env.c)")], ["a(env.c)"], k=1, prune=0)
    assert len(out) == 1


def test_dissimilar_filter_preserves_order():
    a, b, c = _c("ts_a(env.c)"), _c("ts_b(env.h)"), _c("ts_a(env.c)")
    out = dissimilar_filter([a, b, c], ["ts_a(env.c)"], k=1, prune=0.34)
    assert out == [b, c] or out == [a, b]     # 剔除 1 个与近期重合者，余序不变


def test_jaccard_overlap_conventions():
    assert jaccard_overlap(set(), set()) == 1.0
    assert jaccard_overlap({"a"}, set()) == 0.0
    assert abs(jaccard_overlap({"a", "b"}, {"b", "c"}) - 1 / 3) < 1e-9


if __name__ == "__main__":
    for fn in [test_extract_blocks_code_side, test_extract_blocks_text_side,
               test_extract_blocks_empty_and_noise,
               test_extract_blocks_param_variants_same_pattern,
               test_jaccard_distance_null_on_empty, test_jaccard_distance_values,
               test_escape_distance_full_cycle,
               test_escape_distance_null_when_no_first_reaction,
               test_first_reaction_marker_priority_and_fallback,
               test_dissimilar_filter_prunes_overlap_half,
               test_dissimilar_filter_passthrough_cases,
               test_dissimilar_filter_preserves_order,
               test_jaccard_overlap_conventions]:
        fn()
        print(f"ok {fn.__name__}")
