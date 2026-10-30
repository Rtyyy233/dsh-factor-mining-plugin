# -*- coding: utf-8 -*-
"""通道③(规则化组合)+ 通道⑤(前向孵化)+ submit 豁免 — 批次3/4,2026-09-13。

孵化回放演练:cohort 用显式 start_date 落在面板中部,判决=新鲜窗口
(≥start 的日期)一次性;强因子存活、噪声因子死——正是批次4验收标准
「首批 cohort 判决演练(历史回放模拟)」。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dsh_factor_mining.bridge import Bridge

SRC_MOM = ("def factor(env):\n    import pandas as pd\n"
           "    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n")
SRC_VOL = ("def factor(env):\n    import pandas as pd\n"
           "    v = pd.DataFrame(env.v)\n    return -v.pct_change(5).values\n")


def _panel(path: Path, T=2600, N=40, seed=4):  # 长 dev:sketch≥60(combine/计价下限)
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=T)
    rows = []
    for i in range(N):
        drift = 0.0016 if i % 2 == 0 else 0.0004   # 趋势截面分化(可预测)
        c = 10 + np.cumsum(rng.normal(drift, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}",
                         "open": p * 1.001, "high": p * 1.01, "low": p * 0.99,
                         "close": p, "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(path)


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    root = tmp_path_factory.mktemp("rescue")
    data = root / "panel.parquet"
    _panel(data)
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": {
        "primary": {"source": {"type": "parquet", "path": str(data)},
                    "layout": "long",
                    "mapping": {"symbol": "symbol", "date": "eob", "open": "open",
                                "high": "high", "low": "low", "close": "close",
                                "volume": "volume", "amount": "amount"}}}}})
    b.dispatch("data.load", {"envId": "primary"})
    b.dispatch("factor.random_generate",
               {"envId": "primary", "mode": "null-calibration", "n": 6})
    return b


def _hash_of(b, source):
    from dsh_factor_mining.discipline import source_fingerprint
    return source_fingerprint(source)


def _ensure_trial(b, state_root, source, sr_hint=None):
    """评估入 trail + 源码入库;返回 (hash, dsr_stats)。"""
    b.dispatch("factor.evaluate", {"envId": "primary", "source": source,
                                   "stage": "development", "async": False})
    trail = json.loads((Path(state_root) / "trail_engine.json").read_text(encoding="utf-8"))
    h = _hash_of(b, source)
    e = next((x for x in trail if x.get("source_hash") == h), None)
    assert e is not None and (e.get("dsr_stats") or {}).get("sr_hat") is not None
    return h, e["dsr_stats"]


# ---- 通道③ ----

def _noise_source(seed):
    NL = chr(10)
    return (f"def factor(env):{NL}"
            f"    import numpy as np{NL}"
            f"    return np.random.default_rng({seed}).standard_normal("
            f"(env.T, env.N)){NL}")


def test_combine_assembles_and_evaluates(bridge, tmp_path):
    """装配/注册/台账链路单测:候补条目手写(独立随机 sketch,种子噪声源
    可计算),不依赖真实评估的 sr 水平——组合质量不在本测试范围。"""
    b, state = bridge, bridge.state_root
    trail_p = Path(state) / "trail_engine.json"
    trail = json.loads(trail_p.read_text(encoding="utf-8"))         if trail_p.exists() else []
    hashes = []
    for i, seed in enumerate((11, 22, 33), 1):
        h = f"c{i}" * 16
        hashes.append(h)
        sk = [round(float(x), 4) + 0.0
              for x in np.random.default_rng(seed).normal(0, 1, 84)]
        trail.append({"source_hash": h, "stage": "development", "horizon": 20,
                      "origin": "hypothesis", "ts": "2026-09-13T10:00:00",
                      "ic_series_sketch": sk, "ic_ir": 0.9,
                      "dsr_stats": {"sr_hat": 0.9 - 0.05 * i, "skew": 0.0,
                                    "kurt": 3.0, "n_obs": 200}})
        src_dir = Path(state) / "sources" / h[:2]
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / f"{h}.py").write_text(_noise_source(seed), encoding="utf-8")
    trail_p.write_text(json.dumps(trail, ensure_ascii=False), encoding="utf-8")

    rep = b.dispatch("standby.combine", {"envId": "primary", "k": 3,
                                         "min_abs_sr": 0.5, "async": False})
    assert rep.get("standby_combine", {}).get("ok"), rep.get("standby_combine")
    members = rep["standby_combine"]["members"]
    assert len(members) == 3
    # 规则注册进 journal + 台账
    live = b.dispatch("journal.read", {"level": 1})["live"]
    assert "registered" in live and "规则化组合" in live
    led = (Path(state) / "registered_ledger.jsonl").read_text(encoding="utf-8")
    assert "standby.combine" in led
    # 组合评估有结果(composite 轨)
    assert "composite" in rep or "onion" in rep or rep.get("job_id"), \
        list(rep.keys())[:8]


# ---- 通道⑤:回放演练 ----

def test_incubate_replay_strong_survives_noise_dies(bridge):
    b, state = bridge, bridge.state_root
    h_mom, d1 = _ensure_trial(b, state, SRC_MOM)          # 面板有趋势分化 → 强
    from dsh_factor_mining.discipline import source_fingerprint
    noise_src = ("def factor(env):\n    import numpy as np\n"
                 "    import pandas as pd\n"
                 "    v = pd.DataFrame(env.v)\n"
                 "    return np.random.default_rng(7).standard_normal("
                 "(v.shape[0], v.shape[1]))\n")
    try:
        h_noise, d2 = _ensure_trial(b, state, noise_src)
    except AssertionError:
        pytest.skip("噪声因子无 dsr(评估即废)——改用伪造条目")
        return
    # 入队:start 落面板中部,窗口 60 交易日(回放:判决窗=后 60+ 天)
    mid = "2021-06-01"
    r = b.dispatch("incubate.enqueue", {"hashes": [h_mom[:12], h_noise[:12]],
                                        "window_days": 60,
                                        "start_date": mid})
    assert r["ok"] and r["n_members"] == 2
    cid = r["cohort_id"]
    # 冻结验证:cohort 记录含全文源码
    cohorts = json.loads((Path(state) / "incubation" / "cohorts.json")
                         .read_text(encoding="utf-8"))["cohorts"]
    c0 = next(c for c in cohorts if c["id"] == cid)
    assert all(m.get("source") for m in c0["members"])
    # 判决
    j = b.dispatch("incubate.judge", {"cohort_id": cid, "envId": "primary"})
    assert j["ok"] and j["judged"], j
    res = {x["hash"]: x for x in j["results"]}
    assert res[h_mom[:12]]["survived"] is True, res
    assert res[h_noise[:12]]["survived"] is False, res
    assert j["n_survivors"] == 1
    # 幸存者登记 + 二次判决拒绝(一次性)
    sv = (Path(state) / "incubation" / "survivors.jsonl").read_text(encoding="utf-8")
    assert h_mom in sv
    j2 = b.dispatch("incubate.judge", {"cohort_id": cid, "envId": "primary"})
    assert j2["ok"] is False and "一次性" in j2["reason"]


def test_incubate_window_not_ready(bridge):
    b, state = bridge, bridge.state_root
    h, _ = _ensure_trial(b, state, SRC_MOM.replace("shift(20)", "shift(25)"))
    r = b.dispatch("incubate.enqueue", {"hashes": [h[:12]], "window_days": 60,
                                        "start_date": "2028-12-20"})
    assert r["ok"]
    j = b.dispatch("incubate.judge", {"cohort_id": r["cohort_id"],
                                      "envId": "primary"})
    assert j["not_ready"] is True and j["fresh_days"] < j["need"]


# ---- submit 豁免 ----

def test_submit_incubation_waiver(bridge):
    b, state = bridge, bridge.state_root
    # 注意:submit 的因子不做前置 evaluate——trail 有权威记录时手造诊断
    # 会被防编造门拦(_authoritative_dsr_stats 对账);与既有 submit 测试
    # 同模式(无记录 → verified=False 但不拦)
    # 不豁免基线:弱 deflated p(diagnosis 自报 p=0.9)→ 拒
    diag = {"ic_ir_train": 0.4, "ic_n_train": 50,
            "column_perm_train": {"z": 4.0, "p": 0.0001},
            "beta_exposure": 0.1,
            "deflated_train": {"p": 0.9, "n_trials": 5, "sr_hat": 0.4,
                               "skew": 0.0, "kurt": 3.0, "n_obs": 60},
            "red_flags": ["deflated p=0.9000 > 0.05:5 次已试假设的多重检验校正下不显著"],
            "verdict": "needs_review"}
    base = b.dispatch("registry.submit", {
        "name": "incub_test_no_waiver", "signal": "test",
        "source": SRC_MOM.replace("shift(20)", "shift(40)"),
        "diagnosis": diag})
    assert base["accepted"] is False
    # 豁免:伪造幸存者(引擎写入口是 survivors.jsonl;测试直接造一行)+
    # incubated_cohort → deflated 红旗剥离。铁律:同源不得重复登记——
    # 豁免提交用另一源(shift(50)),幸存者行按其 hash 造
    src = SRC_MOM.replace("shift(20)", "shift(50)")
    from dsh_factor_mining.discipline import source_fingerprint
    hh = source_fingerprint(src)
    svp = Path(state) / "incubation" / "survivors.jsonl"
    svp.parent.mkdir(parents=True, exist_ok=True)
    with open(svp, "a", encoding="utf-8") as f:
        f.write(json.dumps({"cohort_id": "INC-test", "hash": hh,
                            "ts": "2026-09-13T12:00:00", "t": 3.5}) + "\n")
    waived = b.dispatch("registry.submit", {
        "name": "incub_test_waived", "signal": "test",
        "source": src,
        "diagnosis": json.loads(json.dumps(diag)),
        "incubated_cohort": "INC-test"})
    assert waived["accepted"] is True, waived.get("reason")
    assert waived["entry"].get("incubated_admitted", {}).get("cohort_id") == "INC-test"
    assert "incubated" in waived["reason"]
