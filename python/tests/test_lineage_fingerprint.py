# coding=utf-8
"""成分血缘家族识别回归（2026-08-25 CMF 事故产品化）。

事故：pw15_compD 合成因子的 IC 序列与 pw15 核心解相关（尾对齐
ρ<0.6），家族链断裂 → family_streak=1 → 3/6/9 升级从未触发，
agent 在同一族磨 10 小时。

修复：源码 MinHash 构造指纹（32 签名，行对 shingle）存入 trail 条目
construction_fp；_family_streak 双信号 OR 判定（IC ρ≥0.6 或指纹
Jaccard≥0.25）——合成共享核心的行对 shingle，指纹重叠保持链连续。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import (  # noqa: E402
    Bridge, _construction_fingerprint, _fingerprint_similarity,
)

CORE = """import numpy as np
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    amount = pd.DataFrame(env.amount)
    # 核心：价格-成交额错配持久性
    close_pos = (c - c.rolling(15).min()) / (c.rolling(15).max() - c.rolling(15).min())
    amt_rank = amount.rolling(15).mean().rank(pct=True, axis=1)
    mismatch = (close_pos - amt_rank).rolling(15).apply(lambda x: (x > 0).mean())
    return mismatch.values
"""

# 合成：核心 + 装饰（条件过滤 + 与 comp_D 混合）——IC 序列会与核心
# 解相关，但构造共享大量核心行对
COMPOSITE = """import numpy as np
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    amount = pd.DataFrame(env.amount)
    v = pd.DataFrame(env.v)
    # 核心：价格-成交额错配持久性（与 pw15 相同构造）
    close_pos = (c - c.rolling(15).min()) / (c.rolling(15).max() - c.rolling(15).min())
    amt_rank = amount.rolling(15).mean().rank(pct=True, axis=1)
    mismatch = (close_pos - amt_rank).rolling(15).apply(lambda x: (x > 0).mean())
    # 装饰：条件性 vol_stability 过滤
    vol_stab = 1 / (1 + v.rolling(15).std() / v.rolling(90).mean())
    range_ratio = (c.rolling(15).max() - c.rolling(15).min()) / c.rolling(60).std()
    conditioned = mismatch * np.where(range_ratio > 0.7, vol_stab, 1.0)
    # 合成：与 comp_D 混合
    comp_d = (amount.rolling(20).mean() * v.rolling(120).mean()).rank(pct=True, axis=1)
    return (0.7 * conditioned.rank(pct=True, axis=1)
            + 0.3 * comp_d).values
"""

# 真正换方向：完全不同的构造（动量 → 波动率不对称）
NEW_DIRECTION = """import numpy as np

def factor(env):
    h = env.h
    l = env.l
    c = env.c
    # 上行 vs 下行波动不对称
    ret = c[1:] / c[:-1] - 1
    up = np.where(ret > 0, ret, 0)
    dn = np.where(ret < 0, -ret, 0)
    asym = up.rolling(60).mean() / (dn.rolling(60).mean() + 1e-9)
    return -asym.values
"""


def test_fingerprint_core_vs_composite():
    """核心与合成共享构造 → 指纹相似度高（≥0.25 阈值）。"""
    fp_core = _construction_fingerprint(CORE)
    fp_comp = _construction_fingerprint(COMPOSITE)
    sim = _fingerprint_similarity(fp_core, fp_comp)
    assert sim >= 0.25, f"核心-合成相似度 {sim:.3f} < 0.25（血缘断裂）"


def test_fingerprint_core_vs_new_direction():
    """核心与全新方向构造不同 → 指纹相似度低（<0.25，链断）。"""
    fp_core = _construction_fingerprint(CORE)
    fp_new = _construction_fingerprint(NEW_DIRECTION)
    sim = _fingerprint_similarity(fp_core, fp_new)
    assert sim < 0.25, f"核心-新方向相似度 {sim:.3f} ≥ 0.25（误连）"


def test_fingerprint_deterministic():
    """同一源码两次计算 → 完全相同（跨进程稳定）。"""
    a = _construction_fingerprint(CORE)
    b = _construction_fingerprint(CORE)
    assert a == b


def test_family_streak_lineage_bridge(tmp_path):
    """端到端：合成因子 IC 序列与核心解相关，但血缘让链保持连续。

    构造场景：3 个核心变体（IC 高相关）→ 1 个合成（IC 解相关但
    构造共享）→ family_streak = 4（而非旧的断在 1）。"""
    rng = np.random.default_rng(7)
    # 3 个高相关 sketch（同族 IC 序列）
    base = list(rng.standard_normal(30))
    core_sk = [list(0.9 * np.array(base) + 0.1 * rng.standard_normal(30))
               for _ in range(3)]
    # 合成的 sketch：与 base 解相关（ρ ≈ 0）
    comp_sk = list(rng.standard_normal(30))
    from dsh_factor_mining.factor.evaluate import _tail_aligned_corr
    rho = _tail_aligned_corr(comp_sk, base)
    assert rho is None or abs(rho) < 0.5, "测试前提失败：合成 sketch 不该与核心相关"

    fp_core = _construction_fingerprint(CORE)
    fp_comp = _construction_fingerprint(COMPOSITE)

    trail = []
    for i, sk in enumerate(core_sk):
        trail.append({"source_hash": f"core{i}", "ic_series_sketch": sk,
                      "construction_fp": fp_core})
    trail.append({"source_hash": "comp", "ic_series_sketch": comp_sk,
                  "construction_fp": fp_comp})

    b = Bridge.__new__(Bridge)  # 不走 __init__（不需要 state_root）
    streak = b._family_streak(trail)
    # v5：血缘让合成保持链连续 → streak = 4
    assert streak == 4, f"血缘未生效：streak={streak}（期望 4）"


def test_family_streak_lineage_breaks_on_new_direction(tmp_path):
    """全新方向 → 链断（血缘不误连）。"""
    rng = np.random.default_rng(9)
    fp_core = _construction_fingerprint(CORE)
    fp_new = _construction_fingerprint(NEW_DIRECTION)
    trail = [
        {"source_hash": "c1", "ic_series_sketch": list(rng.standard_normal(30)),
         "construction_fp": fp_core},
        {"source_hash": "new", "ic_series_sketch": list(rng.standard_normal(30)),
         "construction_fp": fp_new},
    ]
    b = Bridge.__new__(Bridge)
    streak = b._family_streak(trail)
    assert streak == 1, f"新方向误连：streak={streak}（期望 1）"


def test_evaluate_stores_fingerprint(tmp_path):
    """evaluate 落盘的 trail 条目包含 construction_fp。"""
    import pandas as pd
    rng = np.random.default_rng(4)
    T, N = 700, 35
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        c = 10 * np.exp(np.cumsum(rng.normal(0.0002, 0.012, T)))
        for t in range(T):
            p = float(c[t])
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    data = tmp_path / "panel.parquet"
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"},
                    "calibration": {"dev_end": "2020-06-01",
                                    "sel_end": "2021-03-01"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    b.dispatch("factor.check_causality", {
        "envId": "primary",
        "source": "import numpy as np\n\ndef factor(env):\n"
                  "    return env.c / np.roll(env.c, 20, axis=0) - 1.0\n"})
    b.dispatch("factor.evaluate", {
        "envId": "primary", "stage": "development",
        "source": "import numpy as np\n\ndef factor(env):\n"
                  "    f = env.c / np.roll(env.c, 20, axis=0) - 1.0\n"
                  "    f[:21] = np.nan\n    return f\n"})
    import json
    te = json.loads((tmp_path / "state" / "trail_engine.json"
                     ).read_text(encoding="utf-8"))
    assert te and isinstance(te[-1].get("construction_fp"), list) \
        and len(te[-1]["construction_fp"]) == 32, \
        "trail 条目缺 construction_fp"
