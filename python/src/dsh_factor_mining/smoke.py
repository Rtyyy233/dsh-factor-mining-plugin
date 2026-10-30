# coding=utf-8
"""烟测门（smoke gate）核心数学与判定——2026-09-01 效率编译层。

背景：挖掘 agent 习惯先写嵌套循环因子，check_causality（8x factor(env)）
烧满 worker 墙钟 640s 才被发现（2026-08-31 生产三连事故，每轮 10.7 分钟）。
本模块用小子面板实测外推全量成本，把反馈从 640s 压到秒级。

外推用双尺寸实测拟合缩放指数 alpha 而非线性假设——spike 实证（
tmp/numba-spike/spike-report.md）：线性外推会把 O(T^2*N) 排序怪放行成
"118s"（真实小时级），alpha 拟合（实测 1.61）才拦得住；alpha 在亚毫秒
计时上噪声大，worker 侧重复取 min 缓解。njit 自动编译与 LLM 重写层
共用本模块的子采样面板与严格等价口径（allclose rtol 1e-6）。
"""
from __future__ import annotations

import math
import re
from collections import namedtuple

# ---- 尺寸与预算常量（模块级，测试可 monkeypatch——NOISE_BUDGET_SECS 惯例）----
SMOKE_ROWS = 512
SMOKE_COLS = 128
SMOKE_SMALL_ROWS = 256
SMOKE_SMALL_COLS = 64
SMOKE_MIN_CELLS = 200_000      # 小面板豁免：测试面板（1200x40=48k）天然跳过
SMOKE_TIMEOUT_S = 60.0         # 烟测 worker 子进程墙钟（兼抓死循环）
SMOKE_INTERP_CAP_S = 10.0      # 解释版 smoke 尺寸超过即提前返回（超预算信号）
EQUIV_RTOL = 1e-6
EQUIV_ATOL = 1e-9
ALPHA_MIN, ALPHA_MAX = 0.8, 2.5
ALGO_SLOW_ALPHA = 1.4          # 超过 = 算法复杂度超线性（向量化救不了）
ENGAGE_RATIO = 0.5             # est > budget x 0.5 才考虑 njit/警告


def env_subsample(env, rows: int, cols: int):
    """共享数组的子采样视图（零拷贝）。calibration 原样保留——计时用途，
    dev_end/sel_end 是日期串不影响；因子若因窗口不足在小面板上抛错，
    烟测返回 interp_error、门放行（fail-open，让正式调用报真实错误）。"""
    from .factor.env import FactorEnv
    r = min(int(rows), env.T)
    c = min(int(cols), env.N)
    sl = (slice(0, r), slice(0, c))
    return FactorEnv(env.o[sl], env.h[sl], env.l[sl], env.c[sl], env.v[sl],
                     env.dates[:r], env.symbols[:c],
                     listed=env.listed[sl],
                     amount=(env.amount[sl] if env.amount is not None else None),
                     calibration=getattr(env, "calibration", None))


# njit 专用视图：namedtuple（numba nopython 支持属性访问——spike 验证）。
# amount=None 用 8 字段版（None 进不了 numba 类型系统；访问 amount 的因子
# 自然编译失败回退解释版——与解释路径语义一致）。
EnvView8 = namedtuple("EnvView", ["o", "h", "l", "c", "v", "listed", "T", "N"])
EnvView9 = namedtuple("EnvView", ["o", "h", "l", "c", "v", "listed", "amount", "T", "N"])


def env_view(env):
    """FactorEnv -> njit 可编译的 namedtuple 视图（共享数组，零拷贝）。"""
    if getattr(env, "amount", None) is not None:
        return EnvView9(env.o, env.h, env.l, env.c, env.v, env.listed,
                        env.amount, env.T, env.N)
    return EnvView8(env.o, env.h, env.l, env.c, env.v, env.listed, env.T, env.N)


# 平台因子惯例是函数内自行 `import numpy as np`（worker _compile 是裸命名
# 空间），而 numba 不支持 import 语句——生产真实源码（2026-08-31 事故因子
# F6）全卡在这一步。njit 前剥离白名单模块的裸 import（同名绑定同一模块
# 对象，语义等价）；from-import/其它 import 保留（自然编译失败回退）。
_PLAIN_IMPORT_RE = re.compile(
    r"^\s*import (?:numpy|pandas|math)(?: as [A-Za-z_]\w*)?,?\s*$")


def njit_prepare(source: str) -> str:
    """剥离 numpy/pandas/math 的裸 import 行（仅此三类；只剥完整行）。"""
    kept = [ln for ln in source.splitlines()
            if not _PLAIN_IMPORT_RE.match(ln)]
    return "\n".join(kept)


def fit_alpha(t_small: float, t_smoke: float,
              cells_small: int, cells_smoke: int) -> float:
    """双尺寸实测缩放指数：t ∝ cells^alpha，钳 [ALPHA_MIN, ALPHA_MAX]。"""
    if t_small <= 0.0 or t_smoke <= 0.0 or cells_small <= 0 or cells_smoke <= cells_small:
        return 1.0
    alpha = math.log(t_smoke / t_small) / math.log(cells_smoke / cells_small)
    return max(ALPHA_MIN, min(ALPHA_MAX, alpha))


def extrapolate(t: float, cells_from: int, cells_to: int, alpha: float) -> float:
    if cells_from <= 0 or t <= 0:
        return 0.0
    return t * (cells_to / cells_from) ** alpha


def method_cost(method: str, params: dict) -> tuple[float, float | None]:
    """factor(env) 调用次数乘数 + 预算份额（给方法自身的面板机器留余量）。

    调用次数为代码事实（2026-09-01 核验）：check_causality = 1+1+6 扰动；
    audit ≈ 8 + 内嵌 evaluate ≈ 10；noise_test ≥ NOISE_MIN_WORLDS=10 世界
    （预算特例对齐 factor.noise.NOISE_BUDGET_SECS=240）；evaluate 族单次。
    share=None 表示预算特例（noise 门走自身 240s 预算）。
    """
    if method == "factor.check_causality":
        return 8.0, 1.0
    if method == "factor.audit":
        return 10.0, 1.0
    if method == "factor.noise_test":
        return 10.0, None
    if method == "factor.evaluate_composite":
        k = len(params.get("ingredients") or {})
        return float(k + 1), 0.5
    if method == "factor.flatness_test":
        table = params.get("params")
        n = len(table) if isinstance(table, dict) else 0
        return float(1 + 2 * n), 0.5
    # evaluate / evaluate_batch（进程池并行，per-member 墙钟≈单次）/
    # walk_forward / tail_placebo / day_perm_test
    return 1.0, 0.5


def noise_budget_s() -> float:
    from .factor.noise import NOISE_BUDGET_SECS
    return float(NOISE_BUDGET_SECS)


def classify_gate(smoke: dict, method: str, params: dict,
                  budget_s: float) -> dict:
    """烟测原始数据 -> 三档判定（pass / njit / reject）。纯函数可单测。

    smoke 字段（worker factor.smoke gate 模式）：
      t_small_s / small_cells / t_smoke_s / smoke_cells / full_cells
      too_slow（smoke 尺寸超 CAP 提前返回）/ interp_error
      njit: {available, compiled, compile_s, equiv, t_small_s, t_smoke_s, error}
    """
    t_small = float(smoke.get("t_small_s") or 0.0)
    t_smoke = float(smoke.get("t_smoke_s") or 0.0)
    cells_small = int(smoke.get("small_cells") or 0)
    cells_smoke = int(smoke.get("smoke_cells") or 0)
    cells_full = int(smoke.get("full_cells") or 0)
    out: dict = {"mult_share": None}
    if cells_full <= 0 or cells_smoke <= 0:
        return {"verdict": "pass", "reason": "insufficient_data"}

    alpha = fit_alpha(t_small, t_smoke, cells_small, cells_smoke)
    est = extrapolate(t_smoke, cells_smoke, cells_full, alpha)
    mult, share = method_cost(method, params)
    eff_budget = noise_budget_s() if share is None else budget_s * share
    projected = est * mult
    out.update({"alpha": round(alpha, 2), "est_full_s": round(est, 1),
                "mult": mult, "projected_s": round(projected, 1),
                "budget_s": round(eff_budget, 1),
                "too_slow_smoke": bool(smoke.get("too_slow"))})

    nj = smoke.get("njit") or {}
    est_c = None
    if nj.get("compiled") and nj.get("equiv"):
        alpha_c = fit_alpha(float(nj.get("t_small_s") or 0.0),
                            float(nj.get("t_smoke_s") or 0.0),
                            cells_small, cells_smoke)
        est_c = extrapolate(float(nj.get("t_smoke_s") or 0.0),
                            cells_smoke, cells_full, alpha_c)
        out["est_full_njit_s"] = round(est_c, 1)

    # 判定序：njit 可救（编译过+严格等价+编译版投影进预算）> 直跑 > 拒绝
    if (est_c is not None and est_c * mult <= eff_budget
            and (projected > eff_budget * ENGAGE_RATIO
                 or smoke.get("too_slow"))):
        out["verdict"] = "njit"
        return out
    if projected <= eff_budget and not smoke.get("too_slow"):
        out["verdict"] = "pass"
        out["warn"] = projected > eff_budget * ENGAGE_RATIO
        return out
    out["verdict"] = "reject"
    out["reason"] = "algo_slow" if alpha > ALGO_SLOW_ALPHA else "const_slow"
    return out
