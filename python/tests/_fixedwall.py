# -*- coding: utf-8 -*-
"""固定墙时代测试助手（PLAN-fixed-wall-rollout-2026-11-01 任务 E1）。

生产入口=selection 评估自动开票（B4 记账与开票同事务）；单测直开凭据
绕过预算面（预算/封区语义由 test_hardening_fixedwall 专项覆盖）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.bridge import source_fingerprint  # noqa: E402
from dsh_factor_mining.factor.hardening import open_oos_receipt  # noqa: E402


def grant_oos(state_root, source, kind: str = "test_grant") -> None:
    """为一份 source 直开 OOS 凭据（submit 的 B3 硬前置）。"""
    open_oos_receipt(state_root, source_fingerprint(source), kind,
                     {"note": "测试直开（tests/_fixedwall.py grant_oos）"})
