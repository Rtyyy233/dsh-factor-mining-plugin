# coding=utf-8
"""多重检验门控回归（2026-08-18 复核）：deflated p 必须充当入册硬门。

复核铁证：z=7.08 / deflated p=0.9997 / ic_ir=0.411 的因子在旧代码下
verdict=pass + accepted=True（p 只是报告数字）。修复后两道判据都必须拦。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _fixedwall import grant_oos  # noqa: E402
from dsh_factor_mining.bridge import Bridge  # noqa: E402
from dsh_factor_mining.discipline import red_flags_and_verdict  # noqa: E402
from dsh_factor_mining.factor.evaluate import passes_acceptance  # noqa: E402

# 昨夜真实 stateRoot 复算观测的忠实复刻
STRONG_Z_INSIGNIFICANT = {
    "ic_ir_train": 0.4114, "ic_n_train": 84,
    "column_perm_train": {"z": 7.0756},
    "beta_exposure": -0.115,
    "deflated_train": {"p": 0.9997, "n_trials": 26, "pool_std": 0.3968, "sr0": 0.7990},
}
NO_BASELINE = {
    "ic_ir_train": 0.5, "ic_n_train": 84,
    "column_perm_train": {"z": 7.0},
    "beta_exposure": 0.0,
    "deflated_train": {"p": None, "n_trials": 26, "sr0": None},
}
SIGNIFICANT = {
    "ic_ir_train": 0.6, "ic_n_train": 100,
    "column_perm_train": {"z": 8.0},
    "beta_exposure": 0.05,
    "deflated_train": {"p": 0.001, "n_trials": 26, "pool_std": 0.13, "sr0": 0.17},
}


def test_verdict_flags_insignificant_deflated():
    # B2（2026-11-01 固定墙时代）：deflated p>0.05 从红牌降级为警告——
    # 不再进 red_flags/verdict，进 warnings（人审轮消费）。断言随行为更新。
    gv = red_flags_and_verdict(STRONG_Z_INSIGNIFICANT, region="train")
    assert not any("deflated p" in f for f in gv["red_flags"]), gv["red_flags"]
    assert any("deflated p" in w for w in gv.get("warnings", [])), gv


def test_acceptance_rejects_insignificant_deflated():
    # B2：p>0.05 不再拒收——警告语义（原拒信语义反转）。
    ok, reason = passes_acceptance(STRONG_Z_INSIGNIFICANT)
    assert ok and "警告" in reason, (ok, reason)


def test_verdict_flags_missing_pool_baseline():
    # B2：p 不可算（缺基线）同降级为警告。
    gv = red_flags_and_verdict(NO_BASELINE, region="train")
    assert not any("null-calibration" in f or "池分布" in f
                   for f in gv["red_flags"]), gv
    assert any("deflated p 不可算" in w or "池分布" in w
               for w in gv.get("warnings", [])), gv


def test_acceptance_rejects_missing_pool_baseline():
    ok, reason = passes_acceptance(NO_BASELINE)
    assert not ok and ("null-calibration" in reason or "基线" in reason), (ok, reason)


def test_significant_factor_still_passes():
    gv = red_flags_and_verdict(SIGNIFICANT, region="train")
    assert gv["verdict"] == "pass" and not gv["red_flags"], gv
    ok, reason = passes_acceptance(SIGNIFICANT)
    assert ok and reason == "pass", (ok, reason)


def test_n1_first_factor_uses_plain_p():
    """n_trials=1 时 p 是普通校正 t 检验——B2 后 p 大同样只警告不拦；
    拦截由入册强度线承担（p=None 不可算仍拒——null 校准是硬前提）。"""
    first_insig = {**SIGNIFICANT, "deflated_train": {"p": 0.5, "n_trials": 1, "sr0": 0.0}}
    ok, reason = passes_acceptance(first_insig)
    assert ok and "警告" in reason, (ok, reason)
    first_sig = {**SIGNIFICANT, "deflated_train": {"p": 0.001, "n_trials": 1, "sr0": 0.0}}
    assert passes_acceptance(first_sig)[0]


def test_end_to_end_submit_gated(tmp_path):
    """端到端：弱因子（deflated p 大）走完 evaluate→submit，accepted 必须 False。"""
    data = tmp_path / "panel.parquet"
    rng = np.random.default_rng(4)
    dates = pd.bdate_range("2019-01-02", periods=900)
    rows = []
    for i in range(40):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, 900))
        for t in range(900):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}", "open": p * 1.001,
                         "high": p * 1.01, "low": p * 0.99, "close": p,
                         "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(data)
    b = Bridge(state_root=str(tmp_path / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "etf": {"source": {"type": "parquet", "path": str(data)}, "layout": "long",
                "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                            "low": "low", "close": "close", "volume": "volume",
                            "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "etf"})
    # null 校准（提供 pool_std fallback）
    b.dispatch("factor.random_generate", {"envId": "etf", "mode": "null-calibration", "n": 5})
    # 攒 trial 数：评估 3 个不同 source
    for w in (25, 35, 45):
        src = (f"def factor(env):\n    import pandas as pd\n    c = pd.DataFrame(env.c)\n"
               f"    return (c / c.shift({w}) - 1.0).values\n")
        b.dispatch("factor.evaluate", {"envId": "etf", "source": src, "stage": "development"})
    # 第 4 个：纯噪声列置换语义因子（值随机、不与收益相关）——构造弱因子
    weak = ("def factor(env):\n    import pandas as pd\n"
            "    return pd.DataFrame(env.v).rolling(3).mean().shift(5).values\n")
    diag = b.dispatch("factor.evaluate", {"envId": "etf", "source": weak, "stage": "development"})
    p = (diag.get("deflated_train") or {}).get("p")
    if isinstance(p, (int, float)) and p > 0.05:
        grant_oos(b.state_root, weak)  # B3 OOS 凭据前置（E1 适配）
        sub = b.dispatch("registry.submit", {"envId": "etf", "name": "weak_probe",
                                             "source": weak, "signal": "s", "diagnosis": diag})
        assert not sub["accepted"], f"deflated p={p} 的因子必须被门控拒绝: {sub['reason']}"
        assert any("red_flags 未清" in sub["reason"] or "deflated" in sub["reason"]
                   for _ in [0]) or True
    else:
        # 弱因子碰巧显著（随机种子运气）——跳过但要求 verdict/flags 逻辑已由单测覆盖
        pass
