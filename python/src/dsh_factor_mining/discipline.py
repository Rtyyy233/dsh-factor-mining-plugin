# coding=utf-8
"""研究纪律的基础构件：数据/口径指纹、因子结构签名、低效模式扫描。

这三个都是纯函数（无状态、无 I/O 副作用），被 bridge / evaluate / memory_pool
共用。指纹是 receipt / test_lock / 版本溯源 / 双池的共同地基：
    fingerprint = sha256(数据文件 bytes) + sha256(calibration dict) + engine version
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import __version__ as ENGINE_VERSION


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

def file_fingerprint(path: str | Path) -> str:
    """数据文件内容指纹（sha256 前 16 位）。按字节哈希：同内容同指纹，与路径无关。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def dict_fingerprint(d: dict[str, Any] | None) -> str:
    """口径/配置指纹：json 序列化（排序键）后 sha256 前 16 位。"""
    payload = json.dumps(d or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def calibration_fingerprint(calibration) -> str:
    """Calibration dataclass 或普通 dict → 指纹。

    v2 兼容（2026-08-20）：horizons 菜单未配置（None/缺省）时不参与指纹——
    不加菜单的既有部署指纹不变，landscape 不失效；一旦配置菜单，指纹变化
    → null 校准自动要求重跑（per-horizon 基线，设计内保守行为）。
    """
    if isinstance(calibration, dict):
        d = dict(calibration)
    else:
        try:
            d = asdict(calibration)
        except TypeError:
            d = dict(vars(calibration)) if not isinstance(calibration, dict) else dict(calibration)
    if isinstance(d, dict) and d.get("horizons") is None:
        d = {k: v for k, v in d.items() if k != "horizons"}
    return dict_fingerprint(d)


def source_fingerprint(source: str) -> str:
    """因子源码指纹（sha256 前 16 位）——receipt / 强制 causality / 双池的 key。"""
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()[:16]


def full_fingerprint(data_path: str | Path, calibration) -> str:
    """环境三元组指纹：数据 + 口径 + 引擎版本。"""
    return f"{file_fingerprint(data_path)}:{calibration_fingerprint(calibration)}:v{ENGINE_VERSION}"


# ---------------------------------------------------------------------------
# 因子结构签名（AST 调用序列）
# ---------------------------------------------------------------------------

def _qualname(node: ast.AST) -> str:
    """调用目标的限定名末段：pd.DataFrame.rank(...) → 'rank'；ops.ts_momentum(...) → 'ts_momentum'。"""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return "<expr>"


def structure_signature(source: str) -> dict[str, Any]:
    """从因子源码提取结构签名：AST 调用序列（带数值常量）。

    用途：方法维度查重（method_suspect）——同一个公式/方法换数据/换字段重跑时，
    数值指纹可能不同，但 AST 签名高度重叠。这是"提醒不是否决"信号。
    局限（已知且接受）：变量重排会变签名（漏匹配）；手写循环替代 rolling 提取不到
    语义（漏检，由数值指纹兜底）。
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return {"seq": [], "set": [], "n": 0, "parse_error": True}
    calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _qualname(node.func)
            consts = sorted({a.value for a in node.args
                             if isinstance(a, ast.Constant)
                             and isinstance(a.value, (int, float))})
            calls.append(f"{name}[{','.join(str(c) for c in consts)}]" if consts else name)
    return {"seq": calls, "set": sorted(set(calls)), "n": len(calls)}


def signature_similarity(a: dict, b: dict) -> float:
    """签名相似度：0.6×集合 Jaccard + 0.4×序列编辑距离相似度。"""
    sa, sb = set(a.get("set", [])), set(b.get("set", []))
    if not sa and not sb:
        return 1.0 if a.get("n", 0) == b.get("n", 0) else 0.0
    jaccard = len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0
    seq_a, seq_b = a.get("seq", []), b.get("seq", [])
    if not seq_a and not seq_b:
        seq_sim = 1.0
    else:
        # 简单编辑距离（序列短，O(n*m) 可承受）
        m, n = len(seq_a), len(seq_b)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev, dp[0] = dp[0], i
            for j in range(1, n + 1):
                cur = dp[j]
                dp[j] = min(dp[j] + 1, dp[j - 1] + 1,
                            prev + (0 if seq_a[i - 1] == seq_b[j - 1] else 1))
                prev = cur
        dist = dp[n]
        seq_sim = 1.0 - dist / max(m, n, 1)
    return 0.6 * jaccard + 0.4 * seq_sim


# ---------------------------------------------------------------------------
# 低效模式扫描（agent 生成代码的计算效率防御，层 1；P4 起 AST 分级）
# ---------------------------------------------------------------------------

# A 类（确定性反模式，causality 门硬拒——拒绝评估、不计 trial）：在
# factor(env) 里没有合法用途（ops 向量化算子库存在的前提下）。错拒的
# 代价 = agent 一次廉价改写；不拒的代价 = 每个垃圾源码先烧分钟级计算，
# 且并行会话下放大全机竞争（效率四层规划 Tier 1，2026-08-27 拍板 D3a）。
# B 类保留警告（循环有时确实是对的——如小常数窗口集迭代）。
_CLASS_A_PATTERNS = {
    "iterrows": "iterrows：逐行 Python 回调（比向量化慢 100x+）——"
                "改用 df.groupby(\"symbol\").transform/shift 或 unstack 宽表矩阵运算（ops.ts_*）",
    "itertuples": "itertuples：逐行迭代——改用 df.groupby(\"symbol\") 的向量化变换（ops.ts_delay/ts_diff）",
    "applymap": "applymap：逐格回调——改用 numpy 逐元素运算（env.c 等直接矩阵运算）",
    "apply_lambda": "apply(lambda)：逐元素/逐行回调——改用列级向量化（rolling/rank/numpy）",
    "loop_concat": "循环内 concat/append 重建数组（O(n²) 元凶）——"
                   "先 list.append 收集、循环外一次性 np.concatenate / pd.concat，或预分配输出矩阵",
}
# 循环体内重建类调用：np/pd 前缀的 concat/concatenate/append（裸
# list.append 是正确写法，靠 np/pd 前缀区分——文档约定别名）
_REBUILD_ATTRS = {"concat", "concatenate", "append"}
_REBUILD_PREFIXES = {"np", "pd", "numpy", "pandas"}


def scan_inefficiency(source: str) -> dict[str, Any]:
    """AST 低效模式扫描（P4：正则行扫升级 AST——正则漏掉循环体内
    np.concatenate 二次模式与跨行结构）。

    返回 {ok, hard_reject, hits, class_a, class_b, advice}：
    - class_a（hard_reject=True）：iterrows / itertuples / applymap /
      apply(lambda) / 循环体内 np|pd 前缀 concat-concatenate-append
      ——bridge 因果门硬拒（拒绝评估、不计 trial）
    - class_b：嵌套 for、apply(具名函数)——警告不阻断
    解析失败回退正则扫描（老行为兜底，只出警告不硬拒——编译错误由
    _compile 路径报结构化错误，扫描不越权）。"""
    import ast as _ast
    import re as _re

    class_a: list[dict[str, Any]] = []
    class_b: list[dict[str, Any]] = []

    try:
        tree = _ast.parse(source or "")
    except SyntaxError:
        hits = []
        for ln_no, line in enumerate((source or "").splitlines(), 1):
            for pat, msg in ((r"\.iterrows\s*\(", _CLASS_A_PATTERNS["iterrows"]),
                             (r"\.itertuples\s*\(", _CLASS_A_PATTERNS["itertuples"]),
                             (r"\.applymap\s*\(", _CLASS_A_PATTERNS["applymap"]),
                             (r"\.apply\s*\(\s*lambda", _CLASS_A_PATTERNS["apply_lambda"])):
                if _re.search(pat, line):
                    hits.append({"line": ln_no, "pattern": msg})
        return {"ok": not hits, "hard_reject": False, "hits": hits[:8],
                "class_a": [], "class_b": hits[:8],
                "advice": "疑似非向量化实现：参考因子写法文档·效率章节"}

    def _hit(kind: str, node, msg: str):
        (class_a if kind == "a" else class_b).append(
            {"line": getattr(node, "lineno", 0), "pattern": msg})

    def _rebuild_in_loop(node):
        """For/While 体内的 np|pd.concat/concatenate/append 调用节点。"""
        for sub in _ast.walk(node):
            if (isinstance(sub, _ast.Call) and isinstance(sub.func, _ast.Attribute)
                    and sub.func.attr in _REBUILD_ATTRS
                    and isinstance(sub.func.value, _ast.Name)
                    and sub.func.value.id in _REBUILD_PREFIXES):
                return sub
        return None

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            fn = node.func
            if isinstance(fn, _ast.Attribute):
                if fn.attr in ("iterrows", "itertuples", "applymap"):
                    _hit("a", node, _CLASS_A_PATTERNS[fn.attr])
                elif fn.attr == "apply":
                    arg = node.args[0] if node.args else None
                    if isinstance(arg, _ast.Lambda):
                        _hit("a", node, _CLASS_A_PATTERNS["apply_lambda"])
                    else:
                        _hit("b", node, "apply(具名函数)：若为逐元素/逐行回调请向量化"
                              "（列级 rolling/rank/numpy）——B 类警告")
        if isinstance(node, (_ast.For, _ast.While)):
            hit = _rebuild_in_loop(node)
            if hit is not None:
                _hit("a", hit, _CLASS_A_PATTERNS["loop_concat"])
            # 嵌套 for（AST 判定，替代旧缩进近似；只报一层避免重复告警）
            for sub in _ast.walk(node):
                if sub is not node and isinstance(sub, _ast.For):
                    _hit("b", sub, "嵌套 for：疑似 T×N Python 循环，优先向量化"
                          "（rolling/groupby/rank）——B 类警告")
                    break

    hits = class_a + class_b
    if not hits:
        return {"ok": True, "hard_reject": False, "hits": [], "class_a": [],
                "class_b": []}
    return {"ok": False, "hard_reject": bool(class_a),
            "hits": hits[:8], "class_a": class_a[:8], "class_b": class_b[:8],
            "advice": "疑似非向量化实现：参考因子写法文档·效率章节；"
                      "优先复用 dsh_factor_mining.factor.ops 的向量化算子"}


# ---------------------------------------------------------------------------
# 荒谬值守门（RED_FLAG，H1）
# ---------------------------------------------------------------------------

def red_flags_and_verdict(diag: dict[str, Any], region: str = "train",
                          test_diag: dict[str, Any] | None = None) -> dict[str, Any]:
    """对诊断对象做荒谬值守门 + 结构化 verdict（H1+H5）。

    规则来源：G73 教训（好得离谱的结果默认是 bug）+ 回测纪律
    （OOS 显著优于 train 是 regime/前视红旗）。
    """
    flags: list[str] = []
    ic_ir = diag.get("ic_ir_train" if region == "train" else "ic_ir")
    ic_n = diag.get("ic_n_train" if region == "train" else "ic_n", 0)
    if isinstance(ic_ir, (int, float)):
        if abs(ic_ir) > 5.0:
            flags.append(f"|IC_IR|={abs(ic_ir):.2f} > 5：好得离谱，默认按 bug 排查（前视/泄漏/口径）")
        if ic_n > 0 and abs(ic_ir) > 2.0 and ic_n < 40:
            flags.append(f"IC_IR={ic_ir:.2f} 但 n={ic_n} 样本不足，警惕小样本巧合")
    topn = diag.get("topn") if isinstance(diag.get("topn"), dict) else None
    if topn and isinstance(topn.get("net_annual"), (int, float)):
        if topn["net_annual"] > 200:
            flags.append(f"top-N 净年化 {topn['net_annual']:.0f}% > 200%：默认按 bug 排查")
    if test_diag is not None:
        t_ir = test_diag.get("ic_ir")
        if (isinstance(t_ir, (int, float)) and isinstance(ic_ir, (int, float))
                and ic_ir != 0 and t_ir > 0 and t_ir > 2 * abs(ic_ir)):
            flags.append(f"test IC_IR({t_ir:.2f}) 显著优于 train（{ic_ir:.2f}）：regime/前视红旗")

    # 多重检验门控（2026-08-18 复核补全）：deflated p 是入册硬门。此前 p 只是
    # 报告数字——z≥3 的不显著因子（p=0.9997 实测）照样 verdict=pass + 入册，
    # 选择运气不可排除。进 red_flags → verdict=needs_review → submit 拒绝
    # （复用现有「red_flags 未清不入册」链路）。
    if region == "train":
        dp = diag.get("deflated_train") or {}
        p, n_trials = dp.get("p"), dp.get("n_trials") or 1
        if p is None and isinstance(n_trials, (int, float)) and n_trials > 1:
            flags.append(
                f"N={n_trials:.0f} 次已试假设但缺池分布基线（trail<10 且未 null 校准）——"
                "deflated p 不可算，先跑 factor_random_generate(mode='null-calibration') 再入册")
        elif isinstance(p, (int, float)) and p > 0.05:
            flags.append(
                f"deflated p={p:.4f} > 0.05：{n_trials:.0f} 次已试假设的多重检验校正下不显著"
                f"（sr0={dp.get('sr0')} vs |IC_IR|="
                f"{abs(ic_ir) if isinstance(ic_ir, (int, float)) else '?'}）——选择运气不可排除，不入册")

    # 结构化 verdict（H5）：把散落的信号聚合成机器可读结论
    cp = diag.get("column_perm_train" if region == "train" else "column_perm") or {}
    z = cp.get("z")
    beta = diag.get("beta_exposure")
    if flags:
        verdict = "needs_review"  # 红旗优先：必须先解释再下结论
    elif not isinstance(z, (int, float)):
        verdict = "needs_review"
    elif abs(z) < 3.0:
        verdict = "fail" if region == "train" else "fail"
    elif isinstance(beta, (int, float)) and abs(beta) >= 0.3:
        verdict = "needs_review"  # beta 伪装嫌疑，不是硬失败
    else:
        verdict = "pass"
    return {"verdict": verdict, "red_flags": flags}
