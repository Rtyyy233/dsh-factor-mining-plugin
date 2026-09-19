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

from .ops import (DEFAULT_DELAYS, DEFAULT_HUMPS, DEFAULT_WINDOWS, LEAVES,
                  OPERATOR_REGISTRY, SAME_SPACE_BINARY, THREE_INPUT_TS,
                  TWO_INPUT_TS)


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
       "delays": [1, 5],                            # 覆盖延迟采样集
       "humps": [0.01, 0.05],                       # 换手族阈值采样集（2026-09-18）
       "disable_templates": ["cond_gate"],          # 禁用结构模板（2026-09-18）
       "template_share": 0.3}                       # 模板采样占比（0=纯随机）
    """
    ov = read_operator_override(state_root) if state_root else {}
    disable = set(ov.get("disable", []))
    windows = ov.get("windows", DEFAULT_WINDOWS)
    delays = ov.get("delays", DEFAULT_DELAYS)
    humps = ov.get("humps", DEFAULT_HUMPS)
    disable_tpls = set(ov.get("disable_templates", []))
    template_share = float(ov.get("template_share", DEFAULT_TEMPLATE_SHARE))

    ops = {}
    for cat, entries in OPERATOR_REGISTRY.items():
        kept = {n: t for n, t in entries.items() if n not in disable}
        if kept:
            ops[cat] = kept
    return {"ops": ops, "windows": list(windows), "delays": list(delays),
            "humps": list(humps), "disabled": sorted(disable),
            "templates_enabled": [t for t in TEMPLATES if t not in disable_tpls],
            "template_share": template_share}


def opset_fingerprint(opset) -> str:
    """生效算子集指纹（2026-09-18 缝1正修）：null 地形绑定专用。

    覆盖：算子名单+参数类别+算子实现源码哈希（改实现=改 null 分布）、
    窗口/延迟/阈值采样集、模板集与占比。landscape 写入与校验共用此函数
    ——扩算子/改实现/改占比后旧地形判 mismatch，强制重校准（此前指纹只含
    数据+口径+引擎版本，算子集变更静默漂移 pool_std 口径）。
    """
    import hashlib
    import inspect
    from . import ops as _ops
    parts = {}
    for cat, entries in opset["ops"].items():
        for name, (fn, kind) in entries.items():
            try:
                src = inspect.getsource(fn)
            except Exception:
                src = ""
            parts[f"{cat}/{name}"] = [kind,
                                      hashlib.sha256(src.encode("utf-8")).hexdigest()[:12]]
    payload = {
        "ops": parts,
        "windows": sorted(opset.get("windows", [])),
        "delays": sorted(opset.get("delays", [])),
        "humps": sorted(opset.get("humps", [])),
        "templates_enabled": sorted(opset.get("templates_enabled") or []),
        "template_share": float(opset.get("template_share", DEFAULT_TEMPLATE_SHARE)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def env_leaves(env):
    """按 env 实际可用字段过滤叶子（amount 缺失时剔除）。

    2026-09-18 缝2c：explore 补齐同款过滤（此前只有 null 校准过滤——
    无 amount 的 env 上 explore 含 amount 的树整棵 error）。
    """
    return ["o", "h", "l", "c", "v"] + (
        ["amount"] if getattr(env, "amount", None) is not None else [])


def matrix_health(F) -> dict | None:
    """生成树输出健康门（2026-09-18 缝2b）：None=健康，dict=病态描述。

    两道门对已知病态（construction_ledger Day4 除法 inf 事故 / cp_z 1e15
    尺度事故 / 09-14 噪声世界退化树）：
      - finite 门：后半面板 isfinite 占比 < 0.5（前半允许滚动暖机 NaN）
      - 尺度门：|p99| > 1e12 或零方差（退化常量树）
    """
    arr = np.asarray(F, dtype=np.float64)
    if arr.size == 0:
        return {"pathology": "empty"}
    half = arr[arr.shape[0] // 2:]
    fin = np.isfinite(half)
    if float(fin.mean()) < 0.5:
        return {"pathology": "low_finite_frac",
                "finite_frac": round(float(fin.mean()), 4)}
    vals = half[fin]
    if vals.size == 0 or float(np.std(vals)) == 0.0:
        return {"pathology": "degenerate_zero_variance"}
    p99 = float(np.percentile(np.abs(vals), 99))
    if not np.isfinite(p99) or p99 > 1e12:
        return {"pathology": "scale_explosion", "abs_p99": p99}
    return None


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
        return f"{self.op}({', '.join(args)}, {self.param})"


def _sample_param(kind, rng, windows, delays, humps=None):
    if kind == "w":
        return int(rng.choice(windows))
    if kind == "d":
        return int(rng.choice(delays))
    if kind == "h":
        return float(rng.choice(humps if humps else DEFAULT_HUMPS))
    return None


def _space(n: Node) -> str:
    """子树输出空间（2026-09-18 结构扩充的最小类型系统）：raw | norm | bool。

    规则（防已知废树类，只约束组合合法性、不改数学）：
      R1 cs_* 的输入必须是 raw —— cs_rank(cs_zscore(x)) 按日仿射不变性
         恰等于 cs_rank(x)，双重截面归一是纯冗余（FARM_REVIEW 实测废树类）
      R2 add/sub/minv/maxv/signed_diff 与 gt/lt 两子树同空间（单位匹配，
         add(价格, 排名) 类混尺度废树）；where 两分支同空间。mul/div 自由
         ——乘除=加权/比值语义，mul(价格腿, 排名腿) 正是战役兑现的量腿加权
    空间：leaf=raw；ts=raw（时序聚合不改截面归一性）；cs=norm；unary 传递；
    mul/div 双 norm 才 norm；gt/lt=bool；where=分支空间。
    """
    if n.category in ("leaf", "ts"):
        return "raw"
    if n.category == "cs":
        return "norm"
    if n.category == "unary":
        return _space(n.children[0])
    if n.category == "cmp":
        return "bool"
    if n.category == "cond":
        return _space(n.children[1])
    if n.op in SAME_SPACE_BINARY:
        return _space(n.children[0])
    return "norm" if (_space(n.children[0]) == "norm"
                      and _space(n.children[1]) == "norm") else "raw"


def generate_tree(rng, opset, min_depth=3, max_depth=5, leaves=None, template=None):
    """生成一棵满足结构约束的组合树（2026-09-18 结构扩充：空间类型+模板通道）。

    约束由生成保证（后不需单独校验）：
      - 至少 1 个时序算子（ts_*）+ 至少 1 个截面算子（cs_*）
      - 二元算子的两个子树不能都是叶子（禁 c1/c2 纯数据运算）
      - 空间类型规则见 _space docstring
    - 深度 3..max_depth（根=1）。边界说明（2026-08-18 独立审计 F-D11 对齐）：
      深度耗尽且 force/空间未满足时收尾节点可把叶子挂到 max_depth+1 层
      （空间 wrap 如 ts(cs(leaf)) 可到 +2）——有界边界行为而非违约。
    - template（结构模板名）：走带槽骨架（出身=结构先验，非零先验），
      槽内组合仍均匀随机。
    """
    if template is not None:
        return _from_template(rng, opset, leaves or LEAVES, template)
    ops, windows, delays = opset["ops"], opset["windows"], opset["delays"]
    humps = opset.get("humps") or DEFAULT_HUMPS
    leaves = leaves or LEAVES
    ts_names = list(ops.get("ts", {}).keys())
    cs_names = list(ops.get("cs", {}).keys())
    bin_names = list(ops.get("binary", {}).keys())
    una_names = list(ops.get("unary", {}).keys())
    cmp_names = list(ops.get("cmp", {}).keys())
    cond_names = list(ops.get("cond", {}).keys()) if cmp_names else []
    if not ts_names or not cs_names:
        raise ValueError("算子集必须至少含 1 个 ts_* 与 1 个 cs_* 算子")
    ts_single = [n for n in ts_names
                 if n not in TWO_INPUT_TS and n not in THREE_INPUT_TS]

    def _leaf() -> Node:
        return Node(rng.choice(leaves), "leaf")

    def _param(name):
        return _sample_param(OPERATOR_REGISTRY["ts"][name][1], rng, windows, delays, humps)

    def _ts_leaf(name, child=None):
        return Node(name, "ts", _param(name), [child or _leaf()])

    def _grow(depth: int, force_ts: bool, force_cs: bool, need: str = "any") -> Node:
        # ---- 边界：深度耗尽 ----
        if depth >= max_depth:
            if need == "bool":
                return Node(rng.choice(cmp_names), "cmp", None, [_leaf(), _leaf()])
            if force_ts and need == "norm":
                # norm 且必须含 ts：cs(ts(leaf)) —— cs 产 norm，ts 藏其内
                return Node(rng.choice(cs_names), "cs", None,
                            [_ts_leaf(rng.choice(ts_single))])
            if force_ts:
                name = rng.choice(ts_names)
                kids = [_leaf()]
                if name in THREE_INPUT_TS:
                    kids.append(_leaf())
                if name in TWO_INPUT_TS:
                    kids.append(_leaf())
                return Node(name, "ts", _param(name), kids)
            if force_cs and need == "raw":
                # raw 且必须含 cs：ts(cs(leaf)) —— ts 产 raw，cs 藏其内
                return _ts_leaf(rng.choice(ts_single),
                                Node(rng.choice(cs_names), "cs", None, [_leaf()]))
            if force_cs or need == "norm":
                return Node(rng.choice(cs_names), "cs", None, [_leaf()])
            return _leaf()

        # ---- 根部结构：保证 ts 与 cs 至少各一次（根=ts，cs 沿传播链）----
        if force_ts and force_cs and depth == 1:
            name = rng.choice(ts_names)
            kids = [_grow(depth + 1, False, True, "any")]
            if name in THREE_INPUT_TS:
                kids.append(_grow(depth + 1, False, False, "any"))
            if name in TWO_INPUT_TS:
                kids.append(_grow(depth + 1, False, False, "any"))
            return Node(name, "ts", _param(name), kids)

        # ---- 中间节点：类别采样 + 空间约束过滤 ----
        choices = ["ts", "cs", "unary", "leaf"]
        if depth + 2 <= max_depth:
            choices += ["binary", "binary"]   # 鼓励结构组合
        if cmp_names and depth + 2 <= max_depth:
            choices += ["cmp"]
        if cond_names and depth + 3 <= max_depth:
            choices += ["cond"]
        if force_ts:
            choices += ["ts"]
        if force_cs:
            choices += ["cs"]
        pick = rng.choice(choices)
        # 空间约束：need 决定本节点可产出的类别（bool 槽只收 cmp，norm 槽
        # 不收 ts/leaf/cmp，raw 槽不收 cs——重定向保 force 语义优先）
        allowed = {
            "any": {"ts", "cs", "unary", "binary", "cmp", "cond", "leaf"},
            "raw": {"ts", "unary", "binary", "cond", "leaf"},
            "norm": {"cs", "unary", "binary"},
            "bool": {"cmp"},
        }[need]
        if pick not in allowed:
            if force_ts and "ts" in allowed:
                pick = "ts"
            elif force_cs and "cs" in allowed:
                pick = "cs"
            elif need == "norm":
                pick = "cs"
            elif need == "raw":
                pick = "ts"
            else:
                pick = "cmp" if need == "bool" else "ts"
        if pick == "leaf" and (force_ts or force_cs):
            pick = "ts" if force_ts else ("cs" if need != "raw" else "ts")

        if pick == "ts":
            name = rng.choice(ts_names)
            kids = [_grow(depth + 1, False, force_cs, "any")]
            if name in THREE_INPUT_TS:
                kids.append(_grow(depth + 1, False, False, "any"))
            if name in TWO_INPUT_TS:
                kids.append(_grow(depth + 1, False, False, "any"))
            return Node(name, "ts", _param(name), kids)
        if pick == "cs":
            return Node(rng.choice(cs_names), "cs", None,
                        [_grow(depth + 1, force_ts, False, "raw")])
        if pick == "unary":
            return Node(rng.choice(una_names), "unary", None,
                        [_grow(depth + 1, force_ts, force_cs, need)])
        if pick == "cmp":
            # 两子树同空间：先生成 a，b 按 a 的空间生成。叶子对（gt(c,o)
            # 类日内方向门）语义合法，不限全叶——与 binary 的算术约束不同
            a = _grow(depth + 1, False, False, "any")
            b = _grow(depth + 1, force_ts, force_cs, _space(a))
            return Node(rng.choice(cmp_names), "cmp", None, [a, b])
        if pick == "cond":
            c = _grow(depth + 1, False, False, "bool")
            a = _grow(depth + 1, force_ts, False,
                      "raw" if need == "raw" else ("norm" if need == "norm" else "any"))
            b = _grow(depth + 1, False, force_cs, _space(a))
            if a.category == "leaf" and b.category == "leaf":
                b = _unary_or_ts(rng, ops, windows, delays, False)
            return Node(rng.choice(cond_names), "cond", None, [c, a, b])
        if pick == "binary":
            name = rng.choice(bin_names)
            if name in SAME_SPACE_BINARY:
                need_a = need if need in ("raw", "norm") else "any"
            else:
                # mul/div：norm 槽需双 norm 子树
                need_a = "norm" if need == "norm" else "any"
            a = _grow(depth + 1, False, False, need_a)
            if a.category == "leaf":
                a = _grow(depth + 1, False, False, need_a)
                if a.category == "leaf":
                    a = _unary_or_ts(rng, ops, windows, delays, False)
            if name in SAME_SPACE_BINARY:
                need_b = _space(a)
            elif need == "norm":
                need_b = "norm"
            elif need == "raw" and _space(a) == "norm":
                need_b = "raw"      # 保至少一侧 raw → mul/div 输出 raw
            else:
                need_b = "any"
            b = _grow(depth + 1, force_ts, force_cs, need_b)
            if b.category == "leaf" and a.category == "leaf":
                b = _unary_or_ts(rng, ops, windows, delays, force_cs)
            return Node(name, "binary", None, [a, b])
        return _leaf()

    def _unary_or_ts(rng, ops, windows, delays, force_cs) -> Node:
        una = ops.get("unary", {})
        if una and rng.random() < 0.6:
            return Node(rng.choice(list(una.keys())), "unary", None, [_leaf()])
        name = rng.choice(ts_single)
        kids = [_leaf()]
        if name in TWO_INPUT_TS:
            kids.append(_leaf())
        return Node(name, "ts", _param(name), kids)

    root = _grow(1, force_ts=True, force_cs=True)
    return root


# ---------------------------------------------------------------------------
# 结构模板（2026-09-18 结构扩充）：已兑现结构的带槽骨架，出身=结构先验。
# 战役证据：收官六入册全部来自加腿/拆杠杆/条件门——均匀树长不出这些形状。
# 渲染源码首行注释带 template 标记 + explore 返回携带 template 字段；
# null 校准同通道采样（墙必须覆盖实际搜索空间，模板与占比进 opset 指纹）。
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATE_SHARE = 0.3
TEMPLATES = ("leg_weight", "hedge_resid", "window_spread", "cond_gate")


def _tpl_ts_leg(rng, opset, leaves, exclude=None):
    """模板腿：单输入 ts 算子套随机叶子（exclude=(op,param,field) 避同款重掷）。"""
    ts_single = [n for n in opset["ops"].get("ts", {})
                 if n not in TWO_INPUT_TS and n not in THREE_INPUT_TS]
    humps = opset.get("humps") or DEFAULT_HUMPS
    name = rng.choice(ts_single)
    p = _sample_param(OPERATOR_REGISTRY["ts"][name][1], rng,
                      opset["windows"], opset["delays"], humps)
    leaf = rng.choice(leaves)
    if exclude is not None:
        for _ in range(5):
            if (name, p, leaf) != exclude:
                break
            name = rng.choice(ts_single)
            p = _sample_param(OPERATOR_REGISTRY["ts"][name][1], rng,
                              opset["windows"], opset["delays"], humps)
            leaf = rng.choice(leaves)
    return Node(name, "ts", p, [Node(leaf, "leaf")])


def _from_template(rng, opset, leaves, template):
    cs_names = list(opset["ops"].get("cs", {}).keys())
    if not cs_names:
        raise ValueError("模板要求至少 1 个 cs_* 算子")
    if template == "leg_weight":
        # 量腿加权（战役三度兑现）：mul(ts腿A, 截面权重腿B)
        A = _tpl_ts_leg(rng, opset, leaves)
        B = Node(rng.choice(cs_names), "cs", None, [Node(rng.choice(leaves), "leaf")])
        return Node("mul", "binary", None, [A, B])
    if template == "hedge_resid":
        # 残差对冲腿（七元腿战役的机械化内核）：y 对 x 滚动残差 + 截面收口
        if "ts_resid" not in opset["ops"].get("ts", {}):
            return _from_template(rng, opset, leaves, "leg_weight")
        f1 = rng.choice(leaves)
        rest = [l for l in leaves if l != f1] or leaves
        w = int(rng.choice(opset["windows"]))
        R = Node("ts_resid", "ts", w, [Node(f1, "leaf"), Node(rng.choice(rest), "leaf")])
        return Node(rng.choice(cs_names), "cs", None, [R])
    if template == "window_spread":
        # 窗口价差（triplewin 族的机械化内核）：同字段同算子异窗差 + 截面收口
        A = _tpl_ts_leg(rng, opset, leaves)
        pool = (opset["windows"]
                if OPERATOR_REGISTRY["ts"][A.op][1] == "w" else opset["delays"])
        w2 = A.param
        for _ in range(6):
            cand = int(rng.choice(pool))
            if cand != A.param:
                w2 = cand
                break
        B = Node(A.op, "ts", w2, [A.children[0]])
        return Node(rng.choice(cs_names), "cs", None,
                    [Node("sub", "binary", None, [A, B])])
    if template == "cond_gate":
        # 条件门（战役"门+腿"形态）：gt/lt 时序腿门控 + 双腿分支 + 截面收口
        cmp_names = list(opset["ops"].get("cmp", {}).keys())
        if not cmp_names:
            return _from_template(rng, opset, leaves, "window_spread")
        A = _tpl_ts_leg(rng, opset, leaves)
        B = _tpl_ts_leg(rng, opset, leaves,
                        exclude=(A.op, A.param, A.children[0].op))
        gate = Node(rng.choice(cmp_names), "cmp", None, [A, B])
        t = _tpl_ts_leg(rng, opset, leaves)
        f = _tpl_ts_leg(rng, opset, leaves)
        return Node(rng.choice(cs_names), "cs", None,
                    [Node("where", "cond", None, [gate, t, f])])
    raise ValueError(f"未知模板: {template}")


def generate_one(rng, opset, leaves=None):
    """一次生成（explore/null 共用入口）：按 template_share 掷模板或均匀树。

    返回 (tree, template_name|None)。null 校准与 explore 走同一通道——
    墙必须覆盖实际采样空间（模板与占比进 opset 指纹，改配置=须重校准）。
    """
    tpls = opset.get("templates_enabled") or []
    share = float(opset.get("template_share", DEFAULT_TEMPLATE_SHARE))
    if tpls and share > 0 and rng.random() < share:
        t = str(rng.choice(tpls))
        return generate_tree(rng, opset, leaves=leaves, template=t), t
    return generate_tree(rng, opset, leaves=leaves), None


def render_factor_source(tree: Node, name: str, template: str | None = None) -> tuple[str, list[str]]:
    """把组合树渲染成自包含的 factor(env) 源码。

    返回 (source, imports)。源码引用包内 ops 算子库（算子真值源，可配置）。
    函数名固定 factor（2026-08-18 生产审计修正）：下游 causality/evaluate/batch
    的契约都要求 def factor(env)——此前渲染 def random_factor_N(env)，agent 每
    轮冷启动必踩 5 次「缺少 factor(env)」ERR。tree id 移入注释保留可读性。
    template（2026-09-18）：模板出身的树在首行注释带 template 标记——出身
    诚实（结构先验非零先验），台账/审阅按此区分纯随机与模板产物。
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
    head = f"# tree: {name}"
    if template:
        head += f" | template: {template}"
    src = (
        f"{head} | expression: {tree.to_expression()}\n"
        f"def factor(env):\n"
        f"    import pandas as pd\n"
        f"    from dsh_factor_mining.factor.ops import {imports}\n"
        f"    return ({body}).values\n"
    )
    return src, sorted(set(used))


# ---------------------------------------------------------------------------
# 轻量 IC 扫描（阶段 1：只算 IC 序列，不跑 causality/column-perm/beta）
# ---------------------------------------------------------------------------
def spread_percentile(spread_ir, spread_segment: dict | None):
    """spread_ir 在 landscape spread 段的分位（0-100）。

    廉价查表：对持久化的分位 knots（p10/p25/p50/p75/p90/p95）做分段
    线性插值，不存原始样本。低于最低 knot → 该 knot 位；高于 p95 →
    封顶 100（分位查表不冒充尾部外推）。值/knots 不足 → None。"""
    if (not isinstance(spread_ir, (int, float)) or isinstance(spread_ir, bool)
            or not np.isfinite(spread_ir)):
        return None
    if not isinstance(spread_segment, dict):
        return None
    knots = []
    for pname in ("p10", "p25", "p50", "p75", "p90", "p95"):
        v = spread_segment.get(pname)
        if (isinstance(v, (int, float)) and not isinstance(v, bool)
                and np.isfinite(v)):
            knots.append((float(pname[1:]), float(v)))
    if len(knots) < 2:
        return None
    x = float(spread_ir)
    if x <= knots[0][1]:
        return round(knots[0][0], 1)
    if x >= knots[-1][1]:
        return 100.0
    for (q0, v0), (q1, v1) in zip(knots, knots[1:]):
        if v0 <= x <= v1:
            if v1 == v0:
                return round(q1, 1)
            return round(q0 + (q1 - q0) * (x - v0) / (v1 - v0), 1)
    return None


def light_ic_scan(F, env, spread_ref: dict | None = None):
    """轻量诊断：不重叠口径 IC 序列的 mean/ir/n（development 区）。

    与 evaluate 的 _cross_sectional_ic 同口径（sig_only + DEV_END 切分），
    但不含 column-perm / beta / topn / decay 等重计算。

    2026-08-27 规划书 WS-C：追加尾部线 spread_ir（与 null 校准 spread
    段同口径：spread_ir_statistic(F, fwd, pit, step, t_end=train 边界)）
    ——尾部强、IC 平庸的树不再永远浮不上来。spread_ref（landscape
    spread 段分位 dict；指纹门由调用方把关）在场时附 spread_pct
    （spread_percentile 查表插值）。函数名与既有返回键兼容
    （ic_mean/ic_ir/n 不变，新增键可缺省）。"""
    from .evaluate import _cross_sectional_ic, _forward_returns, _pit_mask
    from .tail import spread_turn_stats
    import numpy as _np

    F = _np.asarray(F, dtype=_np.float64)
    if F.shape != (env.T, env.N):
        raise ValueError(f"factor 形状 {F.shape} != {(env.T, env.N)}")
    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    out = {"ic_mean": None, "ic_ir": None, "n": 0}
    ic = _cross_sectional_ic(F, fwd, pit, env, sig_only=True)
    dev_end = env.calibration.dev_end
    if dev_end is None:
        # 无显式分界（直调 API）：null 校准保守用前 60% 样本——
        # 既不被三区卡死，也不把 selection/test 段纳入难度基线。
        dev_end = str(pd.DatetimeIndex(env.dates)[int(env.T * 0.6)].date())
    ic = ic[ic.index < pd.Timestamp(dev_end)]
    out["n"] = len(ic)
    if len(ic) >= 2:
        mean, std = float(ic.mean()), float(ic.std(ddof=1))
        out["ic_mean"] = mean
        out["ic_ir"] = mean / std if std > 0 else None
    # 换手定价（2026-08-28 WS-T3）：一次遍历同出 spread_ir + 换手/c*/
    # net——explore 的成本平局裁决列表（top_net 按 c* 排序）用 c*
    try:
        sts = spread_turn_stats(F, fwd, pit, env.calibration.sample_step,
                                k_frac=env.calibration.tail_k,
                                t_end=_train_end_of(env),
                                cost=float(env.calibration.cost or 0.0))
    except Exception:
        sts = {}
    out["spread_ir"] = sts.get("spread_ir")
    out["spread_pct"] = spread_percentile(out["spread_ir"], spread_ref)
    for _k in ("turn_tail", "break_even_cost", "net_spread_ir"):
        if sts.get(_k) is not None:
            out[_k] = sts.get(_k)
    # P0a(2026-09-13 反同质化奖励):explore 选种的 novelty 项需要候选的
    # 逐采样日 IC 向量(组内相关用)。只在序列可用时携带,轻量(round 4)
    try:
        out["ic_series"] = [round(float(x), 4) + 0.0 for x in ic.tolist()]
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# null 地形（持久化 + 查询）
# ---------------------------------------------------------------------------
def _null_landscape_path(state_root):
    return os.path.join(state_root, "null_landscape.json")


def _bucket_corr(s1, s2):
    """跨 horizon IC 序列相关（v2 null 校准）：位置分桶对齐后 Pearson。

    序列长度 n ≈ train_days/h 不同（h=5 → 152 点 vs h=20 → 38 点）；两条序列
    都按非重叠步长升序采样——把点多的按点少的网格分桶取均值，近似同一时间
    尺度后算相关。粗但对（作 ρ_h 先验足够，缺先验的下游按独立保守计）。
    """
    a = np.asarray(s1, dtype=np.float64)
    b = np.asarray(s2, dtype=np.float64)
    if len(a) < 2 or len(b) < 2:
        return None
    if len(a) != len(b):
        if len(a) < len(b):
            a, b = b, a
        k = len(b)
        idx = np.linspace(0, len(a), k + 1).astype(int)
        a = np.array([a[idx[j]:idx[j + 1]].mean() if idx[j] < idx[j + 1] else a[idx[j]]
                      for j in range(k)])
    if a.std() == 0 or b.std() == 0:
        return None
    c = float(np.corrcoef(a, b)[0, 1])
    return c if np.isfinite(c) else None


def _train_end_date(v) -> str:
    """train 区分界日期（v.calibration.dev_end None → 前 60% 回退）——
    IC 分位段与 spread 段（WS1 2026-08-25）共用同一边界函数，
    两段 null 的区域口径不得漂移。"""
    dev_end = v.calibration.dev_end
    if dev_end is None:
        # 无显式分界（直调 API）：null 校准保守用前 60% 样本——
        # 既不被三区卡死，也不把 selection/test 段纳入难度基线。
        dev_end = str(pd.DatetimeIndex(v.dates)[int(v.T * 0.6)].date())
    return dev_end


def _train_end_of(v) -> int:
    """train 区行边界（与 tail._train_end 同式：searchsorted dates）。"""
    return int(np.searchsorted(v.dates, pd.Timestamp(_train_end_date(v))))


def run_null_calibration(env, state_root, n=50, seed=42, opset=None, on_progress=None,
                         env_fingerprint=None, horizons=None,
                         cost_model_version=None, spread_cost=None):
    """null 地形：n 个随机因子的 IC_IR 经验分布，持久化。

    返回 dict：分位数 + 元信息。后续因子诊断可引用
    "相对随机基线 p95 的分位"（bridge 的 factor.null_landscape 查询）。
    on_progress(done, total)：进度回调（长任务防静默，批次1a）。

    env_fingerprint（2026-08-19 指纹硬门）：调用方（bridge）传入的环境三元组
    指纹（数据文件+口径+引擎版本）。evaluate 读地形做 pool_std 估计时校验
    指纹——不匹配视为无效（换数据集后旧地形不得继续当基线）。旧版文件无
    此字段同样判不匹配（宁可保守：重跑一次校准，几分钟）。

    horizons（v2 2026-08-20 申报制菜单）：菜单内每个 horizon 各建一份
    per-horizon IC_IR 分位（pool_std 按 horizon 取基线——短 horizon 的 null
    天然更窄，防止「短 horizon n 大出小 p」的系统性诱惑白嫖）。同时实测
    跨 horizon IC 序列相关（cross_horizon_corr）——同因子扫 horizon 的族
    结构先验（N_eff 谱方法用它连接 (hash, h1)/(hash, h2) 试验）。成本 ×菜单
    宽度：n=50 × 4 horizon ≈ 单 horizon n=200 的量级。

    spread 段（WS1 2026-08-25 任务书）：同批树 × horizon 视图追加 train 区
    spread_ir（与 tail_metrics 同口径：行集 arange(0, t_end, sample_step)、
    K = round(0.2·n_day)）→ per-horizon spread_ir 经验分布。尾部线 G3 的
    s0 从解析式 1/√n_days 升级为该分布的 std（bridge._landscape_tail_s0）。
    不另跑一批树——同批树保证 spread null 与 IC null 同条件。

    cost_model_version / spread_cost（2026-08-28 换手率定价）：版本戳进
    landscape（net 段与毛段的地形互不可比，文件级区分）；spread_cost
    给定时 spread 段按 2·cost·turn 净掉（随机树同样付成本——null 左移，
    WS1 net 重校的开关；v1 默认 None = 毛口径照旧）。
    """
    from .evaluate import (_cross_sectional_ic, _env_horizon_view,
                           _forward_returns, _pit_mask)
    from .tail import spread_ir_statistic
    opset = opset or effective_operator_set(state_root)
    menu = [int(h) for h in horizons] if horizons else [env.calibration.horizon]
    leaves = env_leaves(env)
    rng = np.random.default_rng(seed)
    views = {h: _env_horizon_view(env, h) for h in menu}
    irs_by_h = {h: [] for h in menu}
    series_by_h = {h: [] for h in menu}
    spread_by_h = {h: [] for h in menu}
    n_pathological = 0
    for _i in range(n):
        if on_progress is not None:
            try:
                on_progress(_i, n)
            except Exception:
                pass
        # 模板同通道采样（2026-09-18）：null 必须覆盖实际采样空间——
        # explore 走 generate_one（含模板占比），null 校准同款
        tree, _tpl = generate_one(rng, opset, leaves=leaves)
        # 内联执行：直接调 ops 算子（不经源码编译，同数学）。
        # asarray 必须有：_eval_tree 返回 DataFrame（symbol 索引），裸传
        # _cross_sectional_ic 会在 pit[t] & isfinite(F[t]) 的 Series/ndarray
        # 对齐处静默炸掉（原 light_ic_scan 同样先 asarray）
        try:
            F = np.asarray(_eval_tree(tree, env), dtype=np.float64)
        except Exception:
            continue
        # 健康门（2026-09-18 缝2b）：inf/爆炸/退化树不进 null 样本——
        # 此前被 IC mask 静默吃掉（样本悄悄变少），现在显式计数
        ph = matrix_health(F)
        if ph is not None:
            n_pathological += 1
            continue
        for h in menu:
            try:
                v = views[h]
                fwd = _forward_returns(v)
                pit = _pit_mask(v)
                ic = _cross_sectional_ic(F, fwd, pit, v, sig_only=True)
                ic = ic[ic.index < pd.Timestamp(_train_end_date(v))]
                if len(ic) >= 2 and ic.std(ddof=1) > 0:
                    ir = float(ic.mean() / ic.std(ddof=1))
                    if np.isfinite(ir):
                        irs_by_h[h].append(ir)
                        series_by_h[h].append(ic.values)
                # spread null：与 IC 段同批树、同视图、同 train 边界；
                # spread_cost 给定时 = net 段（随机树同样付成本）
                sp_ir = spread_ir_statistic(F, fwd, pit,
                                            v.calibration.sample_step,
                                            k_frac=v.calibration.tail_k,
                                            t_end=_train_end_of(v),
                                            cost=spread_cost)
                if sp_ir is not None and np.isfinite(sp_ir):
                    spread_by_h[h].append(sp_ir)
            except Exception:
                continue

    def _q(arr, p):
        return float(np.nanpercentile(arr, p)) if len(arr) else None

    ic_ir_out = {}
    for h in menu:
        arr = np.array(irs_by_h[h]) if irs_by_h[h] else np.array([np.nan])
        ic_ir_out[str(h)] = {
            "p10": _q(arr, 10), "p25": _q(arr, 25), "p50": _q(arr, 50),
            "p75": _q(arr, 75), "p90": _q(arr, 90), "p95": _q(arr, 95),
            "p99": _q(arr, 99),
            "max": float(np.nanmax(arr)) if len(arr) else None,
            "mean_abs": float(np.nanmean(np.abs(arr))) if len(arr) else None,
        }

    # spread 段（WS1）：与 ic_ir per-horizon 结构同构，std 是 G3 的
    # s0_emp（bridge._landscape_tail_s0 读）——同文件同 env_fingerprint，
    # 指纹绑定自动生效
    def _z(x):
        # -0.0 归一（IEEE: -0.0+0.0=+0.0）——lossless JSON 检查拒 -0
        return None if x is None else float(x) + 0.0

    spread_out = {}
    for h in menu:
        k = len(spread_by_h[h])
        arr = np.array(spread_by_h[h]) if k else np.array([np.nan])
        spread_out[str(h)] = {
            "p10": _z(_q(arr, 10)), "p50": _z(_q(arr, 50)),
            "p90": _z(_q(arr, 90)), "p95": _z(_q(arr, 95)),
            "std": _z(float(np.std(arr, ddof=1))) if k >= 2 else None,
            "mean_abs": _z(float(np.mean(np.abs(arr)))) if k else None,
            "n": int(k),
        }

    # 跨 horizon 相关：同批随机因子在 (h1, h2) 的 IC 序列相关，跨因子平均
    cross_h = {}
    for i, h1 in enumerate(menu):
        for h2 in menu[i + 1:]:
            rhos = []
            for s1, s2 in zip(series_by_h[h1], series_by_h[h2]):
                c = _bucket_corr(s1, s2)
                if c is not None and np.isfinite(c):
                    rhos.append(c)
            if rhos:
                cross_h[f"{min(h1, h2)}|{max(h1, h2)}"] = round(float(np.mean(rhos)), 4)

    # 生产可见性（2026-08-20）：某 horizon 全空 = 该赌注无基线（下游 pool_std
    # None → D7 拒绝），静默 0-valid 是审计盲区——必须 stderr 可见
    for h in menu:
        if len(irs_by_h[h]) == 0 or len(spread_by_h[h]) == 0:
            try:
                import sys as _sys
                if len(irs_by_h[h]) == 0:
                    _sys.stderr.write(
                        f"[null-calibration] horizon={h} 无有效随机因子样本（全部"
                        "生成/评估失败）——该 horizon 的 pool_std 基线缺失\n")
                if len(spread_by_h[h]) == 0:
                    _sys.stderr.write(
                        f"[null-calibration] horizon={h} spread 段无有效样本——"
                        "该 horizon 的尾部 s0_emp 基线缺失（G3 降级解析式）\n")
            except Exception:
                pass

    result = {
        "n_generated": n, "n_valid": int(len(irs_by_h[menu[0]])), "seed": seed,
        "n_pathological": int(n_pathological),
        "env_fingerprint": env_fingerprint,
        "horizons": menu,
        "ic_ir": ic_ir_out,
        "spread": spread_out,
        "cross_horizon_corr": cross_h,
        # 换手定价（2026-08-28）：成本口径戳——net 段与毛段的地形互不
        # 可比；_landscape_tail_s0 的指纹门只认同版本
        "cost_model_version": cost_model_version,
        "spread_cost": spread_cost,
        "interpretation": (
            "经验 null 分布（per-horizon）：随机因子的 IC_IR 集中在 p50 附近。"
            "新因子 IC_IR 超过对应 horizon 的 p95 才值得认真对待；p99 以上是强信号。"
            "短 horizon 的 null 天然更窄（n 大）——显著性更容易是其样本量大的正当结果，"
            "不是作弊；但成本换手同框看（cost_bps 对短 horizon 惩罚更重）。"
        ),
        "sampling_note": (
            f"n={n} × {len(menu)} horizon：p95 的估计误差约 ±10 个百分位，p99 基于"
            "不足 1 个期望观测、只能当方向参考——需要精确尾部分位时用 n>=200 重跑。"
            "cross_horizon_corr 是同因子跨 horizon 的族结构先验（N_eff 谱方法用）。"
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
    for cat in ("ts", "cs", "binary", "unary", "cmp", "cond"):
        if node.op in OPERATOR_REGISTRY.get(cat, {}):
            fn = OPERATOR_REGISTRY[cat][node.op][0]
            break
    if fn is None:
        raise ValueError(f"未知算子: {node.op}")
    kids = [_eval_tree(ch, env) for ch in node.children]
    if node.param is None:
        return fn(*kids)
    return fn(*kids, node.param)
