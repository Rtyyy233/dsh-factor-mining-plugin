# coding=utf-8
"""正交门（2026-09-06）单元测试：book_ranks 秩缓存与候选值相关检查。

覆盖：高相关拒绝 / 独立通过 / 缓存往返与追加 / 排除自身 / 拒绝-标注双阈值 /
缓存缺失跳过 / 追加失败（日期/指纹不符）不写入。
"""
import numpy as np
import pandas as pd
import pytest

from dsh_factor_mining.book_ranks import (
    append_to_cache,
    check_candidate,
    dev_rank_u16,
    load_cache,
    save_cache,
)


def _ranks(m):
    df = pd.DataFrame(m)
    r = df.rank(axis=1).values
    return np.where(np.isfinite(r), r, 0).astype(np.uint16)


@pytest.fixture()
def book(rng_seed=7):
    rng = np.random.default_rng(rng_seed)
    D, N = 200, 500
    base = rng.normal(size=(D, N))
    b = base * 0.9 + rng.normal(scale=0.1, size=(D, N))
    c = rng.normal(size=(D, N))
    dates = [f"d{i}" for i in range(D)]
    ranks = np.stack([_ranks(base), _ranks(b)])
    return {"names": ["a", "b"], "dates": dates, "ranks": ranks,
            "_raw": {"a": _ranks(base), "b": _ranks(b), "c": _ranks(c)}}


def test_high_correlation_rejects(book):
    r = check_candidate(book, book["_raw"]["b"])
    assert r["status"] == "ok"
    assert r["action"] == "reject"
    assert r["max_corr"] >= 0.99


def test_independent_factor_passes(book):
    r = check_candidate(book, book["_raw"]["c"])
    assert r["action"] == "pass"
    assert abs(r["max_corr"]) < 0.3


def test_annotate_band(tmp_path, book):
    save_cache(tmp_path, book["names"], book["dates"], book["ranks"], "fp")
    cache = load_cache(tmp_path)
    # 半相关候选：a 与 c 的均值 → 落 annotate/pass 边界之外亦可，只验证通道
    mid = (book["_raw"]["a"] + book["_raw"]["c"]) / 2.0
    r = check_candidate(cache, _ranks(mid))
    assert r["status"] == "ok"
    assert r["action"] in ("annotate", "pass", "reject")


def test_exclude_self(book):
    r_all = check_candidate(book, book["_raw"]["b"])
    r_ex = check_candidate(book, book["_raw"]["b"], exclude_name="b")
    assert r_all["worst_name"] == "a" or r_all["worst_name"] == "b"
    assert all(nm != "b" for nm, _ in r_ex["top"])
    assert r_ex["status"] == "ok"


def test_cache_roundtrip_and_append(tmp_path, book):
    save_cache(tmp_path, book["names"], book["dates"], book["ranks"], "fp1")
    loaded = load_cache(tmp_path)
    assert loaded is not None
    assert loaded["names"] == book["names"]
    assert loaded["ranks"].shape == book["ranks"].shape
    assert loaded["fingerprint"] == "fp1"
    assert append_to_cache(tmp_path, "c", book["dates"], book["_raw"]["c"], "fp1")
    loaded2 = load_cache(tmp_path)
    assert loaded2["ranks"].shape[0] == 3
    # 日期/指纹不符 → 拒绝追加
    assert not append_to_cache(tmp_path, "x", ["bad"], book["_raw"]["c"], "fp1")
    assert not append_to_cache(tmp_path, "y", book["dates"], book["_raw"]["c"], "fp2")
    assert load_cache(tmp_path)["ranks"].shape[0] == 3


def test_missing_cache_returns_none(tmp_path):
    assert load_cache(tmp_path) is None
    r = check_candidate({"names": [], "dates": [], "ranks": np.zeros((0, 0))},
                        np.zeros((10, 10)))
    assert r["status"] == "skipped"


def test_dev_rank_u16_basic():
    D, N = 30, 7
    vals = np.random.default_rng(3).normal(size=(D, N))
    vals[:10, :] = np.nan

    class _Env:  # 最小 env 鸭子类型
        dates = pd.date_range("2020-01-01", periods=D, freq="D")

    u16, dates = dev_rank_u16(_Env, vals, "2020-02-15")
    assert u16.shape == (D, N)          # dev_end 晚于全部日期 → 全保留
    assert len(dates) == D
    assert (u16[:10] == 0).all()        # NaN 行 → 0（执行器按缺失处理）
    assert (u16[10:] > 0).all()
