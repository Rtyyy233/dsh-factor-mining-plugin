# coding=utf-8
"""冷启动链路测试：文件约定路径 + json 参数双态 + 进程重启状态重建 + v0 检测 + 惰性重读。

对应 2026-08-18 DSH 实测暴露的四 bug 修复（测试动线 = 真实用户动线）：
1. config_write 无 path → 现在默认写 stateRoot/data-config.json
2. config 为 JSON 字符串 → 双态归一化（对象或字符串都接受）
3. 手动写配置文件不被识别 → 约定路径启动发现 + mtime 惰性重读
4. 旧 v0 格式静默失败 → 明确报"旧格式 v0"且服务保持可用

Run: cd python && python -m pytest tests/test_cold_start.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError


def _panel(path: Path, T=300, N=20) -> None:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2023-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:03d}", "open": p * 1.001,
                         "high": p * 1.01, "low": p * 0.99, "close": p,
                         "volume": float(rng.integers(100, 9999)),
                         "amount": float(rng.integers(1000, 99999))})
    pd.DataFrame(rows).to_parquet(path)


def _env_spec(data_path: str) -> dict:
    return {
        "label": "synthetic", "kind": "panel",
        "source": {"type": "parquet", "path": data_path, "options": {}},
        "layout": "long",
        "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                    "low": "low", "close": "close", "volume": "volume", "amount": "amount"},
        "constraints": {"minSymbols": 10, "minDates": 100,
                        "requireFiniteOhlcv": True, "allowZeroVolume": True},
    }


def _config(data_path: str, with_alt: bool = False) -> dict:
    envs = {"primary": _env_spec(data_path)}
    if with_alt:
        envs["alt"] = _env_spec(data_path)
    return {"version": 1, "environments": envs}


def test_convention_path_and_next_step():
    """未配置状态自描述：约定路径 + nextStep=factor_data_probe + 环境自检字段。"""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "state"
        b = Bridge(state_root=str(state))
        st = b.dispatch("status", {})
        assert st["dataConfigPath"] == str(state / "data-config.json"), st["dataConfigPath"]
        assert st["dataConfigured"] is False
        assert st["nextStep"] == "factor_data_probe"
        assert st["pythonExecutable"], st
        assert st["bridgeModulePath"], st


def test_config_write_string_config_no_path_then_load():
    """bug①②回归：无 path + config 为 JSON 字符串 → 写约定路径并立即可加载。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _panel(data)
        state = root / "state"
        b = Bridge(state_root=str(state))
        res = b.dispatch("config.save", {"config": json.dumps(_config(str(data)))})
        assert res["ok"] is True, res
        assert res["path"] == str(state / "data-config.json"), res
        st = b.dispatch("status", {})
        assert st["dataConfigured"] is True
        assert st["nextStep"] == "factor_load_env", st
        assert [e["id"] for e in st["environments"]] == ["primary"]
        loaded = b.dispatch("data.load", {"envId": "primary"})
        assert loaded["ok"] is True and loaded["T"] == 300, loaded
        # 加载后 nextStep 清空（全部就绪）
        assert b.dispatch("status", {})["nextStep"] is None


def test_state_rebuild_after_restart():
    """文件即事实源：进程重启（新 Bridge，无显式 data_config_path）从约定路径恢复。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _panel(data)
        state = root / "state"
        b1 = Bridge(state_root=str(state))
        b1.dispatch("config.save", {"config": json.dumps(_config(str(data)))})
        b2 = Bridge(state_root=str(state))  # "重启"
        st = b2.dispatch("status", {})
        assert st["dataConfigured"] is True, "重启后应从约定路径文件重建配置状态"
        assert b2.dispatch("data.load", {"envId": "primary"})["ok"] is True


def test_manual_edit_lazy_reload():
    """bug④回归：手动编辑约定路径文件 → status/data.load 惰性重读，无需重启。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _panel(data)
        state = root / "state"
        b = Bridge(state_root=str(state))
        b.dispatch("config.save", {"config": json.dumps(_config(str(data)))})
        # 手动加第二个环境
        cfg_file = state / "data-config.json"
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        cfg["environments"]["alt"] = _env_spec(str(data))
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        os.utime(cfg_file, ns=(0, 0))  # 强制 mtime 变化（避免时间戳精度问题）
        st = b.dispatch("status", {})
        assert {e["id"] for e in st["environments"]} == {"primary", "alt"}, st["environments"]


def test_v0_config_rejected_but_service_alive():
    """bug⑤回归：v0 旧格式（data_root/file_index）明确报错且服务不崩。"""
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "state"
        state.mkdir(parents=True)
        (state / "data-config.json").write_text(
            json.dumps({"data_root": "x", "file_index": []}), encoding="utf-8")
        b = Bridge(state_root=str(state))
        st = b.dispatch("status", {})
        assert st["ready"] is True, "坏配置不应让服务不可用"
        assert st["dataConfigured"] is False
        assert "v0" in (st["configError"] or ""), st["configError"]
        try:
            b.dispatch("data.load", {"envId": "primary"})
            raise AssertionError("v0 配置下 load 应报 DATA_CONFIG_REQUIRED")
        except BridgeError as e:
            assert e.code == -32001


def test_bad_json_string_error():
    """双态归一化的失败路径：JSON 字符串解析失败 → -32602 带字段名。"""
    with tempfile.TemporaryDirectory() as d:
        b = Bridge(state_root=str(Path(d) / "state"))
        try:
            b.dispatch("config.save", {"config": "{not json"})
            raise AssertionError("坏 JSON 字符串应报错")
        except BridgeError as e:
            assert e.code == -32602, e.code
            assert "config" in e.message, e.message


def test_config_validate_accepts_string():
    """config.validate 同样接受字符串形态（LLM 双态契约全覆盖）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        data = root / "panel.parquet"
        _panel(data)
        b = Bridge(state_root=str(root / "state"))
        res = b.dispatch("config.validate", {"config": json.dumps(_config(str(data)))})
        assert res["ok"] is True, res


def test_operators_override_accepts_string():
    """factor.operators set 的 override 同样接受字符串形态。"""
    with tempfile.TemporaryDirectory() as d:
        b = Bridge(state_root=str(Path(d) / "state"))
        res = b.dispatch("factor.operators",
                         {"action": "set", "override": json.dumps({"disable": ["ts_skew"]})})
        assert res["disabled"] == ["ts_skew"], res
