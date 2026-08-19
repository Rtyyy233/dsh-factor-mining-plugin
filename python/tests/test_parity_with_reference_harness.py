# coding=utf-8
"""P1 parity gate: synthetic matrices, reference harness vs dsh_factor_mining.

Run from the python/ directory:
    PYTHONPATH=src python tests/test_parity_with_reference_harness.py

The reference harness is imported read-only from its current location and is
only given synthetic data.  No real data file is touched.
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Relative worktree location of the read-only reference harness.  The parity
# test reads no data files and only imports the reference modules.
_WORKTREE_ROOT = ROOT.parents[2]  # python/ -> dsh-factor-mining-plugin/ -> projects/ -> worktree root
_env_ref = os.environ.get("DSH_FACTOR_REFERENCE_HARNESS", "")
REFERENCE_HARNESS = (
    Path(_env_ref)
    if _env_ref
    else _WORKTREE_ROOT / "modules" / "investment" / "data" / "factor-mining" / "harness"
)
# The published open-source test ships without a reference checkout and skips.

from dsh_factor_mining.factor.audit import audit as new_audit
from dsh_factor_mining.factor.causality import check_causality as new_check_causality
from dsh_factor_mining.factor.env import FactorEnv as NewFactorEnv
from dsh_factor_mining.factor.evaluate import (
    evaluate as new_evaluate,
    evaluate_batch as new_evaluate_batch,
    evaluate_composite as new_evaluate_composite,
    evaluate_walk_forward as new_evaluate_walk_forward,
)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_synthetic(T=1600, N=50, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=T)
    # one common market return + idiosyncratic return
    market = rng.normal(0.0002, 0.005, size=T)
    idio = rng.normal(0.0005, 0.012, size=(T, N))
    ret = 0.6 * market[:, None] + idio
    c = 10.0 * np.exp(np.cumsum(ret, axis=0))
    c[:2, :] = 10.0
    # small overnight gap for open
    gap = 1.0 + rng.normal(0.0, 0.002, size=(T, N))
    o = c * gap
    h = np.maximum(o, c) * (1.0 + np.abs(rng.normal(0.0, 0.004, size=(T, N))))
    l = np.minimum(o, c) * (1.0 - np.abs(rng.normal(0.0, 0.004, size=(T, N))))
    v = rng.lognormal(mean=12.0, sigma=0.4, size=(T, N))
    amount = v * c
    listed = np.ones((T, N), dtype=bool)
    symbols = [f"S{i:03d}" for i in range(N)]
    return dates, o, h, l, c, v, amount, listed, symbols


def _factor_20d(env):
    c = pd.DataFrame(env.c)
    return (c / c.shift(20) - 1.0).values


def _factor_future(env):
    c = pd.DataFrame(env.c)
    return (c.shift(-1) / c - 1.0).values


def _isclose(a, b, tol=1e-9):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float, np.integer, np.floating)):
        a = float(a)
    if isinstance(b, (int, float, np.integer, np.floating)):
        b = float(b)
    if isinstance(a, str) or isinstance(b, str):
        return a == b
    if not math.isfinite(a) and not math.isfinite(b):
        return True
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=tol)


def _compare_scalars(name, a, b, failures):
    if not _isclose(a, b):
        failures.append(f"{name}: new={a} ref={b}")


def _load_reference():
    sys.path.insert(0, str(REFERENCE_HARNESS))
    ref_env = _load_module("ref_factor_env", REFERENCE_HARNESS / "factor_env.py")
    ref_eval = _load_module("ref_evaluate", REFERENCE_HARNESS / "evaluate.py")
    ref_audit = _load_module("ref_audit", REFERENCE_HARNESS / "audit.py")
    return ref_env, ref_eval, ref_audit


def main():
    if not REFERENCE_HARNESS.exists():
        print("REFERENCE_HARNESS_NOT_FOUND — skip parity test")
        return 0

    ref_env, ref_eval, ref_audit = _load_reference()
    dates, o, h, l, c, v, amount, listed, symbols = _make_synthetic()
    from dsh_factor_mining.factor.env import Calibration
    # parity 对照参考 harness 的硬编码口径（2021/2024 分界 + H20/10bps）
    cal = Calibration(dev_end="2021-01-01", sel_end="2024-01-01")
    new_env = NewFactorEnv(o, h, l, c, v, dates, symbols, listed=listed,
                           amount=amount, calibration=cal)
    ref_env_obj = ref_env.FactorEnv(o, h, l, c, v, dates, symbols, listed=listed, amount=amount)

    failures = []
    checks = 0

    # 1. causality: same synthetic data, same factor functions
    for name, fn in [("good", _factor_20d), ("bad", _factor_future)]:
        a = new_check_causality(fn, new_env)
        b = ref_env.check_causality(fn, ref_env_obj)
        checks += 1
        if a["verdict"] != b["verdict"]:
            failures.append(f"causality({name}): new={a} ref={b}")

    # 2. evaluate core diagnostics
    F = _factor_20d(new_env)
    a = new_evaluate(F, new_env)
    b = ref_eval.evaluate(F, ref_env_obj)
    for key in ("ic_mean_train", "ic_ir_train", "ic_n_train", "beta_exposure"):
        checks += 1
        _compare_scalars(f"evaluate.{key}", a.get(key), b.get(key), failures)
    for sub in ("column_perm_train.z", "column_perm_train.p"):
        aa = a["column_perm_train"]["z"] if sub.endswith("z") else a["column_perm_train"]["p"]
        bb = b["column_perm_train"]["z"] if sub.endswith("z") else b["column_perm_train"]["p"]
        checks += 1
        _compare_scalars(f"evaluate.{sub}", aa, bb, failures)
    for key in ("n_total", "K", "n_per_interval", "interval_slope", "first_last_delta",
                "delta_z", "se_delta", "monotonic_rho", "yearly_slope"):
        checks += 1
        _compare_scalars(f"evaluate.decay_train.{key}", a["decay_train"].get(key), b["decay_train"].get(key), failures)
    checks += 1
    if a["decay_train"]["decay_direction"] != b["decay_train"]["decay_direction"]:
        failures.append("evaluate.decay_train.decay_direction mismatch")
    for key in ("net_annual", "gross_annual", "turn_avg"):
        aa = a["topn"][key] if a.get("topn") else None
        bb = b["topn"][key] if b.get("topn") else None
        checks += 1
        _compare_scalars(f"evaluate.topn.{key}", aa, bb, failures)

    # 3. composite
    part1 = _factor_20d(new_env)
    part2 = np.log1p(np.abs(F))
    comp = 0.5 * part1 + 0.5 * pd.DataFrame(part2).rank(pct=True).values
    a = new_evaluate_composite(comp, {"mom": part1, "abs": part2}, new_env)
    b = ref_eval.evaluate_composite(comp, {"mom": part1, "abs": part2}, ref_env_obj)
    for key in ("best_part",):
        checks += 1
        if a["diagnosis"][key] != b["diagnosis"][key]:
            failures.append(f"composite.{key}: new={a['diagnosis'][key]} ref={b['diagnosis'][key]}")
    checks += 1
    _compare_scalars("composite.synthesis_gain", a["onion"]["synthesis_gain"], b["onion"]["synthesis_gain"], failures)
    checks += 1
    _compare_scalars("composite.corr_vs_best", a["diagnosis"]["corr_vs_best"], b["diagnosis"]["corr_vs_best"], failures)

    # 4. batch deflate
    a = new_evaluate_batch({"mom": part1, "abs": part2}, new_env)
    b = ref_eval.evaluate_batch({"mom": part1, "abs": part2}, ref_env_obj)
    for key in ("M", "rho_bar", "N_eff", "best_name"):
        checks += 1
        _compare_scalars(f"batch.{key}", a["batch"].get(key), b["batch"].get(key), failures)
    checks += 1
    _compare_scalars("batch.best_deflated_p", a["batch"].get("best_deflated_p"),
                     b["batch"].get("best_deflated_p"), failures)

    # 5. walk-forward
    a = new_evaluate_walk_forward(F, new_env, n_folds=5, t0_date="2021-01-01")
    b = ref_eval.evaluate_walk_forward(F, ref_env_obj, n_folds=5, t0_date="2021-01-01")
    if "error" in a or "error" in b:
        checks += 1
        if ("error" in a) != ("error" in b):
            failures.append(f"walk_forward error mismatch: {a} vs {b}")
    else:
        checks += 1
        _compare_scalars("walk_forward.fold_consistency", a["fold_consistency"], b["fold_consistency"], failures)
        checks += 1
        _compare_scalars("walk_forward.overall_ic_mean", a["overall_ic_mean"], b["overall_ic_mean"], failures)

    # 6. audit verdict + independent numbers
    a = new_audit(_factor_20d, new_env)
    b = ref_audit.audit(_factor_20d, ref_env_obj)
    checks += 1
    if a["verdict"] != b["verdict"]:
        failures.append(f"audit.verdict: new={a} ref={b}")
    for key in ("audit_ic_mean_train", "audit_topn_net", "audit_colperm_z", "audit_beta"):
        checks += 1
        _compare_scalars(f"audit.{key}", a.get(key), b.get(key), failures)

    if failures:
        print("PARITY FAIL")
        for f in failures:
            print("  -", f)
        return 1, failures
    print(f"PARITY PASS: {checks} checks across causality/evaluate/composite/batch/walk-forward/audit")
    return 0, []


def _collect_parity():
    """pytest 入口：与原 harness 的对账（参考位置存在才跑，否则 skip）。"""
    rc, failures = main()
    if rc == 0:
        return
    raise AssertionError(f"PARITY FAIL: {failures}")


def test_parity_with_reference():
    pytest.importorskip("dsh_factor_mining")
    _collect_parity()


if __name__ == "__main__":
    sys.exit(main()[0])
