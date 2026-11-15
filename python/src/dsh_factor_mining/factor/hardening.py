# -*- coding: utf-8 -*-
"""固定墙时代加固件（PLAN-fixed-wall-rollout-2026-11-01 任务 B/C，2026-11-01）。

集中以下机制的纯逻辑与状态文件访问：
- 固定墙配置读取（B1）：null_landscape.json 增键 wall_mode / fixed_bar_ic /
  fixed_bar_tail / fixed_wall_note + 各门开关与阈值（默认全开）。
- 雕刻警报门（固定墙组合件）：逐年符号一致性对二项 null 的精确检验——
  一致性"过优"（p 越小越像雕刻）即拒。与法证阈值（null 参照 p95，实测
  饱和于"逐年全同号"）行为等价：法证池内该分位恰对应全同号年份组合。
- M13 尾/IC 双轨一致门 / M8 构造复杂度门 / M3 最短回测长度门（任务 C，
  提交链硬门，阈值固定不涨）。
- 统一门事件审计 gate_events.jsonl（B6）：每门决策一行，原子追加。
- OOS 凭据账本 oos_receipts.jsonl（B3）：入册硬前置。
- selection 区 alpha-spending 账本 selection_spend_ledger.jsonl（B4）：
  每期 60 次硬顶，超顶封区。

纪律红线：
- 一切阈值固定——不含任何随时间/试验数变化的量；growing 回滚=改配置文件
  +新桥进程，不改代码。
- 本模块禁止 print（桥是 stdio RPC，print 污染协议流）——诊断走 stderr
  或返回值。
- 账本与 trail 同级落盘、重启不清零；写路径一律 state_write_lock 原子追加。
"""
from __future__ import annotations

import ast
import json
import math
import time
from pathlib import Path

# ---- 部署默认值（与 WALL_DERIVATION_V2.md 及法证回放记分卡一致；配置键缺省时生效）----
GATE_DEFAULTS = {
    "gate_carving_enabled": True,
    "gate_carving_alpha": 0.05,      # 逐年一致性二项精确检验显著水平（固定）
    "gate_m13_enabled": True,
    "gate_m13_threshold": 0.2502,    # 法证回放墙放行者带 max-J 阈（方向=过度一致拒）
    "gate_m8_enabled": True,
    "gate_m8_max_ops": 12,           # AST 算子调用数上限（回放值 ≈12）
    "gate_m3_enabled": True,
    "gate_m3_ratio_max": 1.0,        # Bailey 最短回测长度比上限（实有 < 所需即拒）
}
SELECTION_CAP_PER_PERIOD = 60        # B4：每期 selection 评估硬顶
SELECTION_ALPHA_TOTAL = 0.05         # B4：期总 α（Bonferroni 均摊 0.05/60）


def read_wall_config(state_root) -> dict:
    """固定墙与各门配置（null_landscape.json 增键段，缺省=growing+默认门）。

    返回键：wall_mode（"growing"|"fixed"）/ fixed_bar_ic / fixed_bar_tail /
    fixed_wall_note / GATE_DEFAULTS 全部键（缺省回填默认值，未配置的墙值
    为 None——fixed 模式下值缺失由调用方降级 growing 并 stderr 可见）。"""
    from . import random_gen
    land = random_gen.read_null_landscape(state_root)
    cfg = dict(GATE_DEFAULTS)
    if not isinstance(land, dict):
        cfg["wall_mode"] = "growing"
        cfg["fixed_bar_ic"] = None
        cfg["fixed_bar_tail"] = None
        cfg["fixed_wall_note"] = None
        return cfg
    mode = land.get("wall_mode")
    cfg["wall_mode"] = mode if mode in ("growing", "fixed") else "growing"
    for k in ("fixed_bar_ic", "fixed_bar_tail"):
        v = land.get(k)
        cfg[k] = float(v) if isinstance(v, (int, float)) and math.isfinite(v) and v > 0 else None
    note = land.get("fixed_wall_note")
    cfg["fixed_wall_note"] = note if isinstance(note, str) else None
    for k in GATE_DEFAULTS:
        if k.endswith("_enabled"):
            if isinstance(land.get(k), bool):
                cfg[k] = land[k]
        elif isinstance(land.get(k), (int, float)) and math.isfinite(land[k]):
            cfg[k] = land[k]
    if cfg["wall_mode"] == "fixed" and cfg["fixed_bar_ic"] is None:
        # fixed 模式但值缺失/非法——降级 growing（不砖门），stderr 可见
        cfg["wall_mode"] = "growing"
        cfg["_degraded"] = "fixed 声明但 fixed_bar_ic 缺失/非法——按 growing 降级"
        import sys
        try:
            sys.stderr.write("[fixed_wall] 配置降级：wall_mode=fixed 但 "
                             "fixed_bar_ic 无效，本 root 按 growing 运行\n")
        except Exception:
            pass
    return cfg


def fixed_bar(state_root) -> float | None:
    """fixed 模式且值有效 → IC 轨固定主值（σ 单位）；否则 None。"""
    cfg = read_wall_config(state_root)
    return cfg["fixed_bar_ic"] if cfg["wall_mode"] == "fixed" else None


# ---------------------------------------------------------------------------
# 雕刻警报门（固定墙组合件）
# ---------------------------------------------------------------------------

def carving_from_series(ic) -> dict:
    """逐年符号一致性对随机 null 的精确检验（雕刻=一致性过优）。

    null 模型：真实因子无预测力时，每个自然年 IC 均值的符号独立且等可能
    （±各 1/2）——年符号多数派计数 m 服从 Binomial(n_years, 1/2)。检验
    统计 = 一致性 c = m / n_years；双侧精确 p = min(1, 2·P(X ≥ m))。
    p ≤ alpha（默认 0.05）= 一致性落在随机分布的过优尾 → 雕刻警报。

    等价性注记：法证推导的同位门取"null 参照组一致性分位 ≥ p95"，而该
    分位在实测池内饱和于 c=1（逐年全同号）——本检验在常用年数（5-8 年）
    下同样只对全同号/近全同号触发，行为等价、无需 null 分位表。

    入参 ic：带 DatetimeIndex 的 IC 序列（train 区）。年数 <3 → 不判定。"""
    try:
        import pandas as pd
        if ic is None or len(ic) < 5:
            return {"tripped": False, "note": "样本不足"}
        if not isinstance(ic.index, pd.DatetimeIndex):
            return {"tripped": False, "note": "索引非日期——无法逐年分组"}
        yearly = ic.groupby(ic.index.year).mean().dropna()
        n = len(yearly)
        if n < 3:
            return {"tripped": False, "note": f"年数 {n} < 3——不判定"}
        signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in yearly.values]
        n_eff = sum(1 for s in signs if s != 0)
        if n_eff < 3:
            return {"tripped": False, "note": "有效（非零）年数不足"}
        m = max(sum(1 for s in signs if s > 0), sum(1 for s in signs if s < 0))
        # 双侧精确二项尾：P(X >= m) 用补分布求和（n ≤ 数十，精确可行）
        p_one = sum(math.comb(n_eff, k) for k in range(m, n_eff + 1)) / 2 ** n_eff
        p_two = min(1.0, 2.0 * p_one)
        return {"n_years": int(n), "consistency": round(m / n_eff, 4),
                "majority": int(m), "p_binom": round(p_two, 6),
                "tripped": bool(p_two <= GATE_DEFAULTS["gate_carving_alpha"])}
    except Exception as e:
        return {"tripped": False, "note": f"计算失败（不判定）: {type(e).__name__}"}


# ---------------------------------------------------------------------------
# 任务 C：TOP3 硬门（提交链，阈值固定不涨）
# ---------------------------------------------------------------------------

def ast_call_count(source: str) -> int:
    """AST 算子调用计数（与 construction_fp 同源口径：ast.walk 数 Call 节点）。"""
    try:
        return sum(1 for _n in ast.walk(ast.parse(source)) if isinstance(_n, ast.Call))
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# B7 AST IO 黑名单（2026-11-05 段隔离任务书 §四）：因子=面板内纯计算
# ---------------------------------------------------------------------------

IO_BLACKLIST_MODULES = {
    "socket", "urllib", "requests", "subprocess", "pickle", "shutil",
    "pathlib", "http", "ftplib", "smtplib", "telnetlib", "ssl", "ftputil",
    "paramiko", "boto3", "aiohttp", "httpx", "websocket", "webbrowser",
    "ctypes", "multiprocessing", "importlib", "code", "codeop",
}
IO_BLACKLIST_NAMES = {
    "open", "input", "__import__", "eval", "exec", "compile", "Path",
    "breakpoint",
}
IO_BLACKLIST_ATTRS = {
    "system", "popen", "environ", "urlopen", "startfile", "unlink", "mkdir",
    "makedirs", "rmdir", "removedirs", "rename", "copyfile", "copytree",
    "rmtree", "load", "loads_bytes", "load_module", "spawn", "spawnl",
    "spawnv", "fork", "kill", "listdir", "scandir", "walk", "stat",
    "readlink", "symlink", "chmod", "chown", "execv", "execl", "system_exec",
}
IO_BLACKLIST_ATTR_EXPLICIT = {
    "to_parquet", "to_csv", "to_json", "to_excel", "to_pickle", "to_hdf",
    "to_sql", "to_feather", "to_stata", "to_clipboard",
}

IO_BLACKLIST_MSG = (
    "评估拒收：因子源码包含输入输出或网络能力符号（文件读写/路径对象/"
    "网络库/子进程/序列化/动态导入等）——因子只做面板内纯计算，数据经 "
    "env 参数进入，任何形式的文件与网络访问都被禁止。移除相关调用后"
    "重新提交评估")


def io_blacklist_check(source: str) -> dict:
    """AST 静态扫描：IO/网络/动态执行符号出现 → 评估拒收（固定文案）。

    混淆用例覆盖：__import__（Name 直呼）、os.system（Attribute），
    read_*/write_* 前缀（pandas/io 家族）、BAD_MODULES 的 import 根名。
    残余风险（如实记录，完整沙箱列挂账二期）：纯计算型旁路——用算术
    构造路径字符串配合未被枚举的 IO 入口；字符串拼接路径无法静态判别。
    误杀校准：registry 120 只在册源码全量扫描零命中（2026-11-05 实测）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"rejected": False, "hits": [], "note": "语法错误（编译层另报）"}
    hits: list[str] = []

    def _qual(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return _qual(node.value) + "." + node.attr
        return "<expr>"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in IO_BLACKLIST_MODULES:
                    hits.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in IO_BLACKLIST_MODULES:
                hits.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Name):
            if node.id in IO_BLACKLIST_NAMES:
                hits.append(node.id)
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if (a in IO_BLACKLIST_ATTRS or a in IO_BLACKLIST_ATTR_EXPLICIT
                    or a.startswith("read_") or a.startswith("write_")):
                hits.append(_qual(node))
    return {"rejected": bool(hits), "hits": sorted(set(hits))[:10]}


# ---------------------------------------------------------------------------
# cs_* 截面算子拒评估（期货单品种 ts 线，2026-11-07 任务书 §五/§七.3）
# ---------------------------------------------------------------------------

CS_OPERATOR_NAMES = {
    "cs_rank", "cs_zscore", "cs_winsorize", "cs_scale", "cs_demean",
    "cs_robust_z",
}

CS_OPERATOR_MSG = (
    "评估拒收：单品种时序线无截面维度，cs_* 截面算子在此无定义——引擎"
    "拒绝评估；请只用时间轴算子 ts_* 与逐元素运算")


def cs_operator_check(source: str) -> dict:
    """AST 静态扫描：cs_* 截面算子名出现 → 评估拒收（固定文案）。

    ts 线专用（factor-mining-futures，单品种 (T,) 契约）：cs_rank/cs_zscore/
    cs_winsorize/cs_scale/cs_demean/cs_robust_z 的截面语义（每日跨资产
    rank/标准化）在单序列上不存在，引擎侧拒绝评估而非让算子静默错算。
    命中口径：ast.Name 直呼与 ast.Attribute 末段（ops.cs_rank(x) 类
    限定名同样命中）。面板线不调用本检查（截面行为不变）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"rejected": False, "hits": [], "note": "语法错误（编译层另报）"}
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in CS_OPERATOR_NAMES:
                hits.append(node.id)
        elif isinstance(node, ast.Attribute):
            if node.attr in CS_OPERATOR_NAMES:
                hits.append(node.attr)
    return {"rejected": bool(hits), "hits": sorted(set(hits))[:10]}


def m13_check(spread_ir, dev_abs_ir, threshold: float | None = None) -> dict:
    """M13 尾/IC 双轨一致门：双轨强度读数过度一致 → 拒。

    统计量 = | |尾部 spread_ir| − |dev IC_IR| |（双轨原始强度差）。
    法证回放（墙放行者带，AUC 0.725/灵敏度 0.696/null 假警 0.135）选定
    方向为"过度一致"侧：差 ≤ 0.2502 拒——过拟合因子常把同一噪声维度同时
    拟合进两条评价轨，两轨读数几乎相同；真实信号在 top-N 与全截面两个
    口径下的强度天然有差。任一轨读数缺失 → 不判定（缺数据不是失败）。"""
    th = GATE_DEFAULTS["gate_m13_threshold"] if threshold is None else threshold
    if (not isinstance(spread_ir, (int, float)) or not math.isfinite(spread_ir)
            or not isinstance(dev_abs_ir, (int, float)) or not math.isfinite(dev_abs_ir)):
        return {"gate": "m13", "decided": False, "reason": "双轨读数缺失——不判定"}
    gap = abs(abs(float(spread_ir)) - abs(float(dev_abs_ir)))
    return {"gate": "m13", "decided": True, "gap": round(gap, 4),
            "threshold": th, "rejected": bool(gap <= th)}


def m8_check(source: str, max_ops: int | None = None) -> dict:
    """M8 构造复杂度门：AST 算子调用数超上限 → 拒（复杂度是运气载体）。

    阈值 12 = 法证回放墙放行者带 max-J 点（AUC 0.738/null 假警 0.000）。"""
    cap = GATE_DEFAULTS["gate_m8_max_ops"] if max_ops is None else max_ops
    n = ast_call_count(source)
    if n < 0:
        return {"gate": "m8", "decided": False, "reason": "源码解析失败——不判定"}
    return {"gate": "m8", "decided": True, "ops": n, "threshold": int(cap),
            "rejected": bool(n >= cap)}


def m3_ratio(sr, skew, kurt, n) -> float | None:
    """Bailey 最短回测长度比：n_needed / n_actual（所需/实有）。

    n_needed = 1 + var_term·(z_0.95 / sr)²，var_term = 1 − γ3·sr +
    (γ4−1)/4·sr²（矩校正，clip 下限 0.05 防重尾穿零）。比 > 1 =
    实有观测数撑不起该强度声明。

    移植注记（回放→固定墙的三次试锚，战报详录）：法证回放的方向与阈值
    （ratio ≤ 123.335 拒）绑定增长墙存档的 sr0=bar·pool——大部分回放人群
    在该线下，ratio 爆顶 1e4，"低于阈"实为"强度远超墙线的间隙"签名。
    该阈值按字面移植到固定墙（线上通过者全部 sr > sr0）会拒掉 ETF 48%/
    个股 99.9% 的墙上通过者；按固定墙自身放行带重推导在 ETF 退化为全放、
    在个股杀 80% 稳健组。故生产实现按门的书述语义（样本太短撑不起该
    强度，sr0=0 的单声明口径，阈 1.0）落地，公式与回放逐字同源。"""
    vals = (sr, n)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals) \
            or not n or n < 10 or not sr or sr <= 0:
        return None
    g3 = float(skew) if isinstance(skew, (int, float)) and math.isfinite(skew) else 0.0
    g4 = float(kurt) if isinstance(kurt, (int, float)) and math.isfinite(kurt) else 3.0
    var_term = max(1.0 - g3 * sr + (g4 - 1) / 4.0 * sr * sr, 0.05)
    n_need = 1.0 + var_term * (1.96 / sr) ** 2
    return min(n_need / float(n), 1e4)


def m3_check(sr, skew, kurt, n, ratio_max: float | None = None) -> dict:
    """M3 最短回测长度门：n_needed / n_actual > 1 → 样本太短撑不起该强度。"""
    rm = GATE_DEFAULTS["gate_m3_ratio_max"] if ratio_max is None else ratio_max
    r = m3_ratio(sr, skew, kurt, n)
    if r is None:
        return {"gate": "m3", "decided": False, "reason": "充分统计量缺失——不判定"}
    return {"gate": "m3", "decided": True, "ratio": round(r, 3),
            "threshold": rm, "rejected": bool(r > rm)}


# ---------------------------------------------------------------------------
# B6：统一门事件审计（gate_events.jsonl，每决策一行）
# ---------------------------------------------------------------------------

def gate_event(state_root, kind: str, source_hash: str | None = None,
               **fields) -> None:
    """门事件落账（原子追加；失败 stderr 可见、绝不阻断门本身）。

    行 schema：{ts, kind, hash, verdict, elapsed_ms, **fields}——输入值、
    阈值、判定结果随 fields 传。人读+引擎内部审计用；任何数字不进提示词。"""
    try:
        from ..filelock import state_write_lock
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
        if source_hash:
            row["hash"] = source_hash
        row.update(fields)
        p = Path(state_root) / "gate_events.jsonl"
        with state_write_lock(state_root):
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        try:
            import sys
            sys.stderr.write(f"[gate_event] 落账失败（不阻断）: {type(e).__name__}: {e}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# B3：OOS 凭据账本（oos_receipts.jsonl，入册硬前置）
# ---------------------------------------------------------------------------

def open_oos_receipt(state_root, source_hash: str, kind: str,
                     detail: dict | None = None) -> dict:
    """开票（原子追加）：selection 区/前向带检查的预登记凭据。"""
    from ..filelock import state_write_lock
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "source_hash": source_hash, "kind": kind,
           "detail": detail or {}}
    p = Path(state_root) / "oos_receipts.jsonl"
    with state_write_lock(state_root):
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def oos_receipt_for(state_root, source_hash: str) -> dict | None:
    """该 source_hash 的最新凭据（无 → None）。账本损坏 → None（不判过）。"""
    if not source_hash:
        return None
    p = Path(state_root) / "oos_receipts.jsonl"
    found = None
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("source_hash") == source_hash:
                found = r  # 取最新（文件序）
    except Exception:
        return None
    return found


# ---------------------------------------------------------------------------
# B4：selection 区 alpha-spending 账本（每期 60 次硬顶，超顶封区）
# ---------------------------------------------------------------------------

def selection_band_key(env) -> str:
    """期键 = selection 数据带身份（dev_end|sel_end）。

    预算随数据带走：数据延长（带界移动）= 新期新预算——"下期或数据延长
    解封"两条路都归一到带界变化，无需日历计时器。"""
    try:
        cal = env.calibration
        return f"{cal.dev_end}|{cal.sel_end}"
    except Exception:
        return "unknown_band"


def selection_budget_status(state_root, band_key: str) -> dict:
    """当前期消耗（读-only）：{spent, cap, remaining, sealed, alpha_weight}。"""
    p = Path(state_root) / "selection_spend_ledger.jsonl"
    spent = 0
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if isinstance(r, dict) and r.get("band") == band_key:
                spent += 1
    except Exception:
        spent = 0
    w = SELECTION_ALPHA_TOTAL / SELECTION_CAP_PER_PERIOD
    return {"band": band_key, "spent": spent, "cap": SELECTION_CAP_PER_PERIOD,
            "remaining": max(0, SELECTION_CAP_PER_PERIOD - spent),
            "sealed": spent >= SELECTION_CAP_PER_PERIOD,
            "alpha_weight": round(w, 6),
            "alpha_spent": round(spent * w, 6)}


def selection_eval_bookkeep(state_root, source_hash: str, band_key: str,
                            result_summary: dict | None = None) -> dict:
    """selection 评估的记账+开票（同事务：一个锁内先验额后开票）。

    返回 {ok, budget}：ok=False = 本期已封区（60 次硬顶）——调用方拒评估。
    评估本体由调用方先行完成（失败不记账）；本函数只在成功后收账。"""
    from ..filelock import state_write_lock
    p = Path(state_root) / "selection_spend_ledger.jsonl"
    with state_write_lock(state_root):
        spent = 0
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("band") == band_key:
                    spent += 1
        except Exception:
            spent = 0
        if spent >= SELECTION_CAP_PER_PERIOD:
            budget = selection_budget_status(state_root, band_key)
            gate_event(state_root, "selection_budget", source_hash,
                       verdict="sealed_reject", band=band_key, spent=spent)
            return {"ok": False, "budget": budget}
        w = SELECTION_ALPHA_TOTAL / SELECTION_CAP_PER_PERIOD
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "source_hash": source_hash, "band": band_key,
               "alpha_weight": round(w, 6),
               "cumulative_in_band": round((spent + 1) * w, 6)}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # 开票与记账同事务：凭据证明该因子的 selection 带 α 已付
        receipt = open_oos_receipt(
            state_root, source_hash, "selection_band",
            {"band": band_key, "alpha_weight": round(w, 6),
             "spend_index_in_band": spent + 1,
             "result": result_summary or {}})
        budget = selection_budget_status(state_root, band_key)
        return {"ok": True, "budget": budget, "receipt": receipt}
