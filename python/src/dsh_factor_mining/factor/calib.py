# coding=utf-8
"""机器校准基准（效率四层 Tier 2，2026-08-27 规划 D3b）。

一次性参考负载 → 本机 ref_cpu_s（合成面板过固定向量化管线的 CPU 秒，
中位数），缓存 stateRoot/machine-calib.json。用途：
- registry 的相对成本 cpu_rel = cpu_s / ref_cpu_s（跨机可比）
- 绝对 CPU 预算（噪声门外推等）不依赖校准——预算是绝对 CPU 秒

失效条件：引擎版本变更 / cpu_count 变更 / 文件缺失 → 自动重测。
"""
from __future__ import annotations

import json
import os
import statistics
import time
from pathlib import Path

CALIB_FILENAME = "machine-calib.json"

# 参考负载规模：与真实面板同数量级偏小（校准要秒级完成）；固定种子可复现
_REF_T, _REF_N, _REF_SEED = 600, 200, 20260827


def _reference_workload() -> None:
    """固定向量化管线（ops 算子组合）——与文档推荐写法同构。
    ops 契约：ts_*/cs_* 吃 DataFrame（rolling/rank），输入需包装。"""
    import numpy as np
    import pandas as pd

    from .ops import cs_rank, ts_corr, ts_mean, ts_rank

    rng = np.random.default_rng(_REF_SEED)
    x = pd.DataFrame(rng.normal(0, 0.01, size=(_REF_T, _REF_N)).cumsum(axis=0))
    y = pd.DataFrame(rng.normal(0, 0.01, size=(_REF_T, _REF_N)).cumsum(axis=0))
    _ = ts_rank(cs_rank(ts_mean(x, 20)), 60)
    _ = ts_corr(x, y, 20)


def ref_cpu_seconds(state_root, force: bool = False) -> float | None:
    """本机参考 CPU 秒（缓存 stateRoot/machine-calib.json）。

    返回 None 的唯一路径：负载自身异常（不该发生——调用方容忍 null
    并附「无校准」注记）。测试可用 monkeypatch _reference_workload 加速。"""
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    p = root / CALIB_FILENAME
    from .. import __version__ as engine_version

    cpu_count = os.cpu_count() or 1
    if not force and p.exists():
        try:
            cached = json.loads(p.read_text(encoding="utf-8"))
            if (isinstance(cached, dict)
                    and cached.get("engine_version") == engine_version
                    and int(cached.get("cpu_count", -1)) == cpu_count
                    and isinstance(cached.get("ref_cpu_s"), (int, float))
                    and cached["ref_cpu_s"] > 0):
                return float(cached["ref_cpu_s"])
        except Exception:
            pass  # 损坏 → 重测
    samples = []
    for _ in range(3):
        t0 = time.process_time()
        _reference_workload()
        samples.append(time.process_time() - t0)
    ref = round(float(statistics.median(samples)), 4)
    payload = {"engine_version": engine_version, "cpu_count": cpu_count,
               "ref_cpu_s": ref, "T": _REF_T, "N": _REF_N, "seed": _REF_SEED,
               "samples": [round(s, 4) for s in samples],
               "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(p))
    return ref


def read_calib(state_root) -> dict | None:
    """只读校准（不在场/损坏 → None；供 status 展示）。"""
    p = Path(state_root) / CALIB_FILENAME
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
