# coding=utf-8
"""策略契约（signal-only，2026-08-27 规划 S1 拍板）。

agent 只声明 what——``fit(env)→state``（可选，ML）与
``apply(state, env)→weights``（必有）；测量归 harness：模拟器/成交/
费用独占（simulator.py），fit/apply 的日程（walk-forward 折边界、
env 物理截断）归 harness（env_adapter.py）。泄漏防护结构性成立：
apply 拿到的 env 一律物理截断，未来数据不在参数里。

weights 语义（截面目标持仓）：
- apply 返回**长度 == env.T 的 list**（与 env.dates 对齐）；第 t 项 =
  收盘 t 的目标组合 ``{symbol: weight}``，在 t+1 开盘执行（模拟器
  固定，非 agent 可选项）。env 截到 t 与截到 t+k 各跑一次、前 t 段
  逐位一致（audit 锁 3）——返回单截面 dict 无法做该审计，是契约违规。
- 权重不必和为 1：余量 = 现金腿（按现金收益计息）。v1 禁裸空与
  杠杆（w ≥ 0、Σw ≤ 1——模拟器 fail-closed；二档 realism 再放开）。
- 持仓持续是策略自己的事：不复述上一目标 = 减仓/清仓指令。

确定性是硬要求（截断不变性审计的前提）：seed 由 harness 注入
（inject_seed 重播种 random/numpy 全局 RNG 后再执行 fit/apply）。
自带独立种子的 np.random.Generator 无法被重播种覆盖——audit 的
同 env 双跑比对会抓出非确定实现（与因子层 causality 检测同一
覆盖边界，见 causality.check_causality note）。

fit 每折只跑一次（训练是超时高发区，单独计时预算——worker 侧）。
"""
from __future__ import annotations

import math
import random

import numpy as np


class StrategyError(ValueError):
    """契约违规（fail-closed）：错误信息必须可行动——指明哪一项、
    第几个 bar/什么值、怎么修（沿因子层四要素错误纪律）。"""


def inject_seed(seed: int) -> None:
    """harness 注入种子：重播种 random / numpy 全局 RNG。

    策略里的随机性必须从全局 RNG 取（np.random.* / random.*）；
    自建 default_rng(seed) 的独立流 harness 无法覆盖——确定性由
    audit 双跑比对兜底。"""
    random.seed(seed)
    np.random.seed(seed)


def compile_strategy(source: str) -> dict:
    """策略源字符串 → 已校验命名空间（agent 侧从不执行策略代码；
    本函数只在 harness worker / harness 进程内调用）。"""
    if not isinstance(source, str) or not source.strip():
        raise StrategyError("策略源为空——需要定义 apply(state, env) 的 Python 源码")
    ns = {"__name__": "dsh_strategy_lab_strategy"}
    exec(compile(source, "<strategy_source>", "exec"), ns)  # noqa: S102 — 契约要求：策略代码只在 harness 沙箱执行
    check_namespace(ns)
    return ns


def check_namespace(ns) -> None:
    """命名空间形状校验：apply 必有；fit 可选；其余不约束（模型类中立：
    线性/树/NN/规则在 harness 眼里都是 fit/apply）。"""
    if not isinstance(ns, dict):
        raise StrategyError(f"策略命名空间应为 dict，得到 {type(ns).__name__}")
    apply_fn = ns.get("apply")
    if not callable(apply_fn):
        raise StrategyError(
            "策略源缺少可调用的 apply(state, env)——函数名必须是 apply；"
            "fit(env) 可选（ML 才需要）")
    fit_fn = ns.get("fit")
    if fit_fn is not None and not callable(fit_fn):
        raise StrategyError("fit 已定义但不可调用——fit(env) 必须是函数")


def run_fit(ns: dict, env, seed: int):
    """harness 侧 fit 入口：注入种子后执行；fit 拿到的 env 由调用方
    （env_adapter.walk_forward_schedule）物理截断。"""
    fit_fn = ns.get("fit")
    if fit_fn is None:
        return None
    inject_seed(seed)
    return fit_fn(env)


def validate_weight_path(path, env, name: str = "apply") -> list[dict]:
    """apply 输出的形状/类型/有限性校验（fail-closed）。

    返回规范化后的 list[dict[str, float]]。违规信息带 bar 序号与
    日期定位，可行动。"""
    if not isinstance(path, (list, tuple)):
        raise StrategyError(
            f"{name} 必须返回长度 == env.T（={env.T}）的 list（与 env.dates "
            f"对齐、每项为 {{symbol: weight}} 的收盘目标组合），"
            f"得到 {type(path).__name__}——单截面 dict 无法做截断不变性审计")
    if len(path) != env.T:
        raise StrategyError(
            f"{name} 返回长度 {len(path)} != env.T（={env.T}）——权重路径"
            "必须逐 bar 对齐 env.dates（第 t 项 = 收盘 t 的目标组合）")
    symset = set(env.symbols)
    out = []
    for t, w in enumerate(path):
        date = env.dates[t]
        if not isinstance(w, dict):
            raise StrategyError(
                f"{name} 第 {t} 项（{date}）应为 dict，得到 {type(w).__name__}")
        row = {}
        for sym, val in w.items():
            if sym not in symset:
                raise StrategyError(
                    f"{name} 第 {t} 项（{date}）含未知资产 {sym!r}——"
                    f"必须是 env.symbols 中的符号（共 {len(symset)} 个）")
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise StrategyError(
                    f"{name} 第 {t} 项（{date}）资产 {sym} 权重类型 "
                    f"{type(val).__name__}——必须是有限 float")
            v = float(val)
            if not math.isfinite(v):
                raise StrategyError(
                    f"{name} 第 {t} 项（{date}）资产 {sym} 权重非有限"
                    f"（{val!r}）——NaN/inf 权重不可执行")
            row[sym] = v
        out.append(row)
    return out


def run_apply(ns: dict, state, env, seed: int) -> list[dict]:
    """harness 侧 apply 入口：注入种子 → 执行 → 形状校验。"""
    inject_seed(seed)
    w = ns["apply"](state, env)
    return validate_weight_path(w, env)
