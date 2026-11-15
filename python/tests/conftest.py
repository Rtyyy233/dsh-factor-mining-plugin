# coding=utf-8
"""测试共享夹具（2026-11-05 段隔离任务书 E）。

段验证（SPRT）是提交路径的 fail-closed 前置：segments.json 缺失时
提交被"验证管线暂不可用"事务中止——这是生产行为。测试桥一律自动
注入合法签名的合成段定义（全覆盖窗口），使提交路径测试无需每文件
自备段文件；段冻结/防篡改语义由专门单测自写自测（覆盖注入件）。
"""
from __future__ import annotations

import json
from pathlib import Path

from dsh_factor_mining.bridge import Bridge
from dsh_factor_mining.factor.verify import write_synthetic_segments

_orig_init = Bridge.__init__


def _init_with_segments(self, *args, **kwargs):
    _orig_init(self, *args, **kwargs)
    try:
        write_synthetic_segments(self.state_root)
    except Exception:  # noqa: BLE001 — 已有段文件（专项测试自写）则跳过
        pass


Bridge.__init__ = _init_with_segments


def set_verify_mode(state_root, mode: str = "off") -> None:
    """测试辅助：读写 mining_state 的 verify_mode（回滚开关的合法用途——
    行为保持型测试（考 ts/origin/loop 等无关特征）关掉段验证，把 SPRT
    判定留给专项单测）。"""
    p = Path(state_root) / "mining_state.json"
    d = {}
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    d["verify_mode"] = mode
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
