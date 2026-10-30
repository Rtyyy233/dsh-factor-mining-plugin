# coding=utf-8
"""并行线（lane）身份 — 2026-08-31 方案 A（并行会话语义隔离）。

背景：dsh web 一个 host 只有一个共享 Python bridge 进程，多会话的
factor_* 调用全部从它走。写完整性有 filelock（P0-P5），但引擎的意图层
状态（pending_hypothesis 义务 / 家族链 streak / arc 方向预算 / frozen
检测）建立在单叙述者假设上——并行会话互相接管对方的「下一步」。

方案 A 的隔离边界（用户拍板 2026-08-31）：
- 意图层（本模块服务的对象）：按 lane 隔离——pending/族链/arc 预算/
  frozen 只看本线条目。
- 事实与计价层：保持全局——n_trials/deflation/tail bar/explored 证伪
  记忆/method_suspect/registry。并行线知道彼此的证伪结果是收益不是
  污染；多重检验计价必须全量计数（state.py 既有设计定案）。

lane 解析链：请求参数 lane（TS 层盖会话 id）→ 环境变量
DSH_FACTOR_MINING_LANE（直连 CLI/ZCode 并行会话用）→ DEFAULT_LANE。

条目归属：无 lane 字段的历史条目一律归 DEFAULT_LANE（= 单线时代的
唯一叙述者，数据不迁移）。bridge 是单线程 dispatch，contextvar 在
请求生命周期内安全贯穿；异步重方法的 trail 写入发生在轮询请求内
（_async_result → _wrap_diagnosis），同样被覆盖。
"""
from __future__ import annotations

import os
import re
from contextvars import ContextVar, Token
from typing import Any, Iterable

DEFAULT_LANE = "default"
ENV_VAR = "DSH_FACTOR_MINING_LANE"

_LANE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_LEN = 48

_lane_ctx: ContextVar[str | None] = ContextVar("dsh_factor_mining_lane",
                                               default=None)


def normalize_lane(raw: Any) -> str | None:
    """清洗外部 lane 字符串（dsh session id 是 UUID，含非文件系统安全字符）。
    非法/空输入 → None（= 回落到进程默认链）。"""
    if not isinstance(raw, str):
        return None
    s = _LANE_RE.sub("-", raw.strip())[:_MAX_LEN].strip("-")
    return s or None


def lane_from_env() -> str | None:
    return normalize_lane(os.environ.get(ENV_VAR))


def resolve_request_lane(params: Any) -> str | None:
    """dispatch 入口：从请求参数摘 lane。请求级 lane 优先于环境变量——
    共享 bridge 下每个请求携带各自会话的身份。"""
    if not isinstance(params, dict):
        return None
    return normalize_lane(params.get("lane") or params.get("_lane"))


def strip_lane_params(params: Any) -> Any:
    """摘掉请求里的 lane 键再交给方法处理器（防严格参数校验炸）。"""
    if not isinstance(params, dict):
        return params
    return {k: v for k, v in params.items() if k not in ("lane", "_lane")}


def set_request_lane(lane: str | None) -> ContextToken:
    return _lane_ctx.set(lane)


def reset_request_lane(token: ContextToken) -> None:
    _lane_ctx.reset(token)


def current_lane() -> str:
    """当前请求归属的 lane（写入打标与读端过滤的统一口径）。"""
    return _lane_ctx.get() or lane_from_env() or DEFAULT_LANE


def entry_lane(entry: Any) -> str:
    """条目归属：无标记 = DEFAULT_LANE（历史数据不迁移的既定决策）。"""
    if isinstance(entry, dict):
        v = entry.get("lane")
        if isinstance(v, str) and v:
            return v
    return DEFAULT_LANE


def own_entries(entries: Iterable[Any], lane: str | None = None) -> list[Any]:
    """本线条目（时间序保持）。"""
    lane = lane or current_lane()
    return [e for e in entries if entry_lane(e) == lane]
