# coding=utf-8
"""随机搜索空间扩充测试（2026-09-18）— 新算子数学、空间类型规则、
结构模板、健康门、算子集指纹敏感度、动量除零保护。

对应实施：RESEARCH-random-search-space-expansion-2026-09-18 的
P0（缝1指纹/缝2病态）+ P1（空间类型+条件结构+模板）+ P2（算子第一批）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dsh_factor_mining.factor import ops as ops_mod
from dsh_factor_mining.factor.random_gen import (
    DEFAULT_TEMPLATE_SHARE, TEMPLATES, _space, effective_operator_set,
    env_leaves, generate_one, generate_tree, matrix_health,
    opset_fingerprint, render_factor_source, run_null_calibration,
    write_operator_override,
)
from dsh_factor_mining.factor.env import FactorEnv


def _synthetic_env(T=400, N=30, seed=4):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2019-01-02", periods=T)
    c = 10 * np.exp(np.cumsum(rng.normal(0.0005, 0.012, (T, N)), axis=0))
    o = c * (1 + rng.normal(0, 0.002, (T, N)))
    h = np.maximum(o, c) * 1.001
    l = np.minimum(o, c) * 0.999
    v = rng.lognormal(12, 0.4, (T, N))
    amount = v * c
    return FactorEnv(o, h, l, c, v, dates, [f"S{i:02d}" for i in range(N)],
                     listed=np.ones((T, N), bool), amount=amount)


def _cats(node, acc):
    if node.category != "leaf":
        acc.add(node.category)
    for ch in node.children:
        _cats(ch, acc)
    return acc


def _check_spaces(node):
    """空间类型规则 R1/R2 递归校验。"""
    if node.category == "cs":
        assert _space(node.children[0]) == "raw", \
            f"R1 违例（cs 输入非 raw）: {node.to_expression()}"
    if node.category == "binary" and node.op in ops_mod.SAME_SPACE_BINARY:
        assert _space(node.children[0]) == _space(node.children[1]), \
            f"R2 违例（同空间二元混空间）: {node.to_expression()}"
    if node.category == "cmp":
        assert _space(node.children[0]) == _space(node.children[1]), \
            f"R2 违例（比较混空间）: {node.to_expression()}"
    if node.category == "cond":
        assert _space(node.children[0]) == "bool", \
            f"cond 门非布尔: {node.to_expression()}"
        assert _space(node.children[1]) == _space(node.children[2]), \
            f"R2 违例（条件分支混空间）: {node.to_expression()}"
    for ch in node.children:
        _check_spaces(ch)


# ---------------------------------------------------------------------------
# 新算子数学
# ---------------------------------------------------------------------------
def test_ts_resid_beta():
    rng = np.random.default_rng(5)
    T, N = 300, 6
    x = pd.DataFrame(rng.normal(0, 0.02, (T, N)))
    noise = pd.DataFrame(rng.normal(0, 0.01, (T, N)))
    y = x.mul(2.0).add(noise)
    b = ops_mod.ts_beta(y, x, 120)
    r = ops_mod.ts_resid(y, x, 120)
    # beta_hat 抽样误差 ~ σe/(σx·√w) ≈ 0.046；900 个估计的极值可达 ~4σ，
    # atol 放到 0.2（残差才是算子语义主体，其精度单独用 0.02 卡）
    assert np.allclose(b.iloc[-150:].values, 2.0, atol=0.2)
    assert np.allclose(r.iloc[-150:].values, noise.iloc[-150:].values, atol=0.02)


def test_ts_decay_exp():
    x = pd.DataFrame(np.arange(1, 11, dtype=float).reshape(-1, 1))
    out = ops_mod.ts_decay_exp(x, 10)
    w = np.power(0.5, np.arange(9, -1, -1))   # 最新样本权重 1（与算子同向）
    w /= w.sum()
    expected = float(np.dot(x.values.ravel(), w))
    assert abs(out.iloc[-1, 0] - expected) < 1e-12
    # 近端权重高：递增序列的衰减均值应高于简单均值
    assert out.iloc[-1, 0] > float(x.values.mean())


def test_hump():
    x = pd.DataFrame(np.array([[100.0], [100.4], [101.5], [101.6]]))
    out = ops_mod.hump(x, 0.01)
    # 相对变化：0.4%(<1% 保持前值) | 1.1%(≥1% 放行) | 0.099%(<1% 保持前值)
    assert out.iloc[1, 0] == 100.0
    assert out.iloc[2, 0] == 101.5
    assert abs(out.iloc[3, 0] - 101.5) < 1e-12


def test_where_gt_lt():
    a = pd.DataFrame(np.array([[1.0], [2.0]]))
    b = pd.DataFrame(np.array([[10.0], [20.0]]))
    c = ops_mod.gt(a, pd.DataFrame(np.array([[0.5], [3.0]])))
    w = ops_mod.where(c, a, b)
    assert c.iloc[0, 0] == 1.0 and c.iloc[1, 0] == 0.0
    assert w.iloc[0, 0] == 1.0 and w.iloc[1, 0] == 20.0


def test_ts_momentum_zero_guard():
    """缝2a：分母为零/贴零列不产 inf（Day4 停牌列事故的算子级修复）。"""
    x = pd.DataFrame(np.array([[10.0], [0.0], [0.0], [5.0]]))
    out = ops_mod.ts_momentum(x, 2)
    assert np.isfinite(out.values[~np.isnan(out.values)]).all()


# ---------------------------------------------------------------------------
# 空间类型规则（性质测试）
# ---------------------------------------------------------------------------
def test_space_rules_property():
    opset = effective_operator_set()
    rng = np.random.default_rng(11)
    n_cond = n_cmp = 0
    for _ in range(300):
        tree = generate_tree(rng, opset)
        _check_spaces(tree)
        cats = _cats(tree, set())
        assert "ts" in cats and "cs" in cats
        n_cond += "cond" in cats
        n_cmp += "cmp" in cats
    # 条件结构确实进入采样空间（300 棵中至少各出现一次）
    assert n_cond > 0 and n_cmp > 0, f"条件结构未进入采样: cond={n_cond} cmp={n_cmp}"


# ---------------------------------------------------------------------------
# 结构模板
# ---------------------------------------------------------------------------
def test_templates_structure_and_render():
    env = _synthetic_env()
    opset = effective_operator_set()
    rng = np.random.default_rng(3)
    for tpl in opset["templates_enabled"]:
        for _ in range(8):
            tree = generate_tree(rng, opset, template=tpl)
            cats = _cats(tree, set())
            assert "ts" in cats and "cs" in cats, f"{tpl}: 缺 ts/cs"
            _check_spaces(tree)
            src, _imports = render_factor_source(tree, "tpl_test", template=tpl)
            assert f"template: {tpl}" in src
            assert "def factor(env):" in src
            ns = {}
            exec(compile(src, "<tpl>", "exec"), ns)
            F = ns["factor"](env)
            assert F.shape == (env.T, env.N)


def test_generate_one_share():
    opset = effective_operator_set()
    assert DEFAULT_TEMPLATE_SHARE == 0.3
    rng = np.random.default_rng(9)
    seen_any = seen_tpl = 0
    for _ in range(300):
        _tree, tpl = generate_one(rng, opset)
        seen_any += tpl is None
        seen_tpl += tpl is not None
    assert seen_any > 0 and seen_tpl > 0
    # share=1 全模板；share=0 纯随机
    opset1 = dict(opset, template_share=1.0)
    for _ in range(50):
        _tree, tpl = generate_one(rng, opset1)
        assert tpl is not None
    opset0 = dict(opset, template_share=0.0)
    for _ in range(50):
        _tree, tpl = generate_one(rng, opset0)
        assert tpl is None


def test_templates_config_override():
    with tempfile.TemporaryDirectory() as d:
        eff = write_operator_override(
            {"disable_templates": ["cond_gate"], "template_share": 0.5}, d)
        assert "cond_gate" not in eff["templates_enabled"]
        assert set(TEMPLATES) - {"cond_gate"} == set(eff["templates_enabled"])
        assert eff["template_share"] == 0.5


# ---------------------------------------------------------------------------
# 健康门 + 叶子过滤
# ---------------------------------------------------------------------------
def test_matrix_health():
    good = np.random.default_rng(0).normal(0, 1, (100, 10))
    assert matrix_health(good) is None
    assert matrix_health(np.ones((100, 10)))["pathology"] == "degenerate_zero_variance"
    arr = good.copy()
    arr[60:, :] = np.inf
    assert matrix_health(arr)["pathology"] == "low_finite_frac"
    arr2 = good.copy()
    arr2[60:, :] *= 1e15
    assert matrix_health(arr2)["pathology"] == "scale_explosion"


def test_env_leaves():
    env = _synthetic_env()
    assert "amount" in env_leaves(env)
    env_no_amt = FactorEnv(env.o, env.h, env.l, env.c, env.v, env.dates,
                           env.symbols, listed=env.listed)
    assert "amount" not in env_leaves(env_no_amt)


# ---------------------------------------------------------------------------
# 算子集指纹（缝1正修）
# ---------------------------------------------------------------------------
def test_opset_fingerprint_sensitivity():
    fp1 = opset_fingerprint(effective_operator_set())
    assert fp1 == opset_fingerprint(effective_operator_set())  # 稳定
    with tempfile.TemporaryDirectory() as d:
        assert opset_fingerprint(
            write_operator_override({"disable": ["ts_kurt"]}, d)) != fp1
        assert opset_fingerprint(
            write_operator_override({"windows": [5, 10]}, d)) != fp1
        assert opset_fingerprint(
            write_operator_override({"humps": [0.02]}, d)) != fp1
        assert opset_fingerprint(
            write_operator_override({"template_share": 0.0}, d)) != fp1


def test_null_calibration_with_templates_and_health():
    """null 校准：模板同通道 + 病态计数 + 基本结构完整。"""
    env = _synthetic_env()
    with tempfile.TemporaryDirectory() as d:
        opset = dict(effective_operator_set(), template_share=0.5)
        r = run_null_calibration(env, d, n=12, seed=42, opset=opset)
        assert r["n_generated"] == 12
        assert r["n_valid"] > 0
        assert "n_pathological" in r
        main = str(env.calibration.horizon)
        assert r["ic_ir"][main]["p95"] is not None


def test_all_registry_ops_evaluate():
    """注册表内每个算子在合成面板上可评估（形状保持、无异常）。

    动机（2026-09-18）：ts_slope 死算子事故——时间索引构造错误导致任何
    含它的树整棵 ValueError，被生成端 except 静默吞掉，ts_slope 实际从未
    参与过采样产出。此测试堵住该类缺口：算子进注册表 = 必须可评估。
    """
    env = _synthetic_env(T=300, N=8)
    from dsh_factor_mining.factor.random_gen import _eval_tree
    from dsh_factor_mining.factor.random_gen import Node as _Node
    w, d, h = 20, 3, 0.05
    for cat, entries in ops_mod.OPERATOR_REGISTRY.items():
        for name, (_fn, kind) in entries.items():
            if cat == "cmp":
                node = _Node(name, "cmp", None, [_Node("c", "leaf"), _Node("o", "leaf")])
            elif cat == "cond":
                node = _Node(name, "cond", None,
                             [_Node("gt", "cmp", None, [_Node("c", "leaf"), _Node("o", "leaf")]),
                              _Node("v", "leaf"), _Node("amount", "leaf")])
            elif name in ops_mod.THREE_INPUT_TS:
                node = _Node(name, "ts", w,
                             [_Node("c", "leaf"), _Node("v", "leaf"), _Node("o", "leaf")])
            elif name in ops_mod.TWO_INPUT_TS:
                node = _Node(name, "ts", w, [_Node("c", "leaf"), _Node("v", "leaf")])
            elif cat == "ts":
                p = {"w": w, "d": d, "h": h, "none": None}[kind]
                node = _Node(name, "ts", p, [_Node("c", "leaf")])
            elif cat == "binary":
                node = _Node(name, "binary", None, [_Node("c", "leaf"), _Node("o", "leaf")])
            else:  # cs / unary
                node = _Node(name, cat, None, [_Node("c", "leaf")])
            F = _eval_tree(node, env)  # 任何算子崩 = 注册表含死算子
            arr = np.asarray(F, dtype=np.float64)
            assert arr.shape == (env.T, env.N), f"{name}: 形状 {arr.shape}"


# ---------------------------------------------------------------------------
# 第二批算子（2026-09-19）
# ---------------------------------------------------------------------------
def test_ts_wmean():
    x = pd.DataFrame(np.arange(1, 6, dtype=float).reshape(-1, 1))
    wgt = pd.DataFrame(np.array([[1.0], [0.0], [0.0], [0.0], [1.0]]))
    out = ops_mod.ts_wmean(x, wgt, 5)
    # 只有非零权重日计入：(1·1 + 5·1)/(1+1) = 3.0
    assert abs(out.iloc[-1, 0] - 3.0) < 1e-12


def test_ts_drawdown_range_pos():
    x = pd.DataFrame(np.array([[100.0], [110.0], [99.0], [105.0]]))
    dd = ops_mod.ts_drawdown(x, 3)
    assert abs(dd.values[2, 0] - (-0.1)) < 1e-12   # 99/110 − 1
    assert dd.values[1, 0] == 0.0                    # 自身即窗口 max
    rp = ops_mod.ts_range_pos(x, 3)
    assert abs(rp.values[1, 0] - 1.0) < 1e-12        # 处于窗口 max
    assert rp.values[2, 0] == 0.0                    # 处于窗口 min
    assert (rp.values[1:] >= 0).all() and (rp.values[1:] <= 1).all()


def test_ts_arg_max_min():
    x = pd.DataFrame(np.array([[5.0], [3.0], [8.0], [8.0], [1.0]]))
    am = ops_mod.ts_arg_max(x, 5)
    # 窗口 [5,3,8,8,1]：argmax=2（首次最大），离最新 = 4−2 = 2
    assert am.values[-1, 0] == 2.0
    an = ops_mod.ts_arg_min(x, 5)
    assert an.values[-1, 0] == 0.0                   # 最小值即最新


def test_ts_median_iqr_winsor():
    rng = np.random.default_rng(2)
    vals = rng.normal(0, 1, (80, 4))
    x = pd.DataFrame(vals)
    assert abs(ops_mod.ts_median(x, 20).values[-1, 0]
               - np.median(vals[-20:, 0])) < 1e-12
    q = np.quantile(vals[-20:, 0], [0.25, 0.75])
    assert abs(ops_mod.ts_iqr(x, 20).values[-1, 0] - (q[1] - q[0])) < 1e-9
    x2 = x.copy()
    x2.iloc[79, 0] = 100.0
    seg = x2.values[60:80, 0]
    hi = seg.mean() + 3.0 * seg.std(ddof=1)
    w = ops_mod.ts_winsor(x2, 20).values[79, 0]
    assert abs(w - hi) < 1e-9 and w < 100.0          # 离群点被截到上界


def test_ts_rank_corr():
    rng = np.random.default_rng(3)
    x = pd.DataFrame(rng.normal(0, 1, (100, 2)))
    y = x.pow(3)   # 严格单调变换 → 滚动秩完全一致
    r = ops_mod.ts_rank_corr(x, y, 30).values[-1, :]
    assert np.all(np.abs(r - 1.0) < 1e-9)


def test_ts_streak():
    x = pd.DataFrame(np.array([[1.0], [2.0], [3.0], [2.0], [2.0], [1.0], [0.5], [0.4]]))
    out = ops_mod.ts_streak(x, 10)
    # sign(diff): [nan,+,+,−,0,−,−,−] → 连涨 2、反号 1、零号 1(值 0)、连跌 3
    assert np.isnan(out.values[0, 0])
    assert out.values[1, 0] == 1.0
    assert out.values[2, 0] == 2.0
    assert out.values[3, 0] == -1.0
    assert out.values[4, 0] == 0.0
    assert out.values[5, 0] == -1.0
    assert out.values[6, 0] == -2.0
    assert out.values[7, 0] == -3.0


def test_ts_coskew_triple_bruteforce():
    rng = np.random.default_rng(7)
    T, N, w = 60, 3, 20
    x = pd.DataFrame(rng.normal(0, 1, (T, N)))
    y = pd.DataFrame(rng.normal(0, 2, (T, N)))
    z = pd.DataFrame(rng.normal(0, 0.5, (T, N)))
    csk = ops_mod.ts_coskew(x, y, w)
    tcp = ops_mod.ts_triple_corr(x, y, z, w)
    for t in range(w - 1, T):
        for j in range(N):
            xs, ys, zs = (x.values[t - w + 1:t + 1, j],
                          y.values[t - w + 1:t + 1, j],
                          z.values[t - w + 1:t + 1, j])
            mx, my, mz = xs.mean(), ys.mean(), zs.mean()
            sx, sy, sz = xs.std(ddof=1), ys.std(ddof=1), zs.std(ddof=1)
            ref_c = ((xs - mx) ** 2 * (ys - my)).mean() / (sx ** 2 * sy)
            ref_t = ((xs - mx) * (ys - my) * (zs - mz)).mean() / (sx * sy * sz)
            assert abs(csk.values[t, j] - ref_c) < 1e-8, (t, j)
            assert abs(tcp.values[t, j] - ref_t) < 1e-8, (t, j)


def test_cs_demean_robust_z():
    rng = np.random.default_rng(4)
    x = pd.DataFrame(rng.normal(5, 2, (30, 12)))
    dm = ops_mod.cs_demean(x)
    assert np.allclose(dm.mean(axis=1).values, 0.0, atol=1e-12)
    rz = ops_mod.cs_robust_z(x)
    assert np.allclose(np.median(rz.values, axis=1), 0.0, atol=1e-12)


def test_transforms_batch2():
    x = pd.DataFrame(np.array([[-3.0], [-0.1], [0.0], [0.5], [100.0]]))
    ls = ops_mod.log_signed(x)
    assert np.sign(ls.values.ravel()).tolist() == [-1.0, -1.0, 0.0, 1.0, 1.0]
    assert abs(ls.values[4, 0] - np.log1p(100.0)) < 1e-12
    ss = ops_mod.softsign(x)
    assert (ss.abs().values < 1).all()
    assert abs(ss.values[4, 0] - 100.0 / 101.0) < 1e-12
    sf = ops_mod.sfdiff(pd.DataFrame(np.array([[1.0], [3.0]])),
                        pd.DataFrame(np.array([[3.0], [1.0]])))
    assert np.allclose(sf.values, [[-0.5], [0.5]])


if __name__ == "__main__":
    fns = [test_ts_resid_beta, test_ts_decay_exp, test_hump, test_where_gt_lt,
           test_ts_momentum_zero_guard, test_space_rules_property,
           test_templates_structure_and_render, test_generate_one_share,
           test_templates_config_override, test_matrix_health, test_env_leaves,
           test_opset_fingerprint_sensitivity,
           test_null_calibration_with_templates_and_health,
           test_all_registry_ops_evaluate,
           test_ts_wmean, test_ts_drawdown_range_pos, test_ts_arg_max_min,
           test_ts_median_iqr_winsor, test_ts_rank_corr, test_ts_streak,
           test_ts_coskew_triple_bruteforce, test_cs_demean_robust_z,
           test_transforms_batch2]
    failures = []
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e!r}")
    if failures:
        raise SystemExit(1)
    print("RANDOM_GEN_EXPANSION_TESTS PASS")
