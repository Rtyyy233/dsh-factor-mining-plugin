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
# 低效模式扫描（agent 生成代码的计算效率防御，层 1）
# ---------------------------------------------------------------------------

# 反模式来自实测教训（历史观测：vectorize-no-nested-loops /
# cost-bug 等）：逐行 apply、循环内 concat 重建面板、双层 Python 循环、循环内截面调用。
_INEFFICIENT_PATTERNS: list[tuple[str, str]] = [
    (r"\.iterrows\s*\(", "iterrows：逐行 Python 回调，比向量化慢 100x+"),
    (r"\.itertuples\s*\(", "itertuples：逐行迭代，优先向量化"),
    (r"\.apply\s*\(\s*lambda", "apply(lambda)：逐元素回调，优先列级向量化运算"),
    (r"\.applymap\s*\(", "applymap：逐格回调，优先向量化"),
]

def scan_inefficiency(source: str) -> dict[str, Any]:
    """静态低效模式扫描。返回 warning（不阻断——循环有时是对的，只提醒）。"""
    hits: list[dict[str, Any]] = []
    lines = (source or "").splitlines()
    for ln_no, line in enumerate(lines, 1):
        for pat, msg in _INEFFICIENT_PATTERNS:
            if re.search(pat, line):
                hits.append({"line": ln_no, "pattern": msg})
    # 双层以上 for 嵌套（近似：缩进递增的两个 for）
    depth = 0
    prev_for_indent = -1
    for ln_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("for ") and indent > prev_for_indent >= 0:
            hits.append({"line": ln_no, "pattern": "嵌套 for：疑似 T×N Python 循环，优先向量化（rolling/groupby/rank）"})
        if stripped.startswith("for "):
            prev_for_indent = indent
        # 循环体内 pd.concat（重建面板的元凶）
    has_concat = any("pd.concat" in l for l in lines)
    has_for = any(l.lstrip().startswith("for ") for l in lines)
    if has_concat and has_for:
        hits.append({"line": 0, "pattern": "for 循环 + pd.concat 共存：循环内重建面板是已知性能元凶，改为先收集再一次性 concat 或预分配"})
    if not hits:
        return {"ok": True, "hits": []}
    return {"ok": False, "hits": hits[:8],
            "advice": "疑似非向量化实现：参考因子写法文档·效率章节；优先复用 dsh_factor_mining.factor.ops 的向量化算子"}


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
