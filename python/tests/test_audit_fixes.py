# coding=utf-8
"""批次2 修复回归测试（2026-08-18 全架构审计 → P0×3 + P1 数学/机制修复）。"""
from __future__ import annotations

import math
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.bridge import Bridge, BridgeError
from dsh_factor_mining.data.adapters import DataError, resolve_calibration
from dsh_factor_mining.factor.evaluate import _block_bootstrap, _cross_sectional_ic, _deflated_sharpe_p
from dsh_factor_mining.factor.audit import _ic_series_numpy

GOOD = """
import pandas as pd

def factor(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values
"""


def _panel(path: Path, T=800, N=40, seed=4, start="2019-01-02"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=T)
    rows = []
    for i in range(N):
        c = 10 + np.cumsum(rng.normal(0.001, 0.01, T))
        for t in range(T):
            p = max(float(c[t]), 0.5)
            rows.append({"eob": dates[t], "symbol": f"S{i:02d}", "open": p * 1.001,
                         "high": p * 1.01, "low": p * 0.99, "close": p,
                         "volume": float(rng.integers(100, 10000)),
                         "amount": float(rng.integers(1000, 100000))})
    pd.DataFrame(rows).to_parquet(path)


def _setup(root: Path, envs: dict | None = None):
    """单环境（primary）或双环境（etf+stock 两份数据）bridge。"""
    if envs is None:
        data = root / "panel.parquet"
        _panel(data)
        envs = {"primary": str(data)}
    env_conf = {}
    for eid, path in envs.items():
        env_conf[eid] = {
            "source": {"type": "parquet", "path": path}, "layout": "long",
            "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",
                        "low": "low", "close": "close", "volume": "volume",
                        "amount": "amount"}}
    b = Bridge(state_root=str(root / "state"), execution_mode="in_process")
    b.dispatch("config.save", {"config": {"version": 1, "environments": env_conf}})
    return b


# ---- P0-2/P0-3 calibration 数值域 ----

def test_calibration_domain_validation():
    for bad in ({"horizon": 0}, {"horizon": -5}, {"cost_bps": -1},
                {"ic_sample_every": 5, "horizon": 20},
                {"dev_end": "2023-01-01"},                     # 单边：缺 sel_end
                {"sel_end": "2024-06-01"},                     # 单边：缺 dev_end
                {"dev_end": "2024/13/45", "sel_end": "2025-01-01"},
                {"dev_end": "2025-01-01", "sel_end": "2023-01-01"}):  # 顺序颠倒
        try:
            resolve_calibration(bad)
            raise AssertionError(f"非法 calibration {bad} 应被拒绝")
        except DataError:
            pass
    # 合法：成对 + 顺序正确
    ok = resolve_calibration({"dev_end": "2023-01-01", "sel_end": "2024-06-01"})
    assert ok["dev_end"] == "2023-01-01"


def test_calibration_minute_annualization_derived():
    merged = resolve_calibration({"profile": "cn_stock_minute", "bars_per_day": 48})
    assert merged["annualization"] == 48 * 252
    # 用户显式给了 annualization → 不覆盖
    merged2 = resolve_calibration({"bars_per_day": 48, "annualization": 100})
    assert merged2["annualization"] == 100


# ---- P1-1 DSR sr0 ----

def test_dsr_sr0_matches_bailey_lopez_de_prado():
    rng = np.random.default_rng(3)
    ic = pd.Series(rng.normal(0.02, 0.1, 120))
    r1 = _deflated_sharpe_p(ic, n_trials=1)
    assert r1["sr0"] == 0.0
    # v3（2026-08-21）：n_trials 降级为纯遥测（谱 N_eff→B-LP 链条退役，
    # 三处失真见 _dsr_sr0 docstring）——门参数是 bar_sigma = E[max|X|]。
    # bar_sigma=None = 直调单检验口径：p 正常给出（无选择折减）
    r_nopool = _deflated_sharpe_p(ic, n_trials=10)
    assert r_nopool["p"] is not None and r_nopool["sr0"] == 0.0
    # bar_sigma>0 而无 pool_std → 拒绝给 p（2026-08-18 审计语义保留：
    # 折减门槛 sr0 = bar_sigma·pool_std 需池分布尺度，缺基线不给不可信数字）
    r_bs_nopool = _deflated_sharpe_p(ic, bar_sigma=2.5)
    assert r_bs_nopool["p"] is None and "pool_std" in r_bs_nopool["note"]
    # pool_std 固定时：sr0 = bar_sigma·pool_std（E[max|X|] 直算口径），
    # bar 越高门槛越高
    got_a = _deflated_sharpe_p(ic, bar_sigma=1.5, pool_std=0.15)
    got_b = _deflated_sharpe_p(ic, bar_sigma=2.5, pool_std=0.15)
    assert abs(got_a["sr0"] - 1.5 * 0.15) < 1e-12, got_a
    assert abs(got_b["sr0"] - 2.5 * 0.15) < 1e-12, got_b
    assert got_a["p"] is not None and got_b["p"] is not None
    assert got_b["p"] > got_a["p"]  # 门槛越高 deflated p 越大


# ---- P1-2 block bootstrap z ----

def test_block_bootstrap_z_alive():
    rng = np.random.default_rng(5)
    # 显著正均值序列：旧实现 z=(mu−bootstrap均值)/se 恒≈0；修复后 z=mu/se 应显著
    strong = pd.Series(rng.normal(0.5, 1.0, 300))
    r = _block_bootstrap(strong.values)
    assert r["z"] > 5 and r["p"] < 1e-6, r
    # 零均值：不显著
    noise = pd.Series(rng.normal(0.0, 1.0, 300))
    r0 = _block_bootstrap(noise.values)
    assert abs(r0["z"]) < 3, r0


# ---- P1-3 audit tie 一致性 ----

def test_audit_evaluate_spearman_agree_on_ties():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        b.dispatch("data.load", {"envId": "primary"})
        env = b.envs["primary"]
        from dsh_factor_mining.factor.evaluate import _forward_returns, _pit_mask
        # 二值因子：截面内大量 tie（True/False 各半）——旧 footrule 公式的失效场景
        # （注意用 diff：价格恒正，c/c.shift(1)>0 是常数不是信号）
        c = pd.DataFrame(env.c)
        F = (c.diff() > 0).values.astype(float)
        fwd = _forward_returns(env)
        pit = _pit_mask(env)
        ic_ev = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
        ic_au = _ic_series_numpy(F, fwd, pit, env, sig_only=True)
        common = ic_ev.index.intersection(ic_au.index)
        diff = (ic_ev[common] - ic_au[common]).abs().max()
        assert diff < 1e-10, f"audit 与 evaluate 的逐日 IC 在有 tie 时必须一致（max diff {diff}）"


# ---- P1-9 causality 缓存按环境隔离 ----

def test_causality_cache_env_scoped():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        p1, p2 = root / "a.parquet", root / "b.parquet"
        _panel(p1, seed=4)
        _panel(p2, seed=9)  # 不同数据
        b = _setup(root, envs={"etf": str(p1), "stock": str(p2)})
        b.dispatch("factor.check_causality", {"envId": "etf", "source": GOOD})
        b.dispatch("factor.check_causality", {"envId": "stock", "source": GOOD})
        assert len(b._causality_cache) == 2, (
            "同一源码在两个环境必须各有一份缓存条目——否则换数据后误命中旧前视结论")


# ---- P1-12 trail_engine 进 mining reset + P1-14 备份目录唯一 ----

def test_state_reset_covers_engine_trail_and_unique_backups():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        b = _setup(root)
        b.dispatch("factor.evaluate", {"envId": "primary", "source": GOOD, "stage": "development"})
        assert (root / "state" / "trail_engine.json").exists()
        # pool/ 目录（双池记忆）也必须在 mining scope 内
        pool_dir = root / "state" / "pool"
        pool_dir.mkdir(parents=True, exist_ok=True)
        (pool_dir / "active.json").write_text("[]", encoding="utf-8")
        (pool_dir / "fp").mkdir(parents=True, exist_ok=True)
        (pool_dir / "fp" / "abc.npy").write_bytes(b"fake")
        r1 = b.dispatch("state.reset", {"scope": "mining"})
        assert "trail_engine.json" in r1["removed"], r1
        assert "pool" in r1["removed"], r1
        assert not (root / "state" / "trail_engine.json").exists()
        assert not pool_dir.exists()
        # 目录备份完整（含 fp 子目录）
        from pathlib import Path as _P
        assert (_P(r1["backup"]) / "pool" / "active.json").exists()
        assert (_P(r1["backup"]) / "pool" / "fp" / "abc.npy").exists()
        # test_lock 永不在任何 scope
        (root / "state" / "test_lock.json").write_text("{}", encoding="utf-8")
        r2 = b.dispatch("state.reset", {"scope": "all"})
        assert (root / "state" / "test_lock.json").exists()
        # 备份目录互不相同（同秒两次 reset 不互相覆盖）
        assert r1["backup"] != r2["backup"]


# ---- P1-13 receipt 容量 ----

def test_receipts_capacity_4096():
    with tempfile.TemporaryDirectory() as d:
        b = _setup(Path(d))
        for i in range(5000):
            b._make_receipt({"ic_ir_train": float(i)})
        assert len(b._receipts) == 4096, len(b._receipts)
        # 最新 receipt 仍在（不被挤出）
        assert b._verify_receipt({"_receipt": list(b._receipts)[-1],
                                  "ic_ir_train": 4999.0}) is True


# ---- 原子写冒烟 ----

def test_atomic_write_json_smoke():
    from dsh_factor_mining.state import _atomic_write_json
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.json"
        _atomic_write_json(p, [1, 2])
        _atomic_write_json(p, [1, 2, 3])   # 覆盖写
        assert __import__("json").loads(p.read_text(encoding="utf-8")) == [1, 2, 3]
        assert not list(Path(d).glob("*.tmp")), "替换后不留 tmp 残件"


# ---- P2 random_gen 叶子过滤 ----

def test_random_gen_leaves_excludes_missing_amount():
    from dsh_factor_mining.factor.random_gen import effective_operator_set, generate_tree
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        opset = effective_operator_set(d)
        rng = np.random.default_rng(1)
        seen = set()
        for _ in range(60):
            tree = generate_tree(rng, opset, leaves=["o", "h", "l", "c", "v"])
            seen.add(str(tree))
        assert seen, "过滤叶子集后树仍可生成"


# ---- 2026-08-18 晚 DSH 实测事故修复（qwen3.7plus session.jsonl 取证） ----

def test_worker_ping_smoke():
    """worker --ping 走完整 import 链（2026-08-18 实测：stdlib backport 污染
    只在 worker 子进程暴露，挖到 evaluate 才炸）。"""
    import subprocess, sys as _sys
    r = subprocess.run([_sys.executable, "-m", "dsh_factor_mining.worker", "--ping"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-400:]
    assert '"ok": true' in r.stdout


def test_library_normalizes_str_entries():
    """python_module 库 list_known_factors 返回 list[str]（如 known_factors.py）
    时不得崩：规范化为 {"name": s}（旧代码 e.values() 对 str 直接 -32603）。"""
    from dsh_factor_mining.library.contract import UserLibrary
    assert UserLibrary._normalize_entries(["a", "b"]) == [
        {"name": "a", "description": ""}, {"name": "b", "description": ""}]
    assert UserLibrary._normalize_entries([{"name": "c", "ic_ir": 1.0}]) == [
        {"name": "c", "ic_ir": 1.0}]
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d:
        mod = Path(d) / "user_lib.py"
        mod.write_text(
            "def list_known_factors():\n    return ['rsi14', 'mom20']\n"
            "def query_known_factors(k=None):\n    return ['rsi14']\n", encoding="utf-8")
        lib = UserLibrary({"type": "python_module", "path": str(mod)})
        r = lib.query("rsi")
        assert r["configured"] is True and r["hits"], r  # 查询不再 -32603
