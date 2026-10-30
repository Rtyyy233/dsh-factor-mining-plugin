# coding=utf-8
"""逐腿归因产品化（调度器票 2a，PLAN-scheduler-task-brief-2026-09-29 §五）。

把 analysis/glm_mining/ 下的人工拆杠杆脚本（batch_rangeamt.py /
bisect_onion.py / d4_dissect.py / onion_quint5.py）产品化进引擎：
多腿因子按需拆解各腿贡献 Δ_k = Q(f) − Q(f 去掉第 k 腿)，供调度器
U 项（U_legs）与模型报表消费。

工程纪律（任务书 §5.5 遥测成本控制）：
- 按需计算：只在状态被调度器打分时算（调用方预算 max_evals 控制）
- 表达式哈希缓存：归因结果内容寻址落 attribution_cache.jsonl
- 走现有异步槽位/评估缓存：q_fn 由调用方注入（桥内封装 evaluate +
  eval_cache，dedup 层直接吸收大量 ablation 重叠）——本模块零新通道
- 拆腿变换是确定性字符串变换（AST 定位 + 行替换）→ 注册进
  derivation._REPLAY（不变量 I2 可重放基础）

多腿形态识别（生产源码实证形态，day13_quint5 等）：
    s1 = <expr>            # 腿：简单赋值，被 return 直接引用
    s2 = <expr>
    return ((s1 + s2 + ...) / N).values
非该形态（单表达式因子/腿间互引）诚实返回 None——不硬拆。
"""
from __future__ import annotations

import ast
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

from ..derivation import register_replay

CACHE_FILE = "attribution_cache.jsonl"


# ---------------------------------------------------------------- 形态识别

def _factor_body(source: str) -> ast.FunctionDef | None:
    """找 def factor(env): 函数体（引擎源码契约）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "factor":
            return node
    return None


def _simple_assigns(fn: ast.FunctionDef) -> dict[str, ast.Assign]:
    """函数体内简单名赋值（Name target）——腿与中间量的候选池。"""
    out: dict[str, ast.Assign] = {}
    for stmt in fn.body:
        if (isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)):
            out[stmt.targets[0].id] = stmt
    return out


def _return_expr(fn: ast.FunctionDef) -> ast.Expr | None:
    for stmt in fn.body:
        if isinstance(stmt, ast.Return):
            return stmt.value
    return None


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def extract_legs(source: str) -> list[str] | None:
    """识别多腿形态：return 表达式直接引用 ≥2 个简单赋值名 → 腿列表。

    腿定义 = 被 return 合成表达式直接引用的简单赋值变量（s1..sN 或
    任意名）；中间量（如 rng）不引用进合成式的不是腿。"""
    fn = _factor_body(source)
    if fn is None:
        return None
    ret = _return_expr(fn)
    if ret is None:
        return None
    assigns = _simple_assigns(fn)
    used = _names_used(ret)
    legs = [name for name in assigns if name in used]
    if len(legs) < 2:
        return None
    # 腿赋值表达式不得引用其它腿（互引形态不拆——语义不明确）
    for name in legs:
        if _names_used(assigns[name].value) & set(legs):
            return None
    return sorted(legs, key=lambda n: assigns[n].lineno)


# ---------------------------------------------------------------- 拆腿变换

def zero_leg_source(source: str, leg: str) -> str | None:
    """去掉第 k 腿 = 腿赋值行替换为 `<leg> = 0.0`（确定性字符串变换）。

    线性合成器下等价于"移除该腿贡献"；（Σ/N）形态的重归一版本
    （除数 N→N−1）不采用——置零在非线性合成器下同样良定义，且变换
    与合成器形态解耦（I2 重放只依赖这一条规则）。"""
    fn = _factor_body(source)
    if fn is None:
        return None
    assigns = _simple_assigns(fn)
    if leg not in assigns:
        return None
    lineno = assigns[leg].lineno
    lines = source.splitlines(keepends=True)
    if not (1 <= lineno <= len(lines)):
        return None
    lines[lineno - 1] = f"{leg} = 0.0\n"
    return "".join(lines)


# 注册为可重放 op（derivation 不变量 I2 的机械基础）：注册表约定
# fn(parent_source, op_params) -> child_source——适配 zero_leg 的 (source, leg)
register_replay("drop_leg",
                lambda source, params: zero_leg_source(
                    source, str((params or {}).get("leg", ""))))


def renormalized_drop_source(source: str, leg: str) -> str | None:
    """等权重归一 drop（仅 (Σ legs)/N 形态）：合成式去掉该腿、N→N−1。

    供模型做"等权重组"类变体参考；调度器归因用 zero 口径（Δ_k 语义
    = 该腿的贡献，而非重组后的质量）。"""
    fn = _factor_body(source)
    if fn is None:
        return None
    ret = _return_expr(fn)
    if ret is None:
        return None
    if isinstance(ret, ast.Attribute) and ret.attr == "values":
        ret = ret.value  # 剥 `.values` 外壳（生产源码形态）
    legs = extract_legs(source)
    if not legs or leg not in legs or len(legs) < 3:
        return None  # 两腿 drop 一步到位用 zero；重归一留给 ≥3 腿
    if not isinstance(ret, ast.BinOp) or not isinstance(ret.op, ast.Div):
        return None
    num, den = ret.left, ret.right
    if not (isinstance(den, ast.Constant) and isinstance(den.value, (int, float))):
        return None
    # 收集分子里的腿引用节点并摘除目标腿
    names_in_num = _names_used(num)
    if set(legs) != names_in_num:
        return None  # 分子不是纯腿和——退回 None（调用方用 zero）
    new_source = zero_leg_source(source, leg)
    if new_source is None:
        return None
    lines = new_source.splitlines(keepends=True)
    # return 行的除数替换：N → N−1（同语法位置，字符串级替换最末除数）
    ret_line = lines[fn.body[-1].lineno - 1] if isinstance(
        fn.body[-1], ast.Return) else None
    if ret_line is None:
        return None
    old_den = f"/ {den.value}"
    new_den = f"/ {len(legs) - 1}.0"
    if old_den not in ret_line:
        old_den = f"/{den.value}"
        new_den = f"/{len(legs) - 1}.0"
    if old_den not in ret_line:
        return None
    lines[fn.body[-1].lineno - 1] = ret_line.replace(old_den, new_den, 1)
    return "".join(lines)


# ---------------------------------------------------------------- 归因报告

def leg_attribution(source: str, q_fn: Callable[[str], float | None],
                    legs: list[str] | None = None) -> dict | None:
    """逐腿归因：Δ_k = Q(f) − Q(f\\k)，p_k = |Δ_k|/Σ|Δ_j|，熵 H_attr。

    q_fn(source) → Q 值（调度器口径 = Φ⁻¹(1−p_def)；测试可注入任意
    确定性函数）。base 或任一腿 Q 为 None → 诚实返回部分报告
    {"incomplete": True}——绝不编数。"""
    legs = legs if legs is not None else extract_legs(source)
    if not legs:
        return None
    base = q_fn(source)
    if base is None:
        return {"legs": legs, "incomplete": True, "reason": "base Q 不可算"}
    deltas: dict[str, float] = {}
    for leg in legs:
        zsrc = zero_leg_source(source, leg)
        if zsrc is None:
            continue
        qk = q_fn(zsrc)
        if qk is None:
            continue
        deltas[leg] = float(base) - float(qk)
    if not deltas:
        return {"legs": legs, "base_Q": float(base), "incomplete": True,
                "reason": "全部 ablation Q 不可算"}
    total = sum(abs(v) for v in deltas.values()) or 1e-12
    p = {k: abs(v) / total for k, v in deltas.items()}
    h_attr = -sum(pk * math.log(pk) for pk in p.values() if pk > 0)
    return {
        "legs": legs,
        "base_Q": float(base),
        "deltas": deltas,
        "p": p,
        # 单腿扛旗度（1=完全集中 0=完全分散）：H_attr 低 → 扩展方向明确
        "dominance": 1.0 - (h_attr / math.log(max(len(deltas), 2))
                            if len(deltas) > 1 else 0.0),
        "H_attr": h_attr,
        "n_evals": 1 + len(deltas),
        "incomplete": len(deltas) < len(legs),
    }


# ---------------------------------------------------------------- 结果缓存

def _cache_path(root) -> Path:
    return Path(root) / CACHE_FILE


def cache_read(root, source_hash: str) -> dict | None:
    """内容寻址读缓存（source_hash → 最近一份归因报告）。"""
    path = _cache_path(root)
    if not path.exists():
        return None
    hit = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if rec.get("source_hash") == source_hash:
                    hit = rec  # 尾行（最新）胜
    except OSError:
        return None
    return (hit or {}).get("report")


def cache_write(root, source_hash: str, report: dict) -> None:
    """追加式写缓存（锁内；jsonl 只增不删，读端取最新）。"""
    from ..filelock import state_write_lock
    root = Path(root)
    with state_write_lock(root):
        with open(_cache_path(root), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source_hash": source_hash, "report": report,
            }, ensure_ascii=False, default=str) + "\n")


def cached_attribution(root, source: str, source_hash: str,
                       q_fn: Callable[[str], float | None]) -> tuple[dict, bool]:
    """缓存包裹的归因入口：命中直接回，miss 才算（并回填缓存）。

    返回 (report, cache_hit)——报表的缓存命中率即两者计数之比。"""
    hit = cache_read(root, source_hash)
    if hit is not None:
        return hit, True
    report = leg_attribution(source, q_fn)
    if report is None:
        report = {"incomplete": True, "reason": "非多腿形态"}
    cache_write(root, source_hash, report)
    return report, False
