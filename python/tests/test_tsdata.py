# coding=utf-8
"""tsdata 合成数据单测（factor-mining-futures 线数据层，零网络）。

覆盖任务书 §三 / §八：
① G-roll 增量复权 point-in-time：roll_audit 6 截断全 bitwise_equal。
② 反例必被抓：故意全样本（末段口径一次性回溯）复权 → 审计 ok=False。
③ 主力判定：dominant 与构造的 OI 优势一致；换月恰在优势切换日。
④ 换月跳空被抹平：复权后换月日收益 ≠ 未复权原始跳空且更小。
⑤ G-limit：|c/c_prev-1|=0.06、limit_pct=0.05 → limit_hit_bars 命中该日。
⑥ fetch 超时 / 缺 hold 列 → TsDataError、无指纹落盘（fail-closed）。
⑦ fetch_and_build（注入合成 fetch_one）全链：parquet+指纹落盘、审计入指纹。

Run: cd python && python -m pytest tests/test_tsdata.py -x -q
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.factor import tsdata
from dsh_factor_mining.factor.env import TsEnv
from dsh_factor_mining.factor.evaluate_ts import limit_hit_bars
from dsh_factor_mining.factor.tsdata import (
    TsDataError,
    build_main_series,
    contract_universe,
    fetch_and_build,
    roll_audit,
)

N_PRE, N_MID, N_POST = 300, 300, 200          # A 主力 300 日 → B 300 日 → C 200 日
T_ALL = N_PRE + N_MID + N_POST
# 合约名取 rb 月码表内的真实形态（01/05/10）——fetch_and_build 走
# contract_universe("rb") 生成宇宙，夹具符号必须在宇宙内才拉得到数据
SYMS = ["RB1405", "RB1501", "RB1510"]


def _make_frames(n_pre: int = N_PRE, n_mid: int = N_MID, n_post: int = N_POST,
                 seed: int = 7) -> dict[str, pd.DataFrame]:
    """3 合约日线：OI 优势错开（A→B→C），各合约独立漂移价格，B 上线时
    与 A 有明显价差（确保换月跳空存在）。"""
    rng = np.random.default_rng(seed)
    T = n_pre + n_mid + n_post
    dates = pd.bdate_range("2014-01-02", periods=T)
    dominant = np.array([SYMS[0]] * n_pre + [SYMS[1]] * n_mid
                        + [SYMS[2]] * n_post)
    frames = {}
    for sym in SYMS:
        base = {"RB1405": 3000.0, "RB1501": 3450.0, "RB1510": 3200.0}[sym]
        drift = {"RB1405": 0.0002, "RB1501": -0.0001, "RB1510": 0.0003}[sym]
        close = base * np.cumprod(1.0 + rng.normal(drift, 0.008, T))
        op = close * (1.0 + rng.normal(0.0, 0.002, T))
        hi = np.maximum(op, close) * (1.0 + np.abs(rng.normal(0.0, 0.002, T)))
        lo = np.minimum(op, close) * (1.0 - np.abs(rng.normal(0.0, 0.002, T)))
        # OI 优势绝对悬殊（10 万级 vs 5 千级）——主力判定无歧义
        oi = np.where(dominant == sym,
                      100000.0 + rng.integers(0, 5000, T),
                      5000.0 + rng.integers(0, 5000, T)).astype(float)
        vol = (np.where(dominant == sym, 90000.0, 4000.0)
               + rng.integers(0, 3000, T)).astype(float)
        frames[sym] = pd.DataFrame({
            "date": dates, "open": op, "high": hi, "low": lo,
            "close": close, "volume": vol, "oi": oi})
    return frames


def _rows_from_frame(df: pd.DataFrame) -> list[dict]:
    """DataFrame → akshare 风格行（hold=持仓量；测试 _clean_rows 的 hold→oi）。"""
    return [
        {"date": str(pd.Timestamp(r["date"]).date()),
         "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"]),
         "volume": float(r["volume"]), "hold": float(r["oi"])}
        for _, r in df.iterrows()
    ]


# ---------------------------------------------------------------- ③ 主力判定
def test_dominant_and_roll_days():
    frames = _make_frames()
    s = build_main_series(frames)
    expected = np.array([SYMS[0]] * N_PRE + [SYMS[1]] * N_MID + [SYMS[2]] * N_POST)
    assert list(s["dominant"]) == list(expected)
    rolls = np.flatnonzero(s["roll"].to_numpy(dtype=bool))
    assert list(rolls) == [N_PRE, N_PRE + N_MID]     # 恰在优势切换日
    assert list(s.columns) == ["date", "open", "high", "low", "close",
                               "volume", "oi", "close2", "dominant", "roll"]
    # oi/volume 取 dominant 原始值（不跨合约调整）
    for t in (0, N_PRE, N_PRE + N_MID, T_ALL - 1):
        d = frames[s["dominant"].iloc[t]]
        row = d[d["date"] == s["date"].iloc[t]].iloc[0]
        assert s["oi"].iloc[t] == row["oi"]
        assert s["volume"].iloc[t] == row["volume"]


def test_contract_universe():
    u = contract_universe("rb", 2014, 2015)
    assert u == ["RB1401", "RB1405", "RB1410", "RB1501", "RB1505", "RB1510"]
    # 通用回退：1/5/9/10 全生成
    assert contract_universe("xx", 2014, 2014) == ["XX1401", "XX1405",
                                                   "XX1409", "XX1410"]
    with pytest.raises(TsDataError):
        contract_universe("", 2014, 2014)
    with pytest.raises(TsDataError):
        contract_universe("rb", 2016, 2014)


# ------------------------------------------------- ① G-roll point-in-time
def test_roll_audit_incremental_pointintime():
    audit = roll_audit(_make_frames(), n_checks=6, seed=42)
    assert audit["ok"] is True
    assert len(audit["checks"]) == 6
    assert all(c["bitwise_equal"] for c in audit["checks"])
    assert audit["n_roll_days"] == 2


# ------------------------------------------------- ② 反例：全样本复权必被抓
def test_roll_audit_catches_fullsample_backadjust(monkeypatch):
    frames = _make_frames()
    real = tsdata.build_main_series

    def cheat(fr, oi_col: str = "oi"):
        """故意全样本复权：以末段口径把每个 roll 的新/旧比率一次性回溯到
        此前全部历史（用最终段信息改写历史 = 前视）。"""
        base = real(fr, oi_col)                    # dominant/roll 仍用真规则
        dates = pd.DatetimeIndex(base["date"])
        dom = list(base["dominant"])
        T = len(base)
        close_at = {}
        for sym, d in fr.items():
            dd = d.copy()
            dd.index = pd.DatetimeIndex(dd["date"])
            close_at[sym] = dd["close"].astype(float)
        f = np.ones(T)
        for t in range(1, T):
            if bool(base["roll"].iloc[t]):
                a, b = dom[t - 1], dom[t]
                f[:t] *= float(close_at[b].loc[dates[t]]) / float(close_at[a].loc[dates[t]])
        out = base.copy()
        for col in ("open", "high", "low", "close", "close2"):
            out[col] = out[col].to_numpy(np.float64) * f
        return out

    monkeypatch.setattr(tsdata, "build_main_series", cheat)
    audit = roll_audit(frames, n_checks=6, seed=42)
    assert audit["ok"] is False
    assert any(not c["bitwise_equal"] for c in audit["checks"])


# ------------------------------------------------- ④ 换月跳空被抹平
def test_roll_jump_smoothed():
    frames = _make_frames()
    s = build_main_series(frames)
    for t in np.flatnonzero(s["roll"].to_numpy(dtype=bool)):
        a = s["dominant"].iloc[t - 1]
        b = s["dominant"].iloc[t]
        raw_a = frames[a][frames[a]["date"] == s["date"].iloc[t - 1]]["close"].iloc[0]
        raw_b = frames[b][frames[b]["date"] == s["date"].iloc[t]]["close"].iloc[0]
        raw_jump = float(raw_b) / float(raw_a) - 1.0
        adj = float(s["close"].iloc[t]) / float(s["close"].iloc[t - 1]) - 1.0
        assert abs(adj) < abs(raw_jump)          # 跳空被复权抹平
        assert not np.isclose(adj, raw_jump)     # 且确实不同于未复权跳空
    # 段内日收益与原始一致（乘性常数在比值中消去）
    seg = slice(10, N_PRE - 1)                   # A 段内部
    raw_ret = frames[SYMS[0]]["close"].to_numpy()[seg][1:] / \
        frames[SYMS[0]]["close"].to_numpy()[seg][:-1] - 1.0
    adj_ret = s["close"].to_numpy()[seg][1:] / s["close"].to_numpy()[seg][:-1] - 1.0
    assert np.allclose(raw_ret, adj_ret, rtol=0, atol=1e-12)


# ------------------------------------------------- ⑤ G-limit 命中
def test_limit_hit_on_built_env():
    s = build_main_series(_make_frames()).copy()
    k = T_ALL // 2                                # 非换月日
    s.loc[k, "close"] = float(s["close"].iloc[k - 1]) * 1.06   # |r|=6% > 5%
    s.loc[k + 1, "close"] = float(s["close"].iloc[k + 1]) * 1.06  # 抵消次日反跳
    env = TsEnv(
        o=s["open"].to_numpy(np.float64), h=s["high"].to_numpy(np.float64),
        l=s["low"].to_numpy(np.float64), c=s["close"].to_numpy(np.float64),
        v=s["volume"].to_numpy(np.float64), oi=s["oi"].to_numpy(np.float64),
        dates=list(pd.DatetimeIndex(s["date"])), instrument="rb",
        close2=s["close2"].to_numpy(np.float64), limit_pct=0.05)
    hits = limit_hit_bars(env)
    assert bool(hits[k])
    assert int(hits.sum()) == 1                   # 恰命中构造日（复权已抹平换月跳空）


# ------------------------------------------------- ⑥ fail-closed：超时/缺 hold
def test_fetch_timeout_fail_closed(tmp_path):
    def slow_fetch(sym):
        time.sleep(1.0)
        return []
    with pytest.raises(TsDataError, match="超时"):
        fetch_and_build(instrument="rb", start="2014-01-01", end="2014-12-31",
                        out_dir=tmp_path, timeout_s=0.2, fetch_one=slow_fetch)
    assert not (tmp_path / "tsdata_fingerprint.json").exists()
    assert not any(tmp_path.iterdir())            # 无任何落盘


def test_fetch_missing_hold_fail_closed(tmp_path):
    def no_hold(sym):
        return [{"date": "2014-01-02", "open": 3000.0, "high": 3050.0,
                 "low": 2980.0, "close": 3010.0, "volume": 123.0}]  # 无 hold
    with pytest.raises(TsDataError):
        fetch_and_build(instrument="rb", start="2014-01-01", end="2014-12-31",
                        out_dir=tmp_path, fetch_one=no_hold)
    assert not (tmp_path / "tsdata_fingerprint.json").exists()
    assert not any(tmp_path.iterdir())


# ------------------------------------------------- ⑦ 全链落盘 + 指纹
def test_fetch_and_build_full_chain(tmp_path):
    frames = _make_frames()
    rows_map = {sym: _rows_from_frame(d) for sym, d in frames.items()}

    def fake_fetch(sym):
        return rows_map.get(sym, [])              # 宇宙中其余合约：无数据（跳过）

    start = str(pd.Timestamp(frames[SYMS[0]]["date"].iloc[0]).date())
    end = str(pd.Timestamp(frames[SYMS[2]]["date"].iloc[-1]).date())
    summary = fetch_and_build(instrument="rb", start=start, end=end,
                              out_dir=tmp_path, fetch_one=fake_fetch)

    pq = tmp_path / "rb_main_daily.parquet"
    fp_path = tmp_path / "tsdata_fingerprint.json"
    assert pq.exists() and fp_path.exists()
    fp = json.loads(fp_path.read_text(encoding="utf-8"))
    assert fp["instrument"] == "rb"
    assert fp["source"] == "akshare-sina"
    assert fp["rows"] == T_ALL
    assert fp["n_roll_days"] == 2
    assert fp["audit"]["ok"] is True              # 审计信息入指纹
    assert fp["sha256"] == hashlib.sha256(pq.read_bytes()).hexdigest()[:16]
    assert fp["columns"] == ["date", "open", "high", "low", "close",
                             "volume", "oi", "close2", "dominant", "roll"]
    assert "built_at" in fp

    df = pd.read_parquet(pq)
    assert len(df) == T_ALL
    assert int(df["roll"].sum()) == 2
    expected = [SYMS[0]] * N_PRE + [SYMS[1]] * N_MID + [SYMS[2]] * N_POST
    assert list(df["dominant"]) == expected
    assert summary["rows"] == T_ALL and summary["n_roll_days"] == 2
    assert summary["audit"]["ok"] is True
    assert summary["skipped_contracts"] == {}     # 无数据 ≠ 失败
    assert set(summary["contracts_used"]) == set(SYMS)
