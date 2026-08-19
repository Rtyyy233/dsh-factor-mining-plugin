# coding=utf-8
"""实测反馈回归测试（2026-08-18 两轮 DSH 实测暴露的问题）：

A1 probe 采样偏差 → 聚簇 parquet 全表统计
A2 requireFiniteOhlcv 误伤 unbalanced 面板 → 上市对齐语义
A3 config_write 静默空壳 + WinError 1921 → 严格化拒绝 + 路径防御
B  envId primary 诱导改名 → 单环境智能 fallback；library 段应用

Run: cd python && python -m pytest tests/test_field_feedback.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError


def _clustered_panel(path: Path, a_rows=3000, b_rows=1000) -> None:
    """聚簇存储：ETF A 的 3000 行全部在前，ETF B 的 1000 行在后（复刻真实 parquet）。"""
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2023-01-02", periods=a_rows)

    def rows(sym: str, dates_):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, len(dates_)))
        return [{"eob": d, "symbol": sym, "open": p * 1.001, "high": p * 1.01,
                 "low": p * 0.99, "close": p, "volume": 1000.0, "amount": 10000.0}
                for d, p in zip(dates_, np.maximum(c, 0.5))]

    df = pd.DataFrame(rows("ETF_A", dates) + rows("ETF_B", dates[-b_rows:]))
    df.to_parquet(path, index=False)


def _env(data_path, **overrides) -> dict:
    spec = {
        "label": "synthetic", "kind": "panel",
        "source": {"type": "parquet", "path": str(data_path), "options": {}},
        "layout": "long",
        "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "volume", "amount": "amount"},
        "constraints": {"minSymbols": 1, "minDates": 50,
                        "requireFiniteOhlcv": True, "allowZeroVolume": True},
    }
    spec.update(overrides)
    return {"version": 1, "environments": {"etf": spec}}


def test_probe_parquet_full_coverage():
    """A1：聚簇 parquet 的前 2000 行只见 1 只 ETF——probe 必须报全表真实标的数。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "clustered.parquet"
        _clustered_panel(data)
        b = Bridge(state_root=str(root / "state"))
        probe = b.dispatch("data.probe", {"path": str(data)})
        assert probe["ok"] is True, probe
        assert probe["coverage"] == "full", probe.get("coverage")
        assert probe["symbol_count"] == 2, probe["symbol_count"]  # 修复前 head(2000) 只见 1
        assert probe["rows_total"] == 4000, probe["rows_total"]


def test_probe_rejects_directory():
    """A3 防御：目录路径明确拒绝（WinError 1921 根因链的一环）。"""
    with tempfile.TemporaryDirectory() as d:
        b = Bridge(state_root=str(Path(d) / "state"))
        probe = b.dispatch("data.probe", {"path": d})
        assert probe["ok"] is False
        assert "目录" in probe["error"], probe["error"]


def test_config_write_rejects_field_feedback_payload():
    """A3：复刻实测第一次调用的 payload（根级 path/symbol_col/description/known_factors_path）
    ——必须报错拒绝且不落盘，而不是静默写空壳。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _clustered_panel(data, a_rows=200, b_rows=100)
        state = root / "state"
        b = Bridge(state_root=str(state))
        payload = {  # 用户实测原文结构
            "version": 1,
            "environments": {
                "etf": {"id": "etf", "path": str(data), "layout": "long",
                        "symbol_col": "symbol", "date_col": "eob",
                        "open_col": "open", "high_col": "high", "low_col": "low",
                        "close_col": "close", "volume_col": "volume",
                        "amount_col": "amount", "description": "ETF日线"},
            },
            "known_factors_path": "C:/nonexistent/known_factors.py",
        }
        try:
            b.dispatch("config.save", {"config": json.dumps(payload)})
            raise AssertionError("schema 外字段必须被拒绝（静默空壳 bug 回归）")
        except BridgeError as e:
            assert e.code == -32002, e.code
            msg = str(e.message)
            assert "未知字段" in msg, msg
            assert "source.path" in msg, msg
        assert not (state / "data-config.json").exists(), "拒绝时不落盘"


def test_config_write_rejects_empty_and_list_environments():
    with tempfile.TemporaryDirectory() as d:
        b = Bridge(state_root=str(Path(d) / "state"))
        for bad in ({"environments": []}, {"environments": {}}, {}):
            try:
                b.dispatch("config.save", {"config": bad})
                raise AssertionError(f"应拒绝: {bad}")
            except BridgeError as e:
                assert e.code == -32002


def test_require_finite_accepts_unbalanced_panel():
    """A2：ETF B 上市晚 150 天 → 上市前 NaN 合法，strict 模式也应通过。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "unbalanced.parquet"
        rng = np.random.default_rng(3)
        dates = pd.bdate_range("2023-01-02", periods=300)
        rows = []
        for sym, start in (("ETF_A", 0), ("ETF_B", 150)):
            c = 10 + np.cumsum(rng.normal(0.001, 0.01, len(dates) - start))
            for dt, p in zip(dates[start:], np.maximum(c, 0.5)):
                rows.append({"eob": dt, "symbol": sym, "open": p * 1.001, "high": p * 1.01,
                             "low": p * 0.99, "close": p, "volume": 1000.0, "amount": 10000.0})
        pd.DataFrame(rows).to_parquet(data, index=False)
        b = Bridge(state_root=str(root / "state"))
        b.dispatch("config.save", {"config": _env(data)})
        loaded = b.dispatch("data.load", {"envId": "etf"})
        assert loaded["ok"] is True, loaded
        assert loaded["symbols"] == 2


def test_require_finite_report_mode_default():
    """A2 修正：默认 report 模式——停牌洞不阻断，dataQuality 摘要进 load 返回。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "suspended.parquet"
        rng = np.random.default_rng(3)
        dates = pd.bdate_range("2023-01-02", periods=300)
        rows = []
        for sym in ("STK_A", "STK_B"):
            c = 10 + np.cumsum(rng.normal(0.001, 0.01, len(dates)))
            for dt, p in zip(dates, np.maximum(c, 0.5)):
                rows.append({"eob": dt, "symbol": sym, "open": p * 1.001, "high": p * 1.01,
                             "low": p * 0.99, "close": p, "volume": 1000.0, "amount": 10000.0})
        df = pd.DataFrame(rows)
        # STK_B 中间停牌 10 天（有效期内洞）
        hole = (df["symbol"] == "STK_B") & (df["eob"].isin(dates[100:110]))
        df.loc[hole, ["open", "high", "low", "close", "volume"]] = np.nan
        # STK_B 第 280 天后退市（右对齐合法缺口）
        delist = (df["symbol"] == "STK_B") & (df["eob"] >= dates[280])
        df.loc[delist, ["open", "high", "low", "close", "volume"]] = np.nan
        df.to_parquet(data, index=False)
        b = Bridge(state_root=str(root / "state"))
        cfg = _env(data)  # 默认 requireFiniteOhlcv 缺省 = report
        cfg["environments"]["etf"]["constraints"]["requireFiniteOhlcv"] = "report"
        b.dispatch("config.save", {"config": cfg})
        loaded = b.dispatch("data.load", {"envId": "etf"})
        assert loaded["ok"] is True, "report 模式停牌洞不阻断"
        dq = loaded["dataQuality"]
        assert dq["inWindowHoles"] == 10, dq       # 停牌洞被正确归类
        assert dq["postLastValidHoles"] > 0, dq    # 退市尾巴右对齐归类


def test_require_finite_rejects_post_listing_hole():
    """A2 反向：strict 模式下有效期内挖洞（真脏数据）→ 必须报"有效期内"。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "hole.parquet"
        rng = np.random.default_rng(3)
        dates = pd.bdate_range("2023-01-02", periods=300)
        rows = []
        for sym in ("ETF_A", "ETF_B"):
            c = 10 + np.cumsum(rng.normal(0.001, 0.01, len(dates)))
            for dt, p in zip(dates, np.maximum(c, 0.5)):
                rows.append({"eob": dt, "symbol": sym, "open": p * 1.001, "high": p * 1.01,
                             "low": p * 0.99, "close": p, "volume": 1000.0, "amount": 10000.0})
        df = pd.DataFrame(rows)
        mask = (df["symbol"] == "ETF_B") & (df["eob"] == dates[200])
        df.loc[mask, "close"] = np.nan
        df.to_parquet(data, index=False)
        b = Bridge(state_root=str(root / "state"))
        b.dispatch("config.save", {"config": _env(data)})
        try:
            b.dispatch("data.load", {"envId": "etf"})
            raise AssertionError("strict 模式有效期内缺失必须报错")
        except BridgeError as e:
            assert "有效期内" in e.message, e.message


def test_envid_fallback_single_env():
    """B：环境名是 etf 不是 primary；缺省/primary 请求应回退到唯一环境。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _clustered_panel(data, a_rows=200, b_rows=100)
        b = Bridge(state_root=str(root / "state"))
        b.dispatch("config.save", {"config": _env(data)})
        loaded = b.dispatch("data.load", {"envId": "primary"})  # primary 不存在但唯一环境
        assert loaded["ok"] is True and loaded["envId"] == "etf", loaded
        # 明确请求不存在的名字（多环境语义的防打错字）——单环境时也报错
        try:
            b.dispatch("data.load", {"envId": "stock"})
            raise AssertionError("不存在的环境名应报错")
        except BridgeError as e:
            assert "etf" in e.message, e.message


def test_library_section_applied():
    """B：config 顶层 library 段 → UserLibrary 生效（status.libraryConfigured=true）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _clustered_panel(data, a_rows=200, b_rows=100)
        lib = root / "known_factors.py"
        lib.write_text(
            "def list_known_factors():\n"
            "    return [{'name': 'momentum20', 'signal': '20日动量'}]\n",
            encoding="utf-8")
        cfg = _env(data)
        cfg["library"] = {"path": str(lib)}  # type 缺省按 .py 后缀推断
        b = Bridge(state_root=str(root / "state"))
        b.dispatch("config.save", {"config": cfg})
        st = b.dispatch("status", {})
        assert st["libraryConfigured"] is True, st
        q = b.dispatch("library.query", {"query": "动量"})
        assert q["configured"] is True and len(q["hits"]) >= 1, q
