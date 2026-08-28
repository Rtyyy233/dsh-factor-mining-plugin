# coding=utf-8
"""策略指纹（规划 §6：键 = 指纹五元组）。

fingerprint = (factor_refs, 规则源 hash, 参数 hash, wf 配置 hash,
cost_model_version)。同组件同 hash、任一组件变则变（测试锁死）；
跨成本档结果不可比由 cost_model_version 进指纹保证。
"""
from __future__ import annotations

import hashlib
import json


def _sha(obj) -> str:
    canonical = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_factor_refs(factor_refs) -> list[dict]:
    """因子引用规范化：[{source_hash, horizon}] 排序去重（顺序无关——
    同一组引用 = 同一指纹）。"""
    refs = []
    for r in factor_refs or []:
        if not isinstance(r, dict):
            raise ValueError(f"factor_ref 应为 dict，得到 {type(r).__name__}")
        refs.append({"source_hash": str(r.get("source_hash") or ""),
                     "horizon": int(r["horizon"]) if r.get("horizon") is not None
                     else None})
    return sorted(refs, key=lambda x: (x["source_hash"], x["horizon"]))


def strategy_fingerprint(factor_refs, source: str, params: dict,
                         wf_config: dict, cost_model_version: str) -> dict:
    """五元组指纹（全字段保留可审计 + 组合键用于账本 dedup）。"""
    refs = normalize_factor_refs(factor_refs)
    fp = {
        "factor_refs": refs,
        "source_hash": _sha(source),
        "params_hash": _sha(params or {}),
        "wf_config_hash": _sha(wf_config or {}),
        "cost_model_version": str(cost_model_version),
    }
    fp["key"] = fingerprint_key(fp)
    return fp


def fingerprint_key(fp: dict) -> str:
    """组合键（稳定短键；账本/registry/缓存按此 dedup）。"""
    return _sha([fp["factor_refs"], fp["source_hash"], fp["params_hash"],
                 fp["wf_config_hash"], fp["cost_model_version"]])[:16]
