# coding=utf-8
"""参数平坦性门（2026-08-25 用户决策：真实结构时序置换之外的第二个
过拟合缺口——调参刀锋）。

设计要点（实现前推演钉死）：

1. **申报制**：agent 在 registry.submit 申报参数表 [{name, value, step}]，
   引擎校验 value 真实出现在 source 数值字面量中（漏报/错报 = 事务
   中止拒收）。不做自动 AST 全量扰动——年化 252、数组索引、结构常数
   会被误动。申报制边界：无通道强制申报，无申报则跳过（SKILL 纪律
   层要求申报可调参数；trail 审计抽查）。
2. **最小步长邻域，只打悬崖不打衰减**：真实 alpha 在参数空间是平滑
   峰（动量 horizon 谱系的结构在大步长——RSI14 vs mom60 差异巨大
   是真实的；最小步长 ±step 不该有断崖）。断崖只可能来自调参贴噪声。
   悬崖签名 = 任一邻居：翻号（ic_ir × center < 0）/ 塌到中心 50%
   以下 / 中心显著而邻居退化（ic_ir 不可计算）。
3. **submit 时引擎自主执行，agent 不能预收割**：无独立 flatness 工具
   通道——否则「平坦性诊断」变成免费多点采样调参。邻域结果全部随
   registry 条目落盘（含拒绝条目），不进 trail_engine 主账本——
   确定性扰动非选择试验，不计入 deflation N（理由：变体由申报表
   唯一决定，agent 无选择自由度；提交行为本身已在主账本计价）。
4. **train 区计算**：IC_IR 只在 dates < dev_end 上算——test 区是
   消耗品，诊断也不得触碰。
5. **硬门已启用**（Phase 6 校准 2026-08-25：registry 19 个已入册因子
   悬崖签名 0/19 假阳性——安全启用；检测功效待证伪 preset 补证）。
   bridge submit 侧执行拒收，本模块仍返回完整多维诊断。

替换语义：replace_numeric_literal 替换 source 中**所有**数值等于
value 的字面量（两处同值窗口 = 联合扰动，保守方向）。
"""
from __future__ import annotations

import ast

import numpy as np

from .env import FactorEnv


def replace_numeric_literal(source: str, old, new) -> str | None:
    """AST 常量替换：所有数值 == old 的字面量替换为 new 并 unparse；
    无匹配 → None（调用方据此判定申报值不在 source）。"""
    tree = ast.parse(source)
    found = False

    class _V(ast.NodeTransformer):
        def visit_Constant(self, node):
            nonlocal found
            v = node.value
            if (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and v == old):
                found = True
                return ast.copy_location(ast.Constant(value=new), node)
            return node

    new_tree = _V().visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree) if found else None


def literal_present(source: str, value) -> bool:
    """申报值是否真实出现在 source 数值字面量中（old==new 探测替换）。"""
    try:
        return replace_numeric_literal(source, value, value) is not None
    except SyntaxError:
        return False


def train_ic_ir(F: np.ndarray, env: FactorEnv) -> float | None:
    """train 区（dates < dev_end）rank IC 序列 → IC_IR。

    与 evaluate ic_ir_train 同区口径（采样日 + PIT + 双方 finite），
    统计量用 fast_rank_ic（向量化，与噪声门同语义）；中心与全部邻居
    用同一内部统计量——平坦性是变体间比较，内部一致即公平。"""
    import pandas as pd

    from .evaluate import _forward_returns, _pit_mask
    from .noise import fast_rank_ic

    fwd = _forward_returns(env)
    pit = _pit_mask(env)
    t_end = int(np.searchsorted(env.dates,
                                pd.Timestamp(env.calibration.dev_end)))
    ic = fast_rank_ic(F[:t_end], fwd[:t_end], pit[:t_end],
                      env.calibration.sample_step)
    if len(ic) < 2:
        return None
    s = ic.std(ddof=1)
    return float(ic.mean() / s) if s > 0 else None


def flatness_test(source: str, env: FactorEnv, decl: list,
                  compile_fn, budget_secs: float = 90.0,
                  collapse_ratio: float = 0.5) -> dict:
    """申报参数表的最小步长邻域评估（见模块 docstring；submit 时引擎
    自主调用，无独立工具通道）。

    返回 center_ic_ir（引擎重算，非 diagnosis 回传——同管线内部一致）
    + 每邻居一行（ic_ir / cliff 签名 / error / skipped）+ cliff 总标。
    预算自适应：超 budget_secs 的邻居记 skipped 不评估（诚实报告覆盖
    缺口，不静默假装平坦）。"""
    import time as _time

    t0 = _time.monotonic()
    F0 = compile_fn(source)(env)
    center = train_ic_ir(F0, env)
    rows = []
    cliff = False
    n_skipped = 0
    for p in decl:
        for sgn, v in (("-", p["value"] - p["step"]),
                       ("+", p["value"] + p["step"])):
            if _time.monotonic() - t0 > budget_secs:
                n_skipped += 1
                rows.append({"param": p["name"], "dir": sgn,
                             "neighbor": v, "skipped": "budget"})
                continue
            vs = replace_numeric_literal(source, p["value"], v)
            if vs is None:
                # bridge 侧已校验 presence；此处消失 = 防御性悬崖
                rows.append({"param": p["name"], "dir": sgn, "neighbor": v,
                             "error": "literal-not-found"})
                cliff = True
                continue
            try:
                Fv = compile_fn(vs)(env)
                ir = train_ic_ir(Fv, env)
            except Exception as e:
                rows.append({"param": p["name"], "dir": sgn, "neighbor": v,
                             "error": f"{type(e).__name__}: {e}"[:120]})
                continue
            row = {"param": p["name"], "value": p["value"], "dir": sgn,
                   "neighbor": v,
                   "ic_ir_train": None if ir is None
                   else round(float(ir), 4) + 0.0}
            if isinstance(center, (int, float)) and center != 0:
                if ir is None:
                    if abs(center) > 0.2:
                        row["cliff"] = "degenerate"
                        cliff = True
                elif ir * center < 0:
                    row["cliff"] = "sign_flip"
                    cliff = True
                elif abs(ir) < collapse_ratio * abs(center):
                    row["cliff"] = f"collapse_{int(collapse_ratio * 100)}pct"
                    cliff = True
            rows.append(row)
    out = {
        "n_params": len(decl),
        "center_ic_ir": None if center is None
        else round(float(center), 4) + 0.0,
        "collapse_ratio": collapse_ratio,
        "neighbors": rows,
        "cliff": cliff,
        "note": ("cliff 仅标注不拒收（report-only）——门方向与阈值由 "
                 "Phase 6 校准（registry × 证伪 preset 混淆矩阵）后启用"),
    }
    if n_skipped:
        out["note"] += (f"；{n_skipped} 个邻居因预算跳过（覆盖不完整，"
                        "不据此判平坦）")
    return out
