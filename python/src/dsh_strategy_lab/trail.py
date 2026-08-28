# coding=utf-8
"""策略账本（单一事实源；派生不落第二份）。

- ``strategy_trail.json``：每次评估一条，键 = (指纹, stage)——重评同键
  = 更新不新增（同指纹展开不放大试验基数）。
- **计试验**：development / walk_forward 两个 stage（walk-forward 为
  策略层必经 stage——regime 依赖是策略第一死因）。
- **不计试验**（一次假设的探针）：placebo 抽样、随机游走面板、启动点
  扰动、平缓性探针、截断审计——同因子层噪声世界不计试验。
- **test 区一次性消费**，永不进 deflation（尾线 WS3 同款纪律；
  stage="test" 的条目被 strategy_ledger 排除在计价之外，test_lock
  另行 fail-closed）。
"""
from __future__ import annotations

import time

from .fingerprint import fingerprint_key
from .state import (atomic_write_json, path_for, read_json_list)

#: 计试验的 stage（deflation 的 N_eff 基数）；test 不在其中
COUNTED_STAGES = {"development", "walk_forward"}
ALL_STAGES = COUNTED_STAGES | {"test"}


def append_trail(entry: dict, root=None) -> dict:
    """评估自动记账（计数不依赖自觉）。

    entry 需含 stage 与 fingerprint（含 key）；键 = (key, stage)：
    重评同键 = 更新不新增（保留首记 ts，更新 last_ts 与指标）。
    写锁内读-合并-写（并行 P0）：并发 CLI 下防丢条目/丢合并。"""
    from dsh_factor_mining.filelock import state_write_lock

    stage = entry.get("stage")
    if stage not in ALL_STAGES:
        raise ValueError(
            f"未知 stage {stage!r}——合法值 {sorted(ALL_STAGES)}；"
            "placebo/噪声面板/启动点扰动/平缓性/截断审计是探针，"
            "不记 trail（不计试验，见模块 docstring）")
    fp = entry.get("fingerprint")
    if not isinstance(fp, dict) or "key" not in fp:
        raise ValueError("trail 条目缺 fingerprint.key（五元组指纹）")
    p = path_for("trail", root)
    with state_write_lock(p.parent):
        entries = read_json_list(p)
        key = (fp["key"], stage)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        for i, e in enumerate(entries):
            if (isinstance(e, dict)
                    and (e.get("fingerprint") or {}).get("key") == key[0]
                    and e.get("stage") == stage):
                merged = {**e, **entry, "ts": e.get("ts", now),
                          "last_ts": now}
                entries[i] = merged
                atomic_write_json(p, entries)
                return merged
        entry = {**entry, "ts": now, "last_ts": now}
        entries.append(entry)
        atomic_write_json(p, entries)
        return entry


def strategy_ledger(root=None) -> list[dict]:
    """计价账本（派生视图，不落第二份文件）：只含计试验 stage。"""
    return [e for e in read_json_list(path_for("trail", root))
            if isinstance(e, dict) and e.get("stage") in COUNTED_STAGES]


def n_trials(root=None) -> int:
    """已计试验数（N_eff 的分母上界；聚类在 gates.trade_minhash）。"""
    return len(strategy_ledger(root))
