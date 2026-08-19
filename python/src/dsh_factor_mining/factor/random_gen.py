# coding=utf-8
"""随机因子种子生成器 — 组合树采样 + factor 源码渲染 + 结构约束校验。

两种模式（用户决策 2026-08-17）：
  - explore（探索）：生成 n 个随机因子 → 轻量 IC 扫描 → 返回 top_k 的源码
    + 表达式 + 轻量诊断。随机幸存是**选择**不是结论：agent 拿 top 源码走
    标准管线（causality → evaluate → evaluate_batch deflate）。
  - null-calibration（null 地形）：生成 n 个 → 全部轻量 IC → 经验 null
    分布（IC_IR 分位数）持久化到 stateRoot/null_landscape.json。
    后续所有因子诊断可引用"相对随机基线的分位"——比 column-perm（单因子
    内部置换）更全局的池子难度校准。

结构约束（防浅层的生死线，[[deep-signal-mining-methodology]] 的代码化）：
  - 至少 1 个时序算子（ts_*）+ 至少 1 个截面算子（cs_*）
    = "提取不可直接观测的潜在结构"的可执行判据
  - 二元算子的两个子树不能都是叶子（禁 c1/c2 纯数据运算）
  - 全部算子来自 ops.OPERATOR_REGISTRY（因果安全、向量化）

随机其实不随机（诚实边界）：curated 算子集是人类先验，决定了能发现什么。
这是"结构化空间的随机探索"，不是无偏搜索。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .ops import (DEFAULT_DELAYS, DEFAULT_WINDOWS, LEAVES, OPERATOR_REGISTRY, TWO_INPUT_TS)


# ---------------------------------------------------------------------------
# 生效算子集（默认 ∪ 用户覆盖，覆盖文件存 stateRoot/operator_set.json）
# ---------------------------------------------------------------------------
def _operator_set_path(state_root):
    return os.path.join(state_root, "operator_set.json")


def read_operator_override(state_root):
    """读用户算子集覆盖（无则空 dict）。"""
    p = _operator_set_path(state_root)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def effective_operator_set(state_root=None):
    """生效算子集 = {category: {name: (fn, param)}} ∪ 用户覆盖后的窗口/延迟。

    覆盖文件结构（全部可选）：
      {"disable": ["ts_kurt", "sigmoid"],          # 禁用算子名
       "windows": [5, 10, 20, 60],                  # 覆盖窗口采样集
       "delays": [1, 5]}                            # 覆盖延迟采样集
    """
    ov = read_operator_override(state_root) if state_root else {}
    disable = set(ov.get("disable", []))
    windows = ov.get("windows", DEFAULT_WINDOWS)
    delays = ov.get("delays", DEFAULT_DELAYS)

    ops = {}
    for cat, entries in OPERATOR_REGISTRY.items():
        kept = {n: t for n, t in entries.items() if n not in disable}
        if kept:
            ops[cat] = kept
    return {"ops": ops, "windows": list(windows), "delays": list(delays),
            "disabled": sorted(disable)}


def write_operator_override(override, state_root):
    """写用户算子集覆盖（全量覆盖式）。"""
    os.makedirs(state_root, exist_ok=True)
    with open(_operator_set_path(state_root), "w", encoding="utf-8") as f:
        json.dump(override, f, ensure_ascii=False, indent=2)
    return effective_operator_set(state_root)


# ---------------------------------------------------------------------------
# 组合树
# ---------------------------------------------------------------------------
@dataclass
class Node:
    op: str                       # 算子名或叶子字段名
    category: str                 # ts | cs | binary | unary | leaf
    param: int | None = None      # w 或 d
    children: list = field(default_factory=list)

    def to_expression(self) -> str:
        """渲染为可读表达式（诊断/记录用）。"""
        if self.category == "leaf":
            return self.op
        args = [c.to_expression() for c in self.children]
        head = self.op if self.param is None else f"{self.op}[{self.param}]"
        return f"{head}({', '.join(args)})"

    def to_code(self, leaf_exprs) -> str:
        """渲染为 pandas 代码片段（叶子用 leaf_exprs 映射到 env 表达式）。"""
        if self.category == "leaf":
            return leaf_exprs[self.op]
        args = [c.to_code(leaf_exprs) for c in self.children]
        if self.param is None:
            return f"{self.op}({', '.join(args)})"
        return f"{self.op}({args[0]}, {self.param})" if len(args) == 1 else \
            f"{self.op}({args[0]}, {args[1]}, {self.param})"


def _sample_param(kind, rng, windows, delays):
    if kind == "w":
        return int(rng.choice(windows))
    if kind == "d":
        return int(rng.choice(delays))
    return None


def generate_tree(rng, opset, min_depth=3, max_depth=5, leaves=None):
    """生成一棵满足结构约束的组合树。

    约束由生成保证（后不需单独校验）：
      - 至少 1 ts + 1 cs 算子
      - 二元算子的子树不全是叶子
      - 深度 3..max_depth（根=1）。边界说明（2026-08-18 独立审计 F-D11 对齐）：
        `depth >= max_depth` 且 force_ts/force_cs 未满足时，收尾节点会把叶子挂到
        max_depth+1 层（含 TWO_INPUT_TS 的第二叶）——这是有界边界行为而非违约，
        实测深度分布为 3..6（约 19% 的树达 6，均为收尾叶子层）。
    """
    ops, windows, delays = opset["ops"], opset["windows"], opset["delays"]
    # 叶子集按 env 实际可用字段过滤（amount 缺失时含 amount 的树必然整棵丢弃，
    # 会把 null 分布系统性偏离 volume/amount 类因子）
    leaves = leaves or LEAVES
    ts_names = list(ops.get("ts", {}).keys())
    cs_names = list(ops.get("cs", {}).keys())
    bin_names = list(ops.get("binary", {}).keys())
    una_names = list(ops.get("unary", {}).keys())
    if not ts_names or not cs_names:
        raise ValueError("算子集必须至少含 1 个 ts_* 与 1 个 cs_* 算子")

    def _grow(depth: int, force_ts: bool, force_cs: bool) -> Node:
        # 到达最大深度 → 只能是单输入算子或叶子
        if depth >= max_depth:
            if force_ts:
                name = rng.choice(ts_names)
                # 深度耗尽时双输入 ts 用叶子对叶子（子树约束只限 binary）
                kind = OPERATOR_REGISTRY["ts"][name][1]
                p = _sample_param(kind, rng, windows, delays)
                kids = [Node(rng.choice(leaves), "leaf")]
                if name in TWO_INPUT_TS:
                    kids.append(Node(rng.choice(leaves), "leaf"))
                return Node(name, "ts", p, kids)
            if force_cs:
                name = rng.choice(cs_names)
                return Node(name, "cs", None, [_leaf()])
            return _leaf()
        # 顶部结构：保证 ts 与 cs 至少各一次 —— 根附近先 ts，尾端收 cs
        if force_ts and force_cs and depth == 1:
            # 根 = ts 算子，其子树里必含一个 cs
            name = rng.choice(ts_names)
            kind = OPERATOR_REGISTRY["ts"][name][1]
            p = _sample_param(kind, rng, windows, delays)
            kids = [_grow(depth + 1, force_ts=False, force_cs=True)]
            if name in TWO_INPUT_TS:
                kids.append(_grow(depth + 1, force_ts=False, force_cs=False))
            return Node(name, "ts", p, kids)
        # 中间节点：ts / cs / binary / unary / leaf 按权重采
        # binary 有限制（子树不能全叶），且深度余量 >=2 才能保孙非叶
        choices = ["ts", "cs", "unary", "leaf"]
        if depth + 2 <= max_depth:
            choices += ["binary", "binary"]   # 鼓励结构组合
        if force_ts:
            choices += ["ts"]
        if force_cs:
            choices += ["cs"]
        pick = rng.choice(choices)
        if pick == "leaf" and (force_ts or force_cs):
            pick = "ts" if force_ts else "cs"
        if pick == "ts":
            name = rng.choice(ts_names)
            kind = OPERATOR_REGISTRY["ts"][name][1]
            p = _sample_param(kind, rng, windows, delays)
            kids = [_grow(depth + 1, force_ts=False, force_cs=force_cs)]
            if name in TWO_INPUT_TS:
                kids.append(_grow(depth + 1, force_ts=False, force_cs=False))
            return Node(name, "ts", p, kids)
        if pick == "cs":
            name = rng.choice(cs_names)
            return Node(name, "cs", None,
                        [_grow(depth + 1, force_ts=force_ts, force_cs=False)])
        if pick == "binary":
            name = rng.choice(bin_names)
            # 约束：两子树不全是叶子 → 至少一侧强制非叶（给 unary/ts）
            side = rng.random() < 0.5
            a = _grow(depth + 1, force_ts=False, force_cs=False)
            if a.category == "leaf":
                a = _grow(depth + 1, force_ts=False, force_cs=False)
                # 仍可能为叶（深度允许 unary），换一个必非叶节点
                if a.category == "leaf":
                    a = _unary_or_ts(rng, ops, windows, delays, force_cs)
            b = _grow(depth + 1, force_ts=force_ts, force_cs=force_cs)
            if b.category == "leaf" and a.category == "leaf":
                b = _unary_or_ts(rng, ops, windows, delays, force_cs)
            return Node(name, "binary", None, [a, b])
        if pick == "unary":
            name = rng.choice(una_names)
            return Node(name, "unary", None,
                        [_grow(depth + 1, force_ts=force_ts, force_cs=force_cs)])
        return _leaf()

    def _leaf() -> Node:
        return Node(rng.choice(leaves), "leaf")

    def _unary_or_ts(rng, ops, windows, delays, force_cs) -> Node:
        una = ops.get("unary", {})
        if una and rng.random() < 0.6:
            return Node(rng.choice(list(una.keys())), "unary", None, [_leaf()])
        ts = ops.get("ts", {})
        name = rng.choice(list(ts.keys()))
        kind = OPERATOR_REGISTRY["ts"][name][1]
        p = _sample_param(kind, rng, windows, delays)
        kids = [_leaf()]
        if name in TWO_INPUT_TS:
            kids.append(_leaf())
        return Node(name, "ts", p, kids)

    root = _grow(1, force_ts=True, force_cs=True)
    return root


def render_factor_source(tree: Node, name: str) -> tuple[str, list[str]]:
    """把组合树渲染成自包含的 factor(env) 源码。

    返回 (source, imports)。源码引用包内 ops 算子库（算子真值源，可配置）。
    函数名固定 factor（2026-08-18 生产审计修正）：下游 causality/evaluate/batch
    的契约都要求 def factor(env)——此前渲染 def random_factor_N(env)，agent 每
    轮冷启动必踩 5 次「缺少 factor(env)」ERR。tree id 移入注释保留可读性。
    """
    leaf_exprs = {
        "o": "pd.DataFrame(env.o)", "h": "pd.DataFrame(env.h)",
        "l": "pd.DataFrame(env.l)", "c": "pd.DataFrame(env.c)",
        "v": "pd.DataFrame(env.v)", "amount": "pd.DataFrame(env.amount)",
    }
    # 收集树里用到的算子名 → import 列表
    used = []

    def _collect(n: Node):
        if n.category != "leaf":
            used.append(n.op)
        for ch in n.children:
            _collect(ch)
    _collect(tree)

    body = tree.to_code(leaf_exprs)
    imports = ", ".join(sorted(set(used)))
    src = (
        f"# tree: {name} | expression: {tree.to_expression()}\n"
        f"def factor(env):\n"
        f"    import pandas as pd\n"
        f"    from dsh_factor_mining.factor.ops import {imports}\n"
        f"    return ({body}).values\n"
    )
    return src, sorted(set(used))


# ---------------------------------------------------------------------------
# 轻量 IC 扫描（阶段 1：只算 IC 序列，不跑 causality/column-perm/beta）
# ---------------------------------------------------------------------------
def light_ic_scan(F, env):
    """轻量诊断：不重叠口径 IC 序列的 mean/ir/n（development 区）。

    与 evaluate 的 _cross_sectional_ic 同口径（sig_only + DEV_END 切分），
    但不含 column-perm / beta / topn / decay 等重计算。
    """
    from .evaluate import _cross_sectional_ic, _forward_returns, _pit_mask
    import numpy as _np

    F = _np.asarray(F, dtype=_np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 形状 {F.shape} != {(env.T, env.N)}")
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    ic = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    dev_end = env.calibration.dev_end
    if dev_end is None:
        # 无显式分界（直调 API）：null 校准保守用前 60% 样本——
        # 既不被三区卡死，也不把 selection/test 段纳入难度基线。
        dev_end = str(pd.DatetimeIndex(env.dates)[int(env.T * 0.6)].date())
    ic = ic[ic.index < pd.Timestamp(dev_end)]
    if len(ic) < 2:
        return {"ic_mean": None, "ic_ir": None, "n": len(ic)}
    mean, std = float(ic.mean()), float(ic.std(ddof=1))
    return {"ic_mean": mean, "ic_ir": mean / std if std > 0 else None, "n": len(ic)}


# ---------------------------------------------------------------------------
# null 地形（持久化 + 查询）
# ---------------------------------------------------------------------------
def _null_landscape_path(state_root):
    return os.path.join(state_root, "null_landscape.json")


def run_null_calibration(env, state_root, n=50, seed=42, opset=None, on_progress=None,
                         env_fingerprint=None):
    """null 地形：n 个随机因子的 IC_IR 经验分布，持久化。

    返回 dict：分位数 + 元信息。后续因子诊断可引用
    "相对随机基线 p95 的分位"（bridge 的 factor.null_landscape 查询）。
    on_progress(done, total)：进度回调（长任务防静默，批次1a）。

    env_fingerprint（2026-08-19 指纹硬门）：调用方（bridge）传入的环境三元组
    指纹（数据文件+口径+引擎版本）。evaluate 读地形做 pool_std 估计时校验
    指纹——不匹配视为无效（换数据集后旧地形不得继续当基线）。旧版文件无
    此字段同样判不匹配（宁可保守：重跑一次校准，几分钟）。
    """
    opset = opset or effective_operator_set(state_root)
    leaves = ["o", "h", "l", "c", "v"] + (["amount"] if getattr(env, "amount", None) is not None else [])
    rng = np.random.default_rng(seed)
    irs, means = [], []
    for _i in range(n):
        if on_progress is not None:
            try:
                on_progress(_i, n)
            except Exception:
                pass
        tree = generate_tree(rng, opset, leaves=leaves)
        # 内联执行：直接调 ops 算子（不经源码编译，同数学）
        try:
            F = _eval_tree(tree, env)
            r = light_ic_scan(F, env)
        except Exception:
            continue
        if r["ic_ir"] is not None and np.isfinite(r["ic_ir"]):
            irs.append(r["ic_ir"])
            means.append(r["ic_mean"])
    irs = np.array(irs) if irs else np.array([np.nan])

    def _q(p):
        return float(np.nanpercentile(irs, p)) if len(irs) else None

    result = {
        "n_generated": n, "n_valid": int(len(irs)), "seed": seed,
        "env_fingerprint": env_fingerprint,
        "ic_ir": {
            "p10": _q(10), "p25": _q(25), "p50": _q(50),
            "p75": _q(75), "p90": _q(90), "p95": _q(95), "p99": _q(99),
            "max": float(np.nanmax(irs)) if len(irs) else None,
            "mean_abs": float(np.nanmean(np.abs(irs))) if len(irs) else None,
        },
        "interpretation": (
            "经验 null 分布：随机因子的 IC_IR 集中在 p50 附近。"
            "新因子 IC_IR 超过 p95 才值得认真对待；p99 以上是强信号。"
            "若 p95 本身已很高，说明该池子极易产生过拟合信号。"
        ),
        "sampling_note": (
            f"n={n}：p95 的估计误差约 ±10 个百分位，p99 基于不足 1 个期望观测、"
            "只能当方向参考——需要精确尾部分位时用 n>=200 重跑"
        ),
    }
    os.makedirs(state_root, exist_ok=True)
    with open(_null_landscape_path(state_root), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def read_null_landscape(state_root):
    p = _null_landscape_path(state_root)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 内联树执行（null 校准用，与渲染源码同一套算子）
# ---------------------------------------------------------------------------
def _eval_tree(node: Node, env):
    """直接对树求值（不渲染源码）。叶子→DataFrame，算子→ops 函数。"""
    import pandas as pd
    from . import ops as _ops
    if node.category == "leaf":
        return pd.DataFrame(getattr(env, node.op))
    fn = None
    for cat in ("ts", "cs", "binary", "unary"):
        if node.op in OPERATOR_REGISTRY.get(cat, {}):
            fn = OPERATOR_REGISTRY[cat][node.op][0]
            break
    if fn is None:
        raise ValueError(f"未知算子: {node.op}")
    kids = [_eval_tree(ch, env) for ch in node.children]
    if node.param is None:
        return fn(*kids)
    return fn(*kids, node.param)
