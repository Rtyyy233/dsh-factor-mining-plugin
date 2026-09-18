# coding=utf-8
"""NDJSON JSON-RPC 2.0 stdio bridge for dsh-factor-mining.

The bridge owns data loading, the persistent environment cache, the user
factor library, and user state.  It never reads or writes files outside:
- user-specified data files (read only)
- user state root (writes)
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import time
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any

import numpy as np

from . import PROTOCOL_SCHEMA_VERSION, __version__
from .data.adapters import (
    DataConfig,
    DataError,
    EnvironmentSpec,
    build_factor_env,
    normalize_environment,
    probe_file,
    validate_env_lightweight,
)
from .discipline import (
    dict_fingerprint,
    file_fingerprint,
    full_fingerprint,
    scan_inefficiency,
    source_fingerprint,
)
from .factor import audit as audit_mod
from .factor.causality import check_causality
from .factor.eval_cache import cache_read, eval_key_from_parts
from .factor.evaluate import (
    _dsr_p_from_stats,
    _LuckSampler,
    evaluate,
    evaluate_batch,
    evaluate_composite,
    evaluate_selection,
    evaluate_test,
    evaluate_walk_forward,
)
from .library.contract import LibraryError, UserLibrary, query_entries
from .filelock import pid_alive, state_write_lock
from .lane import (
    current_lane,
    entry_lane,
    normalize_lane,
    own_entries,
    reset_request_lane,
    resolve_request_lane,
    set_request_lane,
    strip_lane_params,
)
from .procinfo import (
    attach_worker_limits,
    cpu_seconds,
    detach_worker_limits,
    effective_wall_timeout,
    omp_quiet_env,
    posix_limit_preexec,
    resolve_jobs,
    starvation_verdict,
    worker_mem_bytes,
)
from .state import (
    MINING_CONFIG,
    append_explored,
    factor_source_path,
    factor_source_stats,
    store_factor_source,
    append_search_path,
    append_trail,
    arc_rounds_bump,
    check_termination,
    inspiration_declare,
    lane_arc_rounds,
    lane_decl_seen_update,
    lane_explore_hashes_extend,
    lane_random_used,
    random_emit_count,
    read_json_list,
    read_mining_state,
    read_registry,
    record_round,
    reset_mining_state,
    write_registry,
)

JSON_RPC_ERRORS = {
    -32700: "Parse error",
    -32601: "Method not found",
    -32602: "Invalid params",
    -32603: "Internal error",
}

DOMAIN_ERRORS = {
    -32001: "DATA_CONFIG_REQUIRED",
    -32002: "DATA_ERROR",
    -32003: "PRECONDITION",
    -32004: "LIBRARY_ERROR",
    -32005: "PYTHON_PACKAGE_MISSING",
}


class BridgeError(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def _as_dict(value: Any, field: str) -> dict[str, Any]:
    """LLM 侧 json 参数按双态到达：JSON 对象或 JSON 字符串。归一化为 dict。

    这是边界契约：任何 json 类型参数（config/entry/override/sources/ingredients）
    都必须能接受字符串形态——LLM 传字符串是常态不是异常。
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as e:
            raise BridgeError(-32602, f"{field} 是 JSON 字符串但解析失败: {e}")
    if not isinstance(value, dict):
        raise BridgeError(-32602, f"{field} 必须是 JSON 对象（收到 {type(value).__name__}）")
    return value


def _error(code, message, data=None):
    return {"code": code, "message": message, "data": data}


def _lossless(value):
    """dispatch 出口消毒（2026-08-25 factor_trail_summary 事故产品化）：

    DSH 工具框架的 lossless JSON 检查拒绝 NaN/±Infinity/**-0**
    （walkJsonValue: !Number.isFinite || Object.is(x,-0)）。Python 侧
    json.dumps 默认把 NaN 写成裸字面量、-0.0 原样写——JS JSON.parse
    对 -0 不报错但检查击杀（NaN 则 parse 直接炸）。产地实证：
    ic_series_sketch 的 round(-0.00004,4)=-0.0（生产 trail_engine
    66 个字面量，last-5 命中 → trail_summary 整体报错）。

    规则：NaN/±Inf → None；-0.0 → +0.0；numpy 标量 → Python 原生
    （in_process 模式下可能漏出）。响应层兜底，写入口另行归一化。"""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value == 0.0:
            return 0.0  # 归一化 -0.0（含 +0.0 原样）
        return value
    if isinstance(value, int):
        return int(value)
    try:  # numpy 标量（in_process 防御）
        import numpy as _np
        if isinstance(value, _np.generic):
            return _lossless(value.item())
    except Exception:
        pass
    if isinstance(value, dict):
        return {str(k): _lossless(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_lossless(v) for v in value]
    return value


# ---- 穷尽宣告拒收（2026-08-24，session.jsonl 诊断产品化） ----
# 实测：agent 33 个 turn 里 12 次宣告「因子空间已穷尽/天花板/建议换池子」，
# 每次被 push 后同一池子又能立刻挖出新因子族（Kyle IC_IR 0.485~0.612）——
# 12/12 假穷尽。「我的想法队列空了」≠「空间挖尽了」；后者只有机械判据
# （试验计数 / IC_IR 改善收敛）有资格宣判。停笔宣言混进 next_hypothesis
# 会被引擎回显背书成状态，所以写入口和回显口都拒收。
_SURRENDER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("穷尽", "穷尽宣告——12/12 实测全假，空间是否挖尽只有试验计数和 IC_IR 收敛判据说了算"),
    ("探索完成", "探索完成宣告——停点仅由引擎机械判据触发"),
    ("探索结束", "探索结束宣告——停点仅由引擎机械判据触发"),
    ("探索已达", "探索已达上限宣告——停点仅由引擎机械判据触发"),
    ("方向已完成", "方向完成宣告——单方向完成不是停点，换假设继续"),
    ("族已完成", "族完成宣告——单族完成不是停点，换假设继续"),
    ("家族已完成", "族完成宣告——单族完成不是停点，换假设继续"),
    ("合成完成", "合成完成宣告——里程碑不是停点，换假设继续"),
    ("已充分探索", "充分探索宣告——停点仅由引擎机械判据触发"),
    ("天花板", "天花板宣告——边际是否有收益由 IC_IR 收敛判据机械判定"),
    ("理论上限", "理论上限宣告——同上，引擎机械判定"),
    ("理论极限", "理论极限宣告——同上，引擎机械判定"),
    ("等待用户", "等待用户——挖掘阶段禁止停下来等指示"),
    ("用户决定", "等用户决定——挖掘阶段禁止"),
    ("建议用户", "替用户决策——挖掘阶段走法是 agent 自己的事"),
    ("问用户", "挖掘中途问询被禁止（数据路径冷启动除外）"),
    ("需要用户", "挖掘阶段不依赖用户输入"),
    ("换池", "数据集/池轮换是用户操作，不是 agent 假设——随里程碑汇报上报即可"),
    ("换频率", "数据/频率轮换是用户操作，不是 agent 假设"),
    ("换数据源", "数据源轮换是用户操作，不是 agent 假设"),
    ("换数据集", "数据集轮换是用户操作，不是 agent 假设"),
    ("新数据集", "新数据集是用户操作，不是 agent 假设"),
    ("新 stateRoot", "stateRoot 轮换是用户操作，不是 agent 假设"),
    ("新stateRoot", "stateRoot 轮换是用户操作，不是 agent 假设"),
    ("总结汇报", "总结汇报不是假设——下一轮构造什么、测什么才是"),
    ("收尾", "收尾不是假设——停点由引擎决定"),
    ("无法达到", "门槛达不到不是停点——换假设继续，或由 IC_IR 收敛判据机械停"),
    ("无法通过", "门槛判负不是停点——换假设继续，或由 IC_IR 收敛判据机械停"),
)


def _surrender_match(text: str) -> str | None:
    """命中停笔宣言 → 返回拒收理由；合法假设 → None。"""
    for pat, why in _SURRENDER_PATTERNS:
        if pat in text:
            return why
    return None


# ---- 成分血缘指纹（2026-08-25 CMF 事故产品化） ----
# 事故：pw15_compD 合成因子的 IC 序列与 pw15 核心解相关（尾对齐
# ρ<0.6），家族链断裂 → 引擎误以为 agent 在换方向 → 3/6/9 升级从未
# 触发，agent 在同一族磨了 10 小时。修复：源码的 MinHash 构造指纹
# 让同族磨种（共享构造成分）保持链连续——合成=核心+装饰，指纹
# 重叠度高；真正换方向=全新构造，指纹不重叠。

_FP_N_HASHES = 32
_FP_SHINGLE_LINES = 2
_FP_LINEAGE_THRESH = 0.25  # MinHash Jaccard 估计的族归属阈值


def _stable_hash(s: str) -> int:
    """跨进程稳定的 32-bit 哈希（不用内置 hash——PYTHONHASHSEED 每次启动
    不同，指纹会不可复现）。"""
    import hashlib
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)


def _construction_fingerprint(source: str) -> list[int]:
    """源码构造指纹：MinHash 签名（32 个 uint32，~128 字节/条）。

    1. 规范化：去注释行、空行、docstring
    2. 行对 shingle（连续 2 行）：比单行更稳定（微改一行只影响 2 个
       shingle），比 3 行更敏感（装饰性改写不该完全遮蔽核心）
    3. MinHash：32 个确定性通用哈希取 min → Jaccard 估计"""
    import re as _re
    # 去 docstring（三引号块）
    src = _re.sub(r'"""[\s\S]*?"""', "", source)
    src = _re.sub(r"'''[\s\S]*?'''", "", src)
    lines = []
    for ln in src.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    shingles = []
    k = _FP_SHINGLE_LINES
    for i in range(max(len(lines) - k + 1, 1)):
        sh = "\n".join(lines[i:i + k])
        shingles.append(_stable_hash(sh))
    if not shingles:
        return [0] * _FP_N_HASHES
    # MinHash：确定性系数（固定种子 → 跨进程可复现）
    import random as _random
    rng = _random.Random(20260825)
    coeffs = [(rng.randrange(1, 2 ** 31), rng.randrange(0, 2 ** 31))
              for _ in range(_FP_N_HASHES)]
    return [min(((a * sh + b) % (2 ** 31 - 1)) & 0xFFFFFFFF
                for sh in shingles) for a, b in coeffs]


def _fingerprint_similarity(sig_a: list, sig_b: list) -> float:
    """MinHash 签名一致率 = Jaccard 的无偏估计。"""
    if not isinstance(sig_a, list) or not isinstance(sig_b, list) \
            or len(sig_a) != len(sig_b) or not sig_a:
        return 0.0
    return sum(1 for a, b in zip(sig_a, sig_b) if a == b) / len(sig_a)


# ---- 探索相似性防线（2026-09-04，analysis/exploration_similarity 01/04 产品化） ----
# 审计实证：26 对双双 pass 的同构重交全部集中在反重复机制上线前
# （|IC_IR| 差中位 0.008，零信息增量，双双入册 0 对）；42% 的「新方向」
# 宣言轮旗舰与先例近重复（H4）而变体宣言轮均值反而最低（0.319）；
# prompt 干预双盲实验阴性 = 生成层无缺陷。病灶在信息流：重复的代价在
# 提交瞬间不可见、叙事与结构不对账。防线只修信息流，不动生成。
_NOVELTY_GATE_SIM = 0.80          # S1 结构相似拦截线（先例源码可核对常量时）
_NOVELTY_GATE_SIM_NOCODE = 0.95   # 先例源码缺失时的保守拦截线
_DECL_INFO_MARKERS = ("新信息源", "新维度", "首次引入")
_DECL_CONSTRUCT_MARKERS = ("新构造", "新方向", "新机制")
_DECL_FLAGSHIP_MAX_SIM = 0.6      # S2「新构造」宣称的旗舰相似上限

_ENV_ATTR_RE = re.compile(
    r"env\.([A-Za-z_]\w*)"
    r"|env(?:\.data)?\[\s*['\"](\w+)['\"]\s*\]"
    r"|env\.get\(\s*['\"](\w+)")
_NUM_LIT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)")


def _env_attrs(source: str) -> "set[str]":
    """L1 数据列引用集合（S2 新信息源判据——审计 02 文档：口径/文献/
    机制叙事都不是信息源，只有数据列增量是）。env.data/env.get 的
    访问器本身不算列。"""
    attrs: set[str] = set()
    for a, b, c in _ENV_ATTR_RE.findall(source):
        attrs |= {x for x in (a, b, c) if x and x not in ("data", "get")}
    return attrs


def _numeric_literals(source: str) -> "list[float]":
    """去 docstring/注释后的数值常量多重集（S1 参数扫描判据：结构相似
    但常量有变 = 合法深挖（I1 证无边际衰减），放行计遥测；常量未变 =
    同构重交，拦截）。启发式刻意偏「误放不误拦」。"""
    src = re.sub(r'"""[\s\S]*?"""', "", source)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"#[^\n]*", "", src)
    return sorted(float(x) for x in _NUM_LIT_RE.findall(src))


def _entries_linked(a: dict, b: dict) -> bool:
    """两试验条目是否同族（v5 血缘 + v3 IC 双信号 OR，对称谓词）。

    从 _family_chain 的链内比较提取——链回溯与断链检测（arc 归零）必须
    用同一判定，否则「streak 断了但 arc 没归零」或反之。任一侧缺信号
    （无 sketch 且无指纹，如旧条目）→ False（与 _family_chain 的断链
    语义一致：无信号条目是单例族）。"""
    def _sketch(e):
        s = e.get("ic_series_sketch")
        return s if isinstance(s, (list, tuple)) and len(s) >= 5 else None

    sa, sb = _sketch(a), _sketch(b)
    if sa is not None and sb is not None:
        from .factor.evaluate import _tail_aligned_corr
        c = _tail_aligned_corr(list(sa), list(sb))
        if c is not None and abs(c) >= 0.6:
            return True
    fa, fb = a.get("construction_fp"), b.get("construction_fp")
    if (isinstance(fa, list) and len(fa) == _FP_N_HASHES
            and isinstance(fb, list) and len(fb) == _FP_N_HASHES):
        if _fingerprint_similarity(fa, fb) >= _FP_LINEAGE_THRESH:
            return True
    return False


# ---- arxiv 文献通道修复（2026-08-24 审计：31% 物理噪声 / 26% 论文级冗余 /
# 自我词汇回音室 / paper→hypothesis 无溯源） ----
# F2 类目过滤默认：q-fin 全类目 OR 链。审计中 28/28 次调用裸查全库，
# cross-section/volume/momentum 等金融词与物理词碰撞返回了角动量/Higgs/
# 薛定谔方程。除非显式传 category，bridge 层一律注入此过滤。
_QFIN_CATS = ("q-fin.ST", "q-fin.PM", "q-fin.TR", "q-fin.CE",
              "q-fin.GN", "q-fin.MF", "q-fin.PR", "q-fin.RM")
_QFIN_FILTER = "(" + " OR ".join(f"cat:{c}" for c in _QFIN_CATS) + ")"

# F5 查询种子正交化：方法论语域池，与 agent 当前假设词汇解耦——
# 文献注入的目的恰恰是打破先验，用 agent 自己的词汇做相关性检索
# 只会强化先验（28 条实测查询全部落在 agent 正卡着的族词汇内）。
# 按 ledger.searches 计数轮转，不依赖随机数（可复现）。
_LITERATURE_SEEDS = (
    "realized skewness kurtosis higher moments cross-sectional returns",
    "overnight intraday return decomposition predictability",
    "long memory fractional integration volatility persistence",
    "copula tail dependence cross-sectional asset returns",
    "permutation entropy complexity measure financial time series",
    "regime switching state dependent cross-sectional pricing",
    "wavelet multi-scale decomposition return forecasting",
    "extreme value theory cross-sectional tail risk",
    "calendar seasonality anomalies asset pricing",
    "network centrality spillover cross-sectional returns",
    "optimal transport distance return distribution",
    "realized volatility forecasting har heterogeneity",
    "jump detection bipower variation cross-section",
    "option implied information equity returns",
)

# F4 引用溯源：arxiv_id 归一化（接受完整 URL 或裸 id）
_ARXIV_ID_RE = re.compile(r"(?:arxiv\.org/abs/)?(abs/)?(\d{4}\.\d{4,5})(v\d+)?$")


# 灵感重置声明（2026-09-01 规划书 WS-B/D1）：next_hypothesis 前缀语法。
# 判定序在 _surrender_match 之前——换源宣言不是停笔宣言，理由里出现
# 「无新假设」等措辞不该被连坐拒收。半/全角冒号都收。
_INSPIRATION_RESET_RE = re.compile(
    r"^\s*\[灵感重置[:：](?P<mode>literature|random)\]\s*(?P<reason>.+)$",
    re.DOTALL)


def _inspiration_decl_match(text: Any):
    """合法声明 → (mode, reason)；无前缀/模式名不在枚举/理由为空 → None
    （按普通 next_hypothesis 处理，不给 ACK——换源必须说清为什么，
    空理由的声明只是换了包装的停笔）。"""
    if not isinstance(text, str) or not text.strip():
        return None
    m = _INSPIRATION_RESET_RE.match(text.strip())
    if m and m.group("reason").strip():
        return m.group("mode"), m.group("reason").strip()
    return None


# ---- 策略指令选择器（2026-08-24：31 次手动 push 的实证策略机械化） ----
# session.jsonl 全轨迹配对分析（用户 push 类型 × 当时引擎状态）推导的优先级
# 规则表。设计原则与 v3 停点一致：判断收进引擎，机械信号驱动，散文不参与。
_STRATEGY_DIRECTIVES = {
    "continue": ("继续探索：执行上一轮声明的假设；完成后立即记录归因并构造"
                 "下一轮假设，不要停下来等待指示。"),
    "refine": ("深挖当前最优因子：分析它的特性与构成（各窗口/算子/信息源"
               "各自承担了什么），构思补正与强化方法，逐一验证。"),
    "rotate": ("换一个方向：先用 factor_query_paths 查已试路径避免重复，"
               "再从一个未覆盖的维度构造新假设并验证。"),
    "literature": ("从文献找灵感：用 factor_arxiv_search 检索方法论论文"
                   "（数学结构/微观结构/时间序列建模），提取一个可迁移的"
                   "构造思路，自己改进后验证。"),
    "random": ("随机因子探索：用 factor_random_generate(mode='explore') 生成"
               "一批随机因子（省略 seed 走运行时派生），三列表（top/top_tail/"
               "top_net）幸存者逐个走标准管线（causality→evaluate→"
               "evaluate_batch deflation）。幸存者是选择不是结论——未超 "
               "null p95 或门链未过的如实入账；对幸存者的改造（换窗口/换"
               "算子/加条件）按普通假设驱动试验推进。"),
    "compose": ("做中阶合成：从 trail 中挑已验证但未入册的单因子，构造"
                "正交组合（目标截面相关 < 0.7），用 factor_evaluate_composite 验证。"),
    "query": ("正交性审计：计算最近入册因子与既有入册因子的截面相关性，"
              "确认是否带来增量信息；发现高相关就记录 explored 并构造去重组合。"),
}


def _pick_strategy(*, stop_kind: str | None = None, streak: int,
                   pending_rejected, frozen: bool, plateau: bool,
                   pass_unadmitted: int, accepted_n: int, agent_rounds: int,
                   n_trials: int, lit_search_count: int = 0,
                   family_marginal: dict | None = None,
                   inspiration_reset: dict | None = None,
                   random_available: bool = True) -> dict | None:
    """下一轮方向类型选择（纯函数，供注入器与 loop 指令共用）。

    停点分流（2026-08-25 arc 化 + 2026-08-26 v8 族收敛）：
    - stop_kind ∈ {finalize, fail_streak} → None（真终态，注入器静默——
      交还用户；convergence 已退役：全局/族内枯竭都只做换向信号）
    - inspiration_reset（2026-09-01 灵感源规划书 WS-B/D1：agent 声明
      [灵感重置:literature|random] 且配额已扣）→ 强制挂对应策略，
      优先于 direction_budget 与 R1-R8——声明本身就是换源换向动作；
      random 配额在声明受理时已查（D3），此处不再判 random_available
    - stop_kind == direction_budget（arc/簇上限/族内收敛）→ 强制
      rotate/literature/random（双枯竭时经 _exhausted_source 轮转，
      族边际 <-0.10 偏 literature），跳过 R3-R8——预算耗尽/族收敛的
      方向不允许 continue/refine 原地续推；换向断链自动重置预算，
      注入器照常推进
    - stop_kind == None（running）→ 常规优先级

    常规优先级（v6 2026-08-25：家族升级改边际收益驱动，删 streak 绝对阈值；
    2026-08-27 双轨化：R1/R2 与族收敛同口径——**两线都枯竭**才触发，单线
    枯竭只出 escalation（IC 平但尾部有苗头的方向不被强制换向））：
    R1 IC 与尾部线边际都严重枯竭（<-0.10）→ _exhausted_source
       （D2 轮转：lit_search_count % 3 == 2 且 random 配额可用 → random，
       否则 literature——文献连注两发仍未破局时给无先验探索一次机会）
    R2 IC 与尾部线边际都枯竭（<-0.05）→ rotate
    R3 streak≥3 且族内平台 plateau → refine（有最优载体+边际枯竭
       → 深挖载体特性补正强化）
    R4 pending 被拒收（停笔宣言）→ rotate
    R5 frozen pending（同一句冻结 ≥2 轮）→ rotate
    R6 accepted≥3 且为 3 的倍数 → query（里程碑审计）
    R7 pass 未入册 ≥3 → compose
    R8 兜底 → continue
    尾部线无数据（tail_marginal.enough_data=False）→ 回到 IC 单轨判定。
    key：常规类型含 round/n_trials（单调保证新鲜）；query 用 accepted。"""
    if stop_kind in ("finalize", "fail_streak"):
        return None
    fam = family_marginal or {}
    fam_m = fam.get("marginal")
    fam_ok = fam.get("enough_data") and isinstance(fam_m, (int, float))
    tail = fam.get("tail_marginal") or {}
    tail_m = tail.get("marginal")
    tail_ok = tail.get("enough_data") and isinstance(tail_m, (int, float))

    def _both_exhausted(level: float) -> bool:
        """IC 线低于 level 且（尾部线在场时也低于 level）。尾部线无数据
        → IC 单轨（与族收敛的「spread < 2Wf → IC-only」同一回退口径）。"""
        if not (fam_ok and fam_m < level):
            return False
        return (not tail_ok) or tail_m < level

    def _exhausted_source(lead: str, close: str) -> tuple[str, str]:
        """双严重枯竭（-0.10 档）的换源选择（D2 轮转）：每第 3 次
        （lit_search_count % 3 == 2）换 random——确定性轮转，无随机数，
        与 _LITERATURE_SEEDS 同一可复现原则；random 配额尽（本方向段
        已发射过）回落 literature 并如实说明。lead/close 拼接原文案；
        random 分支不拼 close（换族语义由 direction_budget 后缀覆盖）。"""
        mtxt = (f"（IC {fam_m:+.3f}"
                + (f"，尾部 {tail_m:+.3f}" if tail_ok else "，尾部线无数据")
                + "）")
        if lit_search_count % 3 == 2 and random_available:
            return ("random",
                    f"{lead}边际均严重枯竭{mtxt}——文献检索累计 "
                    f"{lit_search_count} 次，轮转到无先验探索"
                    "（随机因子空间不依赖假设源是否枯竭）")
        if lit_search_count % 3 == 2:
            return ("literature",
                    f"{lead}边际均严重枯竭{mtxt}——本应轮转随机探索但本方向段 "
                    "random 配额已用，仍走文献注入" + close)
        return ("literature",
                f"{lead}边际均严重枯竭{mtxt}——内生假设源枯竭，必须文献注入"
                + close)

    t = None
    if inspiration_reset is not None:
        # WS-B/D4 冷启动语义：当前方向整体作废——强制换源，跳过全部
        # 常规优先级；与 direction_budget 并存时声明优先（都是换向动作，
        # 断链重置语义一致，direction_budget 的后缀照常拼接）
        _reason = str(inspiration_reset.get("reason") or "")[:120]
        if inspiration_reset.get("mode") == "random":
            t = "random"
            why = (f"灵感重置（agent 声明）：{_reason}"
                   "——当前方向整体作废，从随机因子探索重启")
        else:
            t = "literature"
            why = (f"灵感重置（agent 声明）：{_reason}"
                   "——当前方向整体作废，从文献冷启动")
    elif stop_kind == "direction_budget":
        # 方向预算耗尽：族边际决定换向强度；无论边际如何都不允许原地续推
        if _both_exhausted(-0.10):
            t, why = _exhausted_source("方向段预算耗尽且 IC/尾部线", "后换族")
        else:
            t = "rotate"
            why = "方向段预算耗尽（轮次/簇试验上限）——必须换向，断链后预算自动重置"
    elif _both_exhausted(-0.10):
        t, why = _exhausted_source("IC 与尾部线", "")
    elif _both_exhausted(-0.05):
        t = "rotate"
        why = (f"IC 与尾部线边际均枯竭（IC {fam_m:+.3f}"
               + (f"，尾部 {tail_m:+.3f}" if tail_ok else "，尾部线无数据")
               + "）——族已饱和，换方向")
    elif streak >= 3 and plateau:
        t = "refine"
        why = (f"同族试验连续 {streak} 次且最近 20 条试验未超越族历史最佳"
               "达 0.05——族内边际枯竭但存在明确最优载体，转向特性分析型深挖")
    elif pending_rejected is not None:
        t = "rotate"
        why = "上一轮 next_hypothesis 是停笔宣言（已被引擎拒收）——当前方向需要更换"
    elif frozen:
        t = "rotate"
        why = "同一句 next_hypothesis 已冻结 ≥2 轮——停摆模式，强制换方向"
    elif accepted_n >= 3 and accepted_n % 3 == 0:
        t = "query"
        why = f"已入册 {accepted_n} 个因子——里程碑正交性审计"
    elif pass_unadmitted >= 3:
        t = "compose"
        why = f"{pass_unadmitted} 个已验证未入册的单因子——组合机会库存充足"
    else:
        t = "continue"
        why = "无异常信号——惯性延续"
    if t == "query":
        # 2026-09-18 死锁修复:query 里程碑 key 若只含 accepted 数——R6 条件
        # (accepted 为 3 的倍数)在 AI 停止后持续满足而 accepted 不再变化 →
        # key 冻结 → 注入器 sameKey 去重永久拦截(实测 stock 账本 36 只恰为
        # 3 的倍数,双线夜挖 stock 会话停摆即此)。并入 agent_rounds 与其他
        # 类型对齐:审计写 trail → round 递增 → key 新鲜,accepted 语义保留。
        key = f"query:{accepted_n}:{agent_rounds}"
    else:
        key = f"{t}:{agent_rounds}:{n_trials}"
    directive = _STRATEGY_DIRECTIVES[t]
    if t == "literature":
        # F5 种子正交化：按台账检索计数轮转方法论语域池，与 agent 当前
        # 假设词汇解耦（相关性检索用自己的词汇查 = 强化先验的回音室）
        seed = _LITERATURE_SEEDS[lit_search_count % len(_LITERATURE_SEEDS)]
        directive = (directive
                     + f"引擎轮转种子查询（与当前族正交）：「{seed}」。"
                       "用它或自己的查询都可以，但论文必须未被标 exhausted，"
                       "且假设来源论文写入 trail 的 papers 字段。")
    if inspiration_reset is not None:
        # D4 冷启动语义（区别于普通 literature 注入）：方向整体作废，
        # pending 已作废不回显，papers 字段对 literature 必填
        directive = (directive
                     + "（灵感重置已确认：当前方向整体作废，上一方向的 "
                       "next_hypothesis 不再回显——断链后方向段预算自动重置；"
                       + ("新假设必须来自未被标 exhausted 的新论文，trail 的 "
                          "papers 字段必填" if t == "literature" else
                          "幸存者走标准管线验证，试验将以 origin=random 入账")
                       + "；同族参数微调不算换源。）")
    if stop_kind == "direction_budget":
        directive = (directive
                     + "（本方向段轮次/簇试验预算已耗尽：新因子必须与当前族"
                       "不同源——构造指纹或 IC 谱不同链；断链后预算自动重置，"
                       "同族参数微调不会重置。）")
    return {"type": t, "why": why, "key": key,
            "directive": directive}


def _novelty_corr(sa, sb):
    """两条 light-IC 序列的尾对齐相关(短/缺 → None=按独立计)。"""
    import numpy as _np
    if not isinstance(sa, (list, tuple)) or not isinstance(sb, (list, tuple)):
        return None
    k = min(len(sa), len(sb))
    if k < 20:
        return None
    a = _np.asarray(sa[-k:], dtype=_np.float64)
    b = _np.asarray(sb[-k:], dtype=_np.float64)
    if not (_np.isfinite(a).all() and _np.isfinite(b).all()):
        return None
    ea, eb = a.std(), b.std()
    if ea <= 0 or eb <= 0:
        return None
    return float(((a - a.mean()) * (b - b.mean())).mean() / (ea * eb))


def _novelty_greedy(cands, score_of, series_of, top_k, hist, lam=0.3):
    """P0a 反同质化贪心选种(2026-09-13,生产端标定 λ=0.3)。

    score = rank(score_of) ∈[0,1] + λ·(1 − max|ρ| vs 已选∪历史)。纯
    argmax(λ=0)是 AlphaSAGE 批评的同质化入口;标定:λ=0.3 买 54%/40%
    选种去相关付 10%/7% IC 牺牲。返回 (选中列表, 组内平均两两|ρ|)。
    """
    import numpy as _np
    pool = [c for c in cands if score_of(c) is not None]
    pool.sort(key=lambda c: -score_of(c))
    n = len(pool)
    if n == 0:
        return [], None
    rk = {id(c): (n - 1 - i) / max(1, n - 1) for i, c in enumerate(pool)}
    chosen, chosen_series = [], list(hist or [])
    for _ in range(min(top_k, n)):
        best, bs = None, -9e9
        for c in pool:
            if any(c is x for x in chosen):
                continue
            mx = 0.0
            s = series_of(c)
            for u in chosen_series:
                r = _novelty_corr(s, u)
                if r is not None:
                    mx = max(mx, abs(r))
            v = rk[id(c)] + lam * (1.0 - mx)
            if v > bs:
                bs, best = v, c
        if best is None:
            break
        chosen.append(best)
        s = series_of(best)
        if isinstance(s, (list, tuple)) and len(s) >= 20:
            chosen_series.append(list(s))
    inner = []
    for i in range(len(chosen)):
        for j in range(i + 1, len(chosen)):
            r = _novelty_corr(series_of(chosen[i]), series_of(chosen[j]))
            if r is not None:
                inner.append(abs(r))
    return chosen, (float(_np.mean(inner)) if inner else None)


class Bridge:
    # C2（修正案 A 2026-08-28）：重方法 = 成本乘数/并行池所在——run 目录
    # 保留 out.json；异步作业并发上限（防单线程桥自淹没）
    _HEAVY_METHODS = frozenset({"factor.evaluate_composite",
                                "factor.evaluate_batch",
                                "factor.audit"})
    _MAX_KEPT_RUNS = 20
    _MAX_ASYNC_JOBS = 2  # 退役：仅作旧配置读取兼容展示（见 _slot_capacity）
    _ASYNC_STAGE = {"factor.evaluate_composite": "composite"}
    # R25：batch 不在 _ASYNC_STAGE——批信封不是单因子 diagnosis，走
    # _async_result 的专用后处理分支（_postprocess_batch_result，R24）
    # 层 1（2026-08-31 计算利用 PLAN）：权重并发槽 + 真队列。
    # 权重防超订（R3）：batch 内部还吃 cpu-1 核池——按当量计容量，
    # evaluate/audit=1、composite=2、batch=4，接受轻度超订（墙钟按 jobs
    # 伸缩已兜底）。容量/深度 env 可调（DSH_FACTOR_SLOTS / _QUEUE）。
    _JOB_WEIGHT = {"factor.evaluate": 1, "factor.audit": 1,
                   "factor.evaluate_composite": 2, "factor.evaluate_batch": 4}

    # 效率编译层（2026-09-01）：烟测门只管会执行 factor(env) 的方法
    _SMOKE_METHODS = {
        "factor.check_causality", "factor.evaluate", "factor.evaluate_composite",
        "factor.evaluate_batch", "factor.walk_forward", "factor.noise_test",
        "factor.tail_placebo", "factor.day_perm_test", "factor.flatness_test",
        "factor.audit"}

    def __init__(self, state_root: str | None = None, data_config_path: str | None = None,
                 library_spec: dict[str, Any] | None = None, execution_mode: str = "worker",
                 worker_timeout_ms: int | None = None):
        self.state_root = str(state_root) if state_root else os.environ.get(
            "DSH_FACTOR_MINER_STATE_ROOT", str(Path.cwd() / ".factor-mining"))
        _sr = Path(self.state_root).resolve()
        if _sr.parent == _sr or ".." in Path(self.state_root).parts:
            raise BridgeError(-32602,
                              f"stateRoot 不能是文件系统根目录（收到 {self.state_root}）")
        Path(self.state_root).mkdir(parents=True, exist_ok=True)
        # 文件约定优先（convention over registration）：未显式给出 data_config_path 时，
        # 约定路径 = stateRoot/data-config.json —— 配置即文件，进程重启后状态从磁盘重建。
        self.data_config_path = str(data_config_path) if data_config_path else str(
            Path(self.state_root) / "data-config.json")
        self.data_config: DataConfig | None = None
        self._config_mtime_ns: int | None = None
        self._config_error: str | None = None
        self.library_spec = library_spec or {}
        self.library = UserLibrary(self.library_spec)
        self._library_error: str | None = None
        self.envs: dict[str, Any] = {}
        self.env_quality: dict[str, Any] = {}
        self.minute_features: dict[str, Any] = {}
        # ---- 纪律层状态（批次1a）----
        self._receipts: dict[str, dict[str, Any]] = {}   # receipt_id -> 关键数字（防编造入册）
        self._causality_cache: dict[str, dict[str, Any]] = {}  # source_hash -> verdict
        self._fp_cache: dict[str, tuple] = {}             # source_hash -> (sig_idx, F采样) 供证伪入池
        # v3（2026-08-21）选择运气采样器：CRN 状态跨调用持久——增量条件采样
        # O(M²)/新试验；trail 内容寻址（keys 前缀失配 → 全量重建）
        self._luck = _LuckSampler()
        # 批次1(2026-09-13 方案B 影子):机制层(registered)独立采样器——
        # 与全局 CRN 状态分离,层内子集的 E[max|X|] 增量维护
        self._luck_mech = _LuckSampler()
        self._pool_instance = None                                 # MemoryPool 惰性初始化（需要 state_root）
        self._on_progress = None                          # main() 注册：进度 notification 回调
        # 执行安全（DESIGN §10）：默认 worker 子进程隔离；in_process 仅受信调试
        self.execution_mode = execution_mode if execution_mode in ("worker", "in_process") else "worker"
        # 墙钟 base（2026-08-31 计算利用 PLAN R1）：300s→600s——全A个股面板
        # 单评 150-322s 实测，300s 线会杀掉合法慢因子；jobs 伸缩公式不变。
        # env DSH_FACTOR_MINER_WORKER_TIMEOUT_MS 可覆盖（部署侧调优用）
        self.worker_timeout_ms = int(
            worker_timeout_ms
            or int(os.environ.get("DSH_FACTOR_MINER_WORKER_TIMEOUT_MS") or 0)
            or 600_000)
        # 效率编译层（2026-09-01）：α 烟测门 + 自动 njit + LLM 重写。
        # 烟测门 fail-open（门自身任何异常放行——新层绝不破坏既有流程）；
        # 重写层在不可用（无 key/显式关）时静默降级为纯拒绝消息。
        self._smoke_enabled = os.environ.get("DSH_FACTOR_SMOKE", "1") != "0"
        self._smoke_min_cells = int(
            os.environ.get("DSH_FACTOR_SMOKE_MIN_CELLS") or 200_000)
        self._smoke_cache: dict[tuple, dict] = {}   # (src_fp, env_fp|cells) -> 原始烟测
        # 并发复审（2026-09-01）F1/F2 修复：同 key 烟测在途事件表（后到者
        # 等待先来者结果，不重复 spawn）；烟测/验证子进程并发信号量（burst
        # 下瞬时 smoke worker 数有界，防权重核算外的进程超订）
        self._smoke_inflight: dict[tuple, threading.Event] = {}
        self._smoke_spawn_sem = threading.Semaphore(2)
        self._rewrite_inflight: set[str] = set()    # source_fp -> 重写作业在途
        self._optimizer_lock = threading.Lock()
        self._optimizer_cache: dict | None = None   # 惰性加载 optimizer_cache.json
        # C2（修正案 A 2026-08-28）：重方法异步作业——单线程桥不被长计算
        # 绑架；层 1（2026-08-31 计算利用 PLAN）升级为权重槽 + 真队列：
        # 槽满不再拒绝而是入队，dispatch 永远秒回
        self._async_lock = threading.Lock()
        self._slot_capacity = max(1, int(os.environ.get("DSH_FACTOR_SLOTS") or 6))
        self._queue_capacity = max(0, int(os.environ.get("DSH_FACTOR_QUEUE") or 20))
        self._async_queue = collections.deque()   # 待启动作业（层 1）
        self._async_weight_active = 0
        self._async_lane_last = None              # 层 3 公平出队游标
        self._async_dedup: dict[Any, str] = {}    # 层 2：在途键 → run_id
        self._async_dedup_rev: dict[str, Any] = {}
        # 提交即登记（job.json 由 _async_submit 入队即落盘——R5 消灭提交/
        # 轮询竞态；此字典降级为内存快查）
        self._async_jobs: dict[str, str] = {}
        # 启动收尸（R6）：上一进程留下的 queued/running 作业标 failed
        self._reap_orphan_jobs()
        if Path(self.data_config_path).exists():
            # 启动失败不致命：保留 configError 上报，服务保持可用（状态可自愈）。
            self._reload_config_file()

    # ---- 纪律层（批次1a）：诊断包装 / receipt / 引擎trail / 对表 / 指纹 ----
    def _pool(self):
        # NOTE: 缓存字段是 self._pool_instance（避免与方法同名互相遮蔽）
        if self._pool_instance is None:
            from .factor.memory_pool import MemoryPool
            self._pool_instance = MemoryPool(self.state_root)
        return self._pool_instance

    def _env_full_fingerprint(self, env_id: str) -> str | None:
        """环境三元组指纹（数据文件 + 实际生效口径 + 引擎版本）。
        口径优先用已构建 env 的 calibration（含自适应回填的分界），未加载时退 spec 声明。"""
        try:
            spec = self.data_config.environments.get(env_id)
            if spec is None:
                return None
            env = self.envs.get(env_id)
            cal = env.calibration if env is not None else None
            return full_fingerprint(spec.source.path,
                                    cal if cal is not None else spec.calibration)
        except Exception:
            return None

    def _landscape_fingerprint_status(self, landscape, env_id: str) -> str:
        """null 地形指纹比对：'match' | 'mismatch' | 'legacy_no_field'。

        指纹硬门（2026-08-19）的统一判据：地形写盘时绑定的 env_fingerprint
        必须等于当前环境指纹（数据文件+口径+引擎版本三元组，纯复用
        _env_full_fingerprint——trail_engine 条目一直在用）。旧版地形无
        字段 → legacy_no_field，与 mismatch 同判无效（宁可保守：重跑一次
        null 校准，几分钟，换永久绑定；否则换数据集的洞一直开着）。
        """
        if not isinstance(landscape, dict):
            return "mismatch"
        fp = landscape.get("env_fingerprint")
        if fp is None:
            return "legacy_no_field"
        try:
            cur = self._env_full_fingerprint(self._resolve_env_id(env_id))
        except Exception:
            cur = self._env_full_fingerprint(env_id)
        return "match" if fp == cur else "mismatch"

    def _progress(self, label: str, done: int, total: int):
        if self._on_progress is not None:
            try:
                self._on_progress({"label": label, "done": done, "total": total})
            except Exception:
                pass

    def _make_receipt(self, result: dict[str, Any]) -> str:
        """H2 receipt：对诊断的关键数字做指纹缓存——registry_submit 校验用（防编造）。

        2026-08-25 扩展：deflated_train 的充分统计量 (sr_hat/skew/kurt/n_obs)
        一并缓存——submit 的 p 重算信的就是这四个数，receipt 不覆盖它们
        等于门只锁门框不锁门（手构高 sr_hat 可直推 acceptance）。"""
        import hashlib as _h
        keys = ("ic_ir_train", "ic_mean_train", "ic_n_train", "ic_ir", "ic_mean", "ic_n", "verdict")
        rec: dict[str, Any] = {k: result.get(k) for k in keys if k in result}
        dp = result.get("deflated_train")
        if isinstance(dp, dict):
            stats = {k: dp.get(k) for k in ("sr_hat", "skew", "kurt", "n_obs")
                     if dp.get(k) is not None}
            if stats:
                rec["deflated_stats"] = stats
        payload = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str)
        rid = _h.sha256(payload.encode("utf-8")).hexdigest()[:12]
        self._receipts[rid] = rec
        # 有界：只保留最近 4096 个 receipt（512 太小——50 轮×10 因子即触顶，
        # 被挤掉的 receipt 会让诚实提交降级 verified:false）
        if len(self._receipts) > 4096:
            self._receipts = dict(list(self._receipts.items())[-4096:])
        return rid

    def _verify_receipt(self, diagnosis: dict[str, Any]) -> bool | None:
        """内部三态：True（验证通过）/ False（声称有 receipt 但不匹配）/ None（无 receipt
        或缓存丢失，降级 unverified）。出口（submit 返回的 receipt_verified）一律
        bool 化（None→False）——下游 `is False` 严格检查不漏「无 receipt 手构」场景
        （2026-08-18 独立审计 F-A1-SEM 修正）。

        deflated_stats（2026-08-25）：缓存的充分统计量与 diagnosis.deflated_train
        逐位比对——不匹配 = 编造/删改痕迹（False，submit 侧拒收）。
        receipt 缓存丢失（bridge 重启）→ None 降级不冤枉；权威数字另有
        trail_engine.dsr_stats 兜底（见 _authoritative_dsr_stats）。"""
        rid = diagnosis.get("_receipt")
        if not rid:
            return None
        rec = self._receipts.get(str(rid))
        if rec is None:
            return None  # bridge 重启后缓存丢失：同样降级，不冤枉
        flat = {k: v for k, v in rec.items() if k != "deflated_stats"}
        ok = all(diagnosis.get(k) == v for k, v in flat.items() if v is not None)
        ds = rec.get("deflated_stats")
        if ok and isinstance(ds, dict) and ds:
            ddp = diagnosis.get("deflated_train")
            ok = isinstance(ddp, dict) and all(ddp.get(k) == v for k, v in ds.items())
        return ok

    def _inspiration_origin(self, source_hash: str | None, lane: str) -> str:
        """WS-C 灵感源判定（2026-09-01 规划书）：证据强度排序——

        - random：source_hash 命中本线 explore 幸存者哈希台账（生成器
          返回的原文直进管线；agent 改造过的哈希不同，如实回落——
          改造即假设驱动）
        - literature：本线最新叙事 trail 条目 papers 非空（写入口已
          校验合法 arxiv_id 列表——文献驱动假设的可审计标记；族内
          变体继承标记，直到新的无 papers 叙事条目出现）
        - hypothesis：缺省（含旧条目无键的回读口径）
        判定失败静默回落 hypothesis（标注不阻断账本）。"""
        try:
            if source_hash:
                bucket = ((read_mining_state(self.state_root).get("lanes") or {})
                          .get(lane) or {})
                if source_hash in set(bucket.get("explore_hashes") or []):
                    return "random"
            own = own_entries(read_json_list("trail", self.state_root), lane)
            if own and isinstance(own[-1], dict) and own[-1].get("papers"):
                return "literature"
        except Exception:
            pass
        return "hypothesis"

    def _append_engine_trail(self, env_id: str, source_hash: str, stage: str,
                             result: dict[str, Any], suspects: dict[str, Any] | None):
        """引擎层强制 trail（evaluate 自动记录硬事实，按 (hash, stage, horizon) 去重更新）。
        agent 的叙事层 trail（record_trail）与此分文件存储，trail_summary 合并。"""
        self._append_engine_trail_batch(env_id, [(source_hash, stage, result, suspects)])

    def _append_engine_trail_batch(self, env_id: str, items: list[tuple]):
        """批量写 trail_engine（2026-08-19 A1；v2 2026-08-20 horizon + 包络）。

        单条版逐成员调用 = 每成员整文件重写（O(M²) IO）；批量版读一次、
        过滤+追加 M 条、原子写一次。items: [(source_hash, stage, result, suspects)]。
        - sketch 处理（A2）：result 里的 ic_series_train 摘出存 entry。
        - v2 试验单元：entry 带 horizon（result["horizon"]，evaluate 盖章）；
          去重键 (hash, stage, horizon)——旧条目无 horizon 字段按主 horizon
          归位（重评主 horizon 补 sketch 更新，不新增）。
        - v2 单调包络：写盘时刻对合并后全量 trial 算一次谱 N_eff，盖章
          n_eff_at_write——后续灌水（灌高相关变体压低谱值）被包络封死。
          盖章失败不阻断写盘（少一章由下次写入补上）。"""
        try:
            p = Path(self.state_root) / "trail_engine.json"
            entries = []
            if p.exists():
                entries = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            fp = self._env_full_fingerprint(env_id)
            main_h = self._main_horizon(env_id)
            # 本线 lane（方案 A 2026-08-31）：条目打标 + 断链检测只对
            # 本线尾部——双线交错时外线条目既不接链也不截断本线链
            my_lane = current_lane()
            new_entries = []
            # 断链检测（2026-08-25 arc 化；2026-08-31 lane 化）：新试验
            # 不接本线尾部链 = 机械换向事件 → 本线方向段轮次（arc_rounds）
            # 归零。簇试验计数（cluster_trials=streak）读时按线过滤，
            # 断链自然断——两个计数器在同一事件上重置，判定谓词同为
            # _entries_linked。
            chain_broke = False
            for source_hash, stage, result, suspects in items:
                sketch = None
                horizon = None
                if isinstance(result, dict):
                    h = result.get("horizon")
                    if isinstance(h, (int, float)) and int(h) > 0:
                        horizon = int(h)
                    s = result.get("ic_series_train")
                    if isinstance(s, (list, tuple)) and len(s) > 0:
                        # 截尾 256 点（均匀采样）：防御性上限，正常 train IC 序列
                        # 远小于此；round 4 位足够聚类相关计算
                        if len(s) > 256:
                            idx = np.linspace(0, len(s) - 1, 256).astype(int)
                            s = [s[i] for i in idx]
                        # +0.0 归一化 -0.0（IEEE: -0.0+0.0=+0.0）——DSH 的
                        # lossless JSON 检查拒绝 -0（Object.is(-0)），round
                        # (-0.00004,4)=-0.0 曾让 factor_trail_summary 整体
                        # 报错（生产 trail_engine 实测 66 个 -0.0 字面量）
                        sketch = [round(float(x), 4) + 0.0 for x in s]
                origin = self._inspiration_origin(source_hash, my_lane)
                entry = {
                    "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                    "envId": env_id, "source_hash": source_hash, "stage": stage,
                    "horizon": horizon,
                    "lane": my_lane,
                    # WS-C 灵感源标注（2026-09-01）：random/literature/
                    # hypothesis——归因分析可区分假设驱动 vs 随机幸存 vs
                    # 文献迁移；旧条目无此键读作 hypothesis
                    "origin": origin,
                    # S4 署名（2026-09-04 探索相似性防线）：authored =
                    # 非 random——审计教训 stage≠署名（batch 是工具通道
                    # 非生成器），origin 的 explore_hashes 台账才是真判据。
                    # 旧条目无此键读作 True（hypothesis/literature 均手写）
                    "authored": origin != "random",
                    "fingerprint": fp,
                    "engine_version": __version__,
                    "ic_ir": result.get("ic_ir_train", result.get("ic_ir")) if isinstance(result, dict) else None,
                    "ic_mean": result.get("ic_mean_train", result.get("ic_mean")) if isinstance(result, dict) else None,
                    "verdict": result.get("verdict") if isinstance(result, dict) else None,
                    "red_flags": result.get("red_flags", []) if isinstance(result, dict) else [],
                    # P4 Tier 3：效率入账（CPU 口径——并行下墙钟失真）。
                    # 族均成本可从 trail 聚合；D3c：只展示不参与升级阶梯
                    "cpu_s": ((result.get("perf") or {}).get("cpu_s")
                              if isinstance(result, dict)
                              and isinstance(result.get("perf"), dict) else None),
                    "suspects": suspects,
                    "ic_series_sketch": sketch,
                    # 成分血缘指纹（2026-08-25 CMF 事故）：合成因子的 IC
                    # 序列与核心解相关骗过链判定；构造指纹让同族磨种
                    # 保持链连续，3/6/9 升级正常触发强制换向
                    "construction_fp": (result.get("_construction_fp")
                                        if isinstance(result, dict) else None),
                    # 尾部三件套计算层（2026-08-25）：spread_ir/tail_ic/
                    # spread_ic_corr/topn placebo——Phase 5 tail 账本
                    # （键 source_hash×horizon×K）的数据源；自动计算=
                    # 自动计数
                    "tail": (result.get("tail")
                             if isinstance(result, dict) else None),
                    # DSR 充分统计量（2026-08-25 防编造）：submit 重算 p
                    # 的权威数字源。receipt 缓存随进程丢失，trail 是盘上
                    # 硬事实——diagnosis 自报的 sr_hat 只在无 trail 条目时
                    # 兜底（见 _authoritative_dsr_stats）
                    "dsr_stats": (
                        {k: (result.get("deflated_train") or {}).get(k)
                         for k in ("sr_hat", "skew", "kurt", "n_obs")}
                        if isinstance(result, dict)
                        and isinstance(result.get("deflated_train"), dict)
                        and (result.get("deflated_train") or {}).get("sr_hat") is not None
                        else None),
                }
                norm_new = horizon if horizon is not None else main_h

                def _same_trial(e) -> bool:
                    if not isinstance(e, dict):
                        return False
                    if e.get("source_hash") != source_hash or e.get("stage") != stage:
                        return False
                    eh = e.get("horizon")
                    if eh is None and main_h is not None:
                        eh = main_h
                    return eh == norm_new

                prior = [e for e in entries if _same_trial(e)]
                entries = [e for e in entries if not _same_trial(e)]
                if prior:
                    # R26（2026-08-31 隔离复审）：重评已有试验（含跨线重评）
                    # ——条目保留首创线的 lane（B 线重评不改写 A 线的视图/
                    # 链尾），且重评不是新试验、不参与断链判定（否则 B 线
                    # 重抽 A 线尾部条目后 A 续写会伪断链、arc 误归零——
                    # 隔离评审可复现实证）。计价层 (hash,stage,horizon) 全局
                    # 去重语义不变：同试验全局仍只计一次
                    entry["lane"] = (prior[-1].get("lane") if isinstance(prior[-1], dict)
                                     and prior[-1].get("lane") else my_lane)
                    own_tail = None
                else:
                    # 断链只对本线判定：全局尾部是外线条目时不参与——否则
                    # 双线交错每条新试验都「断链」，arc 归零风暴 + 预算失效
                    own_tail = next((e for e in reversed(entries)
                                     if entry_lane(e) == my_lane), None)
                if own_tail is not None and isinstance(own_tail, dict) \
                        and not _entries_linked(own_tail, entry):
                    chain_broke = True
                entries.append(entry)
                new_entries.append(entry)
            # v3 单调包络盖章（σ 单位）：写盘时刻的 E[max|X|]（尾对齐 R +
            # 跨 horizon 先验）。CRN 采样器保证估计量天然单调，章防 rebuild
            # 重估噪声与跨版本回退。谱 n_eff_at_write 章保留为遥测。
            if new_entries:
                try:
                    prior = self._cross_h_prior_loaded(env_id)
                    stats = self._n_eff_from_entries(entries, None, None,
                                                     cross_h_prior=prior,
                                                     main_horizon=main_h,
                                                     sampler=self._luck)
                    for e in new_entries:
                        # ceil: stamp >= raw envelope -> gate recomputed from disk is
                        # strictly monotonic (round truncates down 5e-5)
                        e["bar_sigma_at_write"] = math.ceil(
                            float(stats["bar_sigma"]) * 1e4) / 1e4
                        e["n_eff_at_write"] = round(float(stats["n_eff"]), 4)
                except Exception:
                    pass
            # 原子写（tmp + os.replace）：中途崩溃不留半截 JSON
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(str(tmp), str(p))
            if chain_broke:
                # arc 归零在写盘成功之后（写失败不断链）；归零失败不阻断
                # trail（下次断链或 reset 补——arc 少归一次零只会更保守）。
                # 只归本线（方案 A）：A 线换向不解锁 B 线预算
                try:
                    arc_rounds_bump(self.state_root, reset=True, lane=my_lane)
                except Exception:
                    pass
        except Exception as e:
            # trail 是「不可瞒报」防线——写入失败必须可见（stderr 进 client 的 64KB 环形缓冲）
            try:
                import sys as _sys
                _sys.stderr.write(f"[trail_engine] 写入失败: {type(e).__name__}: {e}\n")
            except Exception:
                pass

    def _wrap_diagnosis(self, env_id: str, source: str, stage: str,
                        result: Any) -> Any:
        """evaluate 类结果的统一后处理：_meta（指纹/版本/receipt）+ 双池对表 + 引擎 trail。"""
        if not isinstance(result, dict) or result.get("error"):
            return result
        # envId fallback 解析（primary -> 唯一环境），保证指纹/溯源用真实环境
        try:
            env_id = self._resolve_env_id(env_id)
        except Exception:
            pass
        source_hash = source_fingerprint(source)
        # 构造指纹（v5 血缘，2026-08-25）：存 result 内部字段，trail append
        # 时摘出为 construction_fp
        result["_construction_fp"] = _construction_fingerprint(source)
        result["_meta"] = {
            "fingerprint": self._env_full_fingerprint(env_id),
            "engine_version": __version__,
            "source_hash": source_hash,
            "receipt": self._make_receipt(result),
        }
        # receipt 同时放顶层：diagnosis 整体提交时自然携带（H2 校验读取 _receipt）
        result["_receipt"] = result["_meta"]["receipt"]
        suspects = None
        try:
            env = self.envs.get(env_id)
            if env is not None:
                import numpy as _np
                fn = self._compile_factor(source)
                F = _np.asarray(fn(env), dtype=_np.float64)
                sig_idx = _np.arange(0, env.T, env.calibration.sample_step)
                self._fp_cache[source_hash] = (sig_idx, F)
                # 有界（大面板下每因子采样矩阵可达 ~10MB，无上限长会话内存线性涨）
                if len(self._fp_cache) > 128:
                    self._fp_cache = dict(list(self._fp_cache.items())[-128:])
                suspects = self._pool().check(F, sig_idx, source)
                result["duplicate_suspect"] = suspects.get("duplicate_suspect")
                result["method_suspect"] = suspects.get("method_suspect")
                # 强池准入：pass/needs_review 交给 should_admit 语义（提醒入池，弱踢弱）
                if result.get("verdict") in ("pass", "needs_review"):
                    ir = result.get("ic_ir_train", result.get("ic_ir"))
                    if isinstance(ir, (int, float)) and _np.isfinite(ir):
                        self._pool().offer_active(
                            F, sig_idx, source, name=f"{stage}:{source_hash[:8]}",
                            ic_ir=float(ir), fingerprint=result["_meta"]["fingerprint"])
        except Exception as e:
            # 对表/入池失败不阻断评估主结果，但必须可见——静默吞错曾酿成真实事故
            result["_pool_error"] = f"{type(e).__name__}: {e}"[:200]
        # 源码外挂库（2026-08-29）：每次入账的试验源码按 hash 入库——
        # trail 条目的 source_hash 即键，账本不膨胀、历史可回溯。幂等
        # 无锁；失败可见（与 _pool_error 同款遥测）不阻断账本
        try:
            store_factor_source(source, self.state_root)
        except Exception as e:
            try:
                result["_source_store_error"] = f"{type(e).__name__}: {e}"[:150]
            except Exception:
                pass
        # 写锁（并行 P0）：trail_engine 是读全量→过滤→追加→重写，多进程
        # 并发评估（多会话/batch 池化）下无锁会互相覆盖丢试验条目
        with state_write_lock(self.state_root):
            self._append_engine_trail(env_id, source_hash, stage, result, suspects)
        # A2：sketch 已由 _append_engine_trail 摘出存 trail；agent 可见
        # schema 保持不变（ic_series_train 不外泄，registry 不膨胀）
        result.pop("ic_series_train", None)
        # WS3（2026-08-25）：test 区结果的 train 对照——反查 trail_engine
        # 该 source_hash 的 train 尾块，附 tail_decay（报告数字，不自动
        # 红旗：衰减多少算失败是校准问题，先给数字；翻号红旗等有样本
        # 后定阈再上）。test 条目本身已带尾块但被两道防线挡在计价外。
        if stage == "test":
            try:
                result["tail_decay"] = self._tail_decay_lookup(
                    env_id, source_hash, result)
            except Exception as e:
                result["tail_decay"] = {"error": f"{type(e).__name__}: {e}"[:120]}
                try:
                    import sys as _sys
                    _sys.stderr.write(
                        f"[tail_decay] 对照失败: {type(e).__name__}: {e}\n")
                except Exception:
                    pass
        # v2（2026-08-20）：trail 写盘后就地重算 deflated_train——与 batch/submit
        # 同一口径。provisional n_eff（evaluate 前算，pending 无 sketch）与
        # 最终值（sketch 已入 trail）有微小差；重算消掉它，agent 在单评估
        # 视图看到的 p 与随后 submit 的门控 p 一致（决策支持一致性）。
        # verdict/red_flags 仍是 provisional 口径（evaluate 内部算）——接受门
        # （submit 的 passes_acceptance）以重算后的 p 为准，口径不放松。
        if stage == "development":
            dp = result.get("deflated_train")
            if isinstance(dp, dict) and dp.get("sr_hat") is not None:
                h_eff = result.get("horizon")
                if isinstance(h_eff, (int, float)) and int(h_eff) > 0:
                    h_eff = int(h_eff)
                else:
                    h_eff = None
                stats, trail_std = self._trial_stats(source_hash, h_eff, env_id)
                pool_std, detector = self._resolve_pool_std(env_id, trail_std, h_eff)
                bar = stats["bar_sigma"]
                p_new = _dsr_p_from_stats(dp.get("sr_hat"), dp.get("skew"),
                                          dp.get("kurt"), dp.get("n_obs"),
                                          bar, pool_std)
                result["deflated_train"] = {**dp, "p": p_new,
                                            "n_trials": float(stats["n_trials"]),
                                            "n_eff": float(stats["n_eff"]),
                                            "bar_sigma": float(bar),
                                            "pool_std": pool_std,
                                            "p_family": (self._luck.p_fw(
                                                abs(float(dp["sr_hat"])) / pool_std)
                                                if pool_std else None),
                                            "nu": stats.get("nu"),
                                            "signal_detector": detector,
                                            "recomputed_at_trail": True}
        # 推理日志镜像（构造面 v7 2026-09-12）：development 评估完成后按
        # source_hash 机械关联本线 registered 预期条目——引擎只做并置
        # （预期原文 vs 实测关键数字），不判定；结算由模型 update 写。
        # journal 任何失败不阻断评估（日志是解释层，评估是事实层）。
        if stage == "development":
            try:
                from . import journal as _journal
                _journal.note_eval(self.state_root)
                _dp = result.get("deflated_train")
                _m = _journal.mirror_hook(
                    self.state_root, source_hash,
                    {"ic_ir": result.get("ic_ir_train", result.get("ic_ir")),
                     "verdict": result.get("verdict"),
                     "deflated_p": (_dp.get("p") if isinstance(_dp, dict)
                                    else None),
                     "red_flags": result.get("red_flags") or None})
                if _m:
                    result["journal_mirror"] = _m
                    # 方案B 批次1(2026-09-13):注册台账落盘——机制层成员资格
                    # + 时机证据(registered_ts ≤ eval_ts)+ 复杂度指纹。
                    # 独立于 journal 生命周期(reset 不动),jsonl 追加
                    try:
                        import ast as _ast
                        _tree = _ast.parse(source)
                        _ops = sum(1 for _n in _ast.walk(_tree)
                                   if isinstance(_n, _ast.Call))
                        _d = 0

                        def _dep(n, k):
                            nonlocal _d
                            _d = max(_d, k)
                            for _c in _ast.iter_child_nodes(n):
                                _dep(_c, k + 1)
                        _dep(_tree, 0)
                        _rows = [{
                            "ts_registered": e.get("registered_ts"),
                            "ts_eval": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "source_hash": source_hash,
                            "journal_id": e.get("id"),
                            "ops": _ops, "depth": _d,
                            "journal_lane": _journal.resolve_journal_lane(),
                        } for e in _m.get("entries", [])]
                        _lp = Path(self.state_root) / "registered_ledger.jsonl"
                        with state_write_lock(self.state_root):
                            with open(_lp, "a", encoding="utf-8") as _f:
                                for _r in _rows:
                                    _f.write(json.dumps(
                                        _r, ensure_ascii=False) + "\n")
                    except Exception as _le:
                        try:
                            sys.stderr.write(
                                f"[registered_ledger] 落盘失败(不阻断评估): {_le}\n")
                        except Exception:
                            pass
                # 方案B 批次1 影子(2026-09-13):注册候选附机制层价——只算只报,
                # 门不变(翻转待影子期分布审查)。α_layer=0.0253(层间 Šidák)。
                # 放在台账落盘之后:首评即有层成员资格(镜像命中=注册证据)
                try:
                    if source_hash in self._registered_hashes():
                        _dp = result.get("deflated_train")
                        _h = result.get("horizon")
                        _h = int(_h) if isinstance(_h, (int, float)) and _h > 0 else None
                        if isinstance(_dp, dict) and _dp.get("sr_hat") is not None:
                            st_m, tsd_m = self._trial_stats(
                                source_hash, _h, env_id, stratum="mechanism")
                            pool_m, _det = self._resolve_pool_std(env_id, tsd_m, _h)
                            if pool_m:
                                p_m = _dsr_p_from_stats(
                                    _dp.get("sr_hat"), _dp.get("skew"), _dp.get("kurt"),
                                    _dp.get("n_obs"), st_m["bar_sigma"], pool_m)
                                _dp["shadow_stratum"] = {
                                    "p_mechanism": p_m,
                                    "bar_sigma_mechanism": float(st_m["bar_sigma"]),
                                    "n_trials_mechanism": int(st_m["n_trials"]),
                                    "pool_std": pool_m,
                                    "alpha_layer": 0.0253,
                                    "note": ("影子口径(批次1):journal 预注册候选的机制层"
                                             "价——M 按台账注册试验计。当前不进门;翻转"
                                             "由影子期分布审查拍板"),
                                }
                except Exception as _se:
                    try:
                        sys.stderr.write(f"[shadow_stratum] 失败(不阻断): {_se}\n")
                    except Exception:
                        pass
            except Exception as _e:
                try:
                    sys.stderr.write(f"[journal] 镜像失败(不阻断评估): {_e}\n")
                except Exception:
                    pass
        # 自主性停走指令（2026-08-21）：evaluate 响应必带——agent 在评估
        # 后的"总结/停顿"倾向最强，loop.state=running 时必须继续内循环
        try:
            result["loop"] = self._loop_directive()
        except Exception:
            pass
        return result

    # ---- config 文件即事实源 ----
    def _reload_config_file(self) -> bool:
        """从 data_config_path 重读配置。失败保留旧配置并记录 configError。"""
        try:
            cfg = DataConfig.load(self.data_config_path)
        except Exception as e:
            self._config_error = f"{type(e).__name__}: {e}"
            return False
        self.data_config = cfg
        self._config_error = None
        self.envs.clear()
        self.env_quality.clear()
        self.minute_features.clear()
        self._apply_library()
        try:
            self._config_mtime_ns = Path(self.data_config_path).stat().st_mtime_ns
        except OSError:
            self._config_mtime_ns = None
        return True

    def _apply_library(self) -> None:
        """config 顶层 library 段（优先）或插件 library_spec 构建 UserLibrary。
        库加载失败不阻塞配置——记录 libraryError，保留旧库。"""
        lib_spec: dict[str, Any] = dict(self.library_spec)
        if self.data_config is not None and self.data_config.library:
            cfg_lib = dict(self.data_config.library)
            if "type" not in cfg_lib and cfg_lib.get("path"):
                suffix = str(cfg_lib["path"]).lower().rsplit(".", 1)[-1]
                cfg_lib["type"] = {"py": "python_module", "json": "json_registry"}.get(
                    suffix, "expression_list")
            lib_spec.update(cfg_lib)
        try:
            self.library = UserLibrary(lib_spec)
            self._library_error = None
        except Exception as e:
            self._library_error = f"{type(e).__name__}: {e}"

    def _maybe_reload_config(self) -> None:
        """惰性重读：手动编辑约定路径文件后无需重启即生效。"""
        p = Path(self.data_config_path)
        try:
            mtime = p.stat().st_mtime_ns if p.exists() else None
        except OSError:
            return
        if mtime != self._config_mtime_ns:
            self._reload_config_file()

    def _method(self, method: str):
        table = {
            "ping": self._ping,
            "status": self._status,
            "config.load": self._config_load,
            "config.save": self._config_save,
            "config.validate": self._config_validate,
            "data.probe": self._data_probe,
            "data.list_environments": self._data_list_envs,
            "data.load": self._data_load,
            "factor.check_causality": self._factor_check_causality,
            "factor.evaluate": self._factor_evaluate,
            "factor.evaluate_composite": self._factor_evaluate_composite,
            "factor.evaluate_batch": self._factor_evaluate_batch,
            "factor.walk_forward": self._factor_walk_forward,
            "factor.noise_test": self._factor_noise_test,
            "factor.day_perm_test": self._factor_day_perm_test,
            "factor.audit": self._factor_audit,
            "factor.random_generate": self._factor_random_generate,
            "factor.operators": self._factor_operators,
            "factor.null_landscape": self._factor_null_landscape,
            "library.query": self._library_query,
            "library.list": self._library_list,
            "paths.query": self._paths_query,
            "paths.append": self._paths_append,
            "state.mining.get": self._state_mining_get,
            "state.mining.record": self._state_mining_record,
            "state.mining.reset": self._state_mining_reset,
            "state.trail_summary": self._state_trail_summary,
            "state.reset": self._state_reset,
            "registry.get": self._registry_get,
            "registry.submit": self._registry_submit,
            "registry.update": self._registry_update,
            "report.export": self._report_export,
            "book.backfill": self._book_backfill,
            "journal.read": self._journal_read,
            "journal.append": self._journal_append,
            "journal.update": self._journal_update,
            "journal.distill": self._journal_distill,
            "journal.stats": self._journal_stats,
            "standby.view": self._standby_view,
            "standby.combine": self._standby_combine,
            "incubate.enqueue": self._incubate_enqueue,
            "incubate.status": self._incubate_status,
            "incubate.judge": self._incubate_judge,
            "arxiv.search": self._arxiv_search,
        }
        fn = table.get(method)
        if fn is None:
            raise BridgeError(-32601, f"Method not found: {method}")
        return fn

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        # lane（2026-08-31 方案 A）：请求级并行线身份（共享 bridge 下
        # = dsh 会话 id；TS 工具层盖入）。意图层状态（pending/族链/arc
        # 预算）按它隔离，计价层不受影响。单线程 dispatch 内 contextvar
        # 请求生命周期安全；异步重方法的 trail 写入在轮询请求内完成，
        # 同样被覆盖。
        _lane_tok = set_request_lane(resolve_request_lane(params))
        params = strip_lane_params(params or {})
        # journal 线名(2026-09-12 双入口修复):DSH 会话 lane=UUID 会把
        # 日志切成每会话一本;journal_lane 由 TS 层从插件配置盖(基础设施
        # 事实,非模型可声明),镜像/读写同链解析。剥离防严格参数校验炸
        from .journal import (reset_request_journal_lane as _rj,
                              set_request_journal_lane as _sj)
        _jl = params.pop("journal_lane", None)
        _jl_tok = _sj(_jl if isinstance(_jl, str) and _jl.strip() else None)
        try:
            fn = self._method(method)
            try:
                return _lossless(fn(params or {}))
            except DataError as e:
                # 用户数据/配置错误统一映射到域错误码（调用方无需感知异常类型）
                raise BridgeError(-32002, str(e)) from None
            except BridgeError as e:
                # W3（2026-08-26 规划书）：infra 失败（-32005 worker 超时/无输出）
                # 入台账——loop 指令超时纠偏声道的数据源。纯附加遥测：异常原样
                # re-raise，异常语义零变化
                if e.code == -32005:
                    self._record_infra_failure(method)
                raise
        finally:
            _rj(_jl_tok)
            reset_request_lane(_lane_tok)

    def _record_infra_failure(self, method: str) -> None:
        """-32005 基础设施失败台账（stateRoot/tool_failures.json，有界 100、原子写）。

        写失败绝不阻断原异常（台账是遥测不是事务）。"""
        try:
            p = Path(self.state_root) / "tool_failures.json"
            entries = []
            if p.exists():
                entries = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            entries.append({"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                            "method": method, "code": -32005})
            entries = entries[-100:]
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(str(tmp), str(p))
        except Exception:
            pass

    def _recent_infra_failures(self, window_secs: float = 1800.0) -> list[dict]:
        """最近 window_secs 内的 -32005 台账条目（读失败/无文件 = 空）。"""
        try:
            from datetime import datetime, timedelta
            p = Path(self.state_root) / "tool_failures.json"
            if not p.exists():
                return []
            entries = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                return []
            cutoff = datetime.now() - timedelta(seconds=window_secs)
            out = []
            for e in entries:
                if not isinstance(e, dict) or e.get("code") != -32005:
                    continue
                try:
                    ts = datetime.fromisoformat(str(e.get("ts", "")))
                except ValueError:
                    continue
                if ts >= cutoff:
                    out.append(e)
            return out
        except Exception:
            return []

    # ---- system ----
    def _ping(self, params):
        return {"pong": True, "version": __version__, "schemaVersion": PROTOCOL_SCHEMA_VERSION}

    def _status(self, params):
        self._maybe_reload_config()
        # 冷启动自描述：未配置/未加载时告诉 agent 下一步该调什么工具。
        next_step = None
        if self.data_config is None:
            next_step = "factor_data_probe"
        elif not self.envs and not self.minute_features:
            next_step = "factor_load_env"
        # 自主性停走指令：status 恒带（冷启动期 state 自然为 running——
        # 引擎未说 may_stop 之前，挖掘会话不停）
        try:
            loop = self._loop_directive()
        except Exception:
            loop = None
        result = {
            "ready": True,
            "version": __version__,
            "schemaVersion": PROTOCOL_SCHEMA_VERSION,
            "stateRoot": self.state_root,
            "dataConfigPath": self.data_config_path,
            "dataConfigured": self.data_config is not None,
            "configError": self._config_error,
            "nextStep": next_step,
            "environments": self._data_list_envs({}),
            "libraryConfigured": self.library.configured,
            "libraryError": self._library_error,
            "loadedEnvironments": sorted(self.envs.keys()),
            "executionMode": self.execution_mode,
            "pythonExecutable": sys.executable,
            "bridgeModulePath": str(Path(__file__).resolve()),
            "workerRuns": self._recent_runs(),
        }
        if loop is not None:
            result["loop"] = loop
        return result

    # ---- config ----
    def _config_load(self, params):
        path = params.get("path") or self.data_config_path
        if not path:
            raise BridgeError(-32001, "缺少数据配置路径")
        self.data_config_path = str(path)
        if not self._reload_config_file():
            raise BridgeError(-32002, f"数据配置加载失败: {self._config_error}")
        return {"ok": True, "path": str(path), "environments": self._data_list_envs({})}

    def _config_save(self, params):
        raw = params.get("config")
        if raw is None:
            raise BridgeError(-32602, "缺少 config")
        cfg_dict = _as_dict(raw, "config")
        # 约定路径默认：无 path 时写 stateRoot/data-config.json（文件即事实源）。
        path = params.get("path") or self.data_config_path or str(
            Path(self.state_root) / "data-config.json")
        self.data_config = DataConfig.from_dict(cfg_dict)
        self.data_config.save(path)
        self.data_config_path = str(path)
        self._config_error = None
        # 配置变化 = 环境定义失效：缓存的环境（含旧口径/旧数据）必须重建
        self.envs.clear()
        self.env_quality.clear()
        self.minute_features.clear()
        self._apply_library()
        try:
            self._config_mtime_ns = Path(path).stat().st_mtime_ns
        except OSError:
            self._config_mtime_ns = None
        return {"ok": True, "path": str(path)}

    def _config_validate(self, params):
        raw = params.get("config")
        cfg = _as_dict(raw, "config") if raw is not None else (
            self.data_config.to_dict() if self.data_config else None)
        if cfg is None:
            raise BridgeError(-32001, "没有可校验的数据配置")
        try:
            parsed = DataConfig.from_dict(cfg)
        except Exception as e:
            return {"ok": False, "errors": [str(e)]}
        errors = []
        for env_id, spec in parsed.environments.items():
            try:
                # 轻量校验（文件可达 + 列名匹配）；不读数据体——全量校验在 load_env
                validate_env_lightweight(spec)
            except Exception as e:
                errors.append({"environment": env_id, "error": str(e)})
        # normalized 预览（只读不落盘）：写入后每个环境将长这样
        return {
            "ok": len(errors) == 0,
            "errors": errors,
            "environments": [
                {"id": spec_.id, "label": spec_.label, "sourcePath": spec_.source.path,
                 "mapping": {"symbol": spec_.mapping.symbol, "date": spec_.mapping.date,
                             "close": spec_.mapping.close}}
                for spec_ in parsed.environments.values()
            ],
            "library": parsed.library or None,
            "note": "validate 不写盘；factor_config_write 成功返回 ok:true 时才落盘",
        }

    # ---- data ----
    def _data_probe(self, params):
        path = params.get("path")
        if not path:
            raise BridgeError(-32602, "缺少 path")
        return probe_file(path, layout=params.get("layout"), date_format=params.get("dateFormat"))

    def _data_list_envs(self, params):
        if not self.data_config:
            return []
        return [
            {"id": spec.id, "label": spec.label, "kind": spec.kind,
             "layout": spec.layout, "configured": True}
            for spec in self.data_config.environments.values()
        ]

    def _require_config(self):
        if self.data_config is None:
            raise BridgeError(-32001, "数据配置未提供：请先 factor_data_probe + factor_config_write")

    def _resolve_env_id(self, env_id: str) -> str:
        """环境 ID 解析：存在即用；缺省 primary 不存在且仅有一个环境 → 直接用它
        （消除"示例都用 primary 诱导 agent 把用户环境名改成 primary"的坑）；
        其他不存在的情况报错并列出可用 ID。"""
        envs = self.data_config.environments
        if env_id in envs:
            return env_id
        available = list(envs.keys())
        if (not env_id or env_id == "primary") and len(envs) == 1:
            return available[0]
        raise BridgeError(-32002,
                          f"未知环境 '{env_id}'（可用: {', '.join(available) or '无'}）",
                          {"available": available})

    def _require_env(self, env_id: str):
        # 文件即事实源：任何环境访问前先检查约定路径文件是否被手动编辑过。
        self._maybe_reload_config()
        self._require_config()
        env_id = self._resolve_env_id(env_id)
        spec = self.data_config.environments[env_id]
        if spec.kind == "minute_features":
            if env_id not in self.minute_features:
                mats = normalize_environment(spec)
                self.minute_features[env_id] = mats
            return None, spec, self.minute_features[env_id]
        if env_id not in self.envs:
            mats = normalize_environment(spec)
            self.env_quality[env_id] = mats.get("dataQuality")
            self.envs[env_id] = build_factor_env(mats, calibration=spec.calibration)
        return self.envs[env_id], spec, None

    def _require_panel_env(self, env_id: str):
        env, spec, _ = self._require_env(env_id)
        if spec.kind != "panel" or env is None:
            raise BridgeError(-32002, f"环境 {env_id} 是 {spec.kind}，不能直接做截面因子评估")
        return env

    def _worker_health(self) -> dict[str, Any]:
        """worker 启动冒烟：--ping 走完整 import 链。结果缓存。

        目的：把环境问题（stdlib backport 污染、缺依赖、坏 numpy）暴露在
        load_env 冷启动，而不是挖到一半 evaluate 才炸。
        """
        if getattr(self, "_worker_health_cache", None) is not None:
            return self._worker_health_cache
        out: dict[str, Any] = {"ok": False}
        try:
            pkg_dir = str(Path(__file__).resolve().parent)
            src_dir = str(Path(pkg_dir).parent)
            env_os = dict(os.environ)
            if "site-packages" not in src_dir:
                env_os["PYTHONPATH"] = src_dir + os.pathsep + env_os.get("PYTHONPATH", "")
            proc = subprocess.run(
                [sys.executable, "-m", "dsh_factor_mining.worker", "--ping"],
                capture_output=True, text=True, timeout=60, env=env_os,
                cwd=str(Path(self.state_root)))
            if proc.returncode == 0:
                out = {"ok": True, "detail": (proc.stdout or "").strip()[:200]}
            else:
                err = (proc.stderr or "")[-800:]
                out = {"ok": False, "error": err}
                # 常见污染特征：第三方 stdlib backport 遮蔽内置库
                if "pathlib" in err or "from collections import" in err:
                    out["hint"] = ("疑似 site-packages 里的 stdlib backport（如 pathlib 1.0.1）"
                                   "遮蔽内置模块：pip uninstall pathlib 后重启会话。"
                                   "PYTHONPATH 已不再注入 site-packages，此污染只会来自环境本身")
        except Exception as e:
            out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._worker_health_cache = out
        return out

    def _data_load(self, params):
        env_id = params.get("envId", "primary")
        env, spec, minute = self._require_env(env_id)
        env_id = spec.id  # 报告实际解析到的环境（含单环境 fallback）
        if spec.kind == "minute_features":
            frame = minute["frame"]
            return {
                "ok": True,
                "envId": env_id,
                "kind": "minute_features",
                "rows": int(len(frame)),
                "columns": list(frame.columns),
            }
        return {
            "ok": True,
            "envId": env_id,
            "kind": "panel",
            "T": env.T,
            "N": env.N,
            "dates": [str(env.dates[0]), str(env.dates[-1])],
            "symbols": len(env.symbols),
            "hasAmount": env.amount is not None,
            "hasListed": env.listed is not None,
            "dataQuality": self.env_quality.get(env_id),
            "calibration": {
                "frequency": env.calibration.frequency,
                "horizon": env.calibration.horizon,
                "cost_bps": env.calibration.cost_bps,
                "execution": env.calibration.execution,
                "limit_up_down_mask": env.calibration.limit_up_down_mask,
                "dev_end": env.calibration.dev_end,
                "sel_end": env.calibration.sel_end,
                "regions_mode": ("auto-60/20/20（保底切分，未经用户确认——建议在 config "
                                 "calibration 里显式写 dev_end/sel_end 并向用户确认）"
                                 if env.calibration.regions_auto else "manual"),
            },
            # worker 冒烟：把环境问题暴露在冷启动（evaluate/causality 全走 worker 子进程）
            "workerHealth": self._worker_health() if self.execution_mode == "worker" else "n/a(in_process)",
        }

    # ---- factor helpers ----
    @staticmethod
    def _compile_factor(source: str):
        if not source or "factor" not in source:
            raise BridgeError(
                -32602,
                "source 必须是定义 `def factor(env)` 的 Python 源码（函数名必须是 "
                "factor——random_generate 返回的 source 已符合此格式，原样使用即可；"
                "手写因子请把函数命名为 factor）")
        ns: dict[str, Any] = {"__name__": "dsh_factor_mining_user_factor"}
        try:
            code = compile(source, "<factor_source>", "exec")
            exec(code, ns)
        except Exception as e:
            raise BridgeError(-32003, f"factor 源码编译失败: {e}")
        fn = ns.get("factor")
        if not callable(fn):
            raise BridgeError(
                -32602,
                "source 编译通过但没有 `def factor(env)` 函数（函数名必须是 factor——"
                "不是 random_factor_N/rfN 之类的变体。random_generate 返回的 source "
                "已符合契约原样使用；手写因子请命名为 factor）")
        return fn

    def _cached_env_npz(self, env, params: dict) -> Path:
        """env.npz 内容寻址缓存（层 0b，2026-08-31 计算利用 PLAN）。

        面板不可变——每次评估重复压缩全面板（个股 root 267MB 产物/0.5GB
        原始，zlib 单线程数十秒）纯浪费且占死 bridge 线程。键 = env 全指纹
        （数据内容+口径+引擎版本；trail 条目每评必算，无新增成本）。
        写序：meta 先、npz 后（npz 存在性即读方 commit 判据）；tmp+replace
        原子，并发写幂等（同字节，store_factor_source 同款）。写成功后清
        非当前指纹文件（R9 防积累，占用中的删除失败跳过）。指纹不可得 →
        回退一次性 npz（行为同旧版，用后即删）。"""
        fp = None
        try:
            env_id = self._resolve_env_id(params.get("envId", "primary"))
            fp = self._env_full_fingerprint(env_id)
        except Exception:
            fp = None
        cache_dir = Path(self.state_root) / "worker_env"
        from .worker import write_env_npz
        if not fp:
            cache_dir.mkdir(parents=True, exist_ok=True)
            npz = cache_dir / f"ephemeral-{os.urandom(6).hex()}.npz"
            write_env_npz(str(npz), env)
            return npz
        # 指纹形如 hash:hash:version——冒号是 Windows 非法文件名字符，清洗
        fp_file = re.sub(r"[^A-Za-z0-9._-]", "-", str(fp))[:48]
        npz = cache_dir / f"{fp_file}.npz"
        meta = cache_dir / f"{fp_file}.npz.meta.json"
        if not npz.exists():
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                tmp_base = cache_dir / f".tmp-{os.urandom(4).hex()}.npz"
                write_env_npz(str(tmp_base), env)
                os.replace(str(tmp_base.with_suffix(
                    tmp_base.suffix + ".meta.json")), str(meta))
                os.replace(str(tmp_base), str(npz))   # npz 最后 = commit
            except Exception:
                # 缓存写失败不阻断评估：退回一次性 npz
                cache_dir.mkdir(parents=True, exist_ok=True)
                ephem = cache_dir / f"ephemeral-{os.urandom(6).hex()}.npz"
                write_env_npz(str(ephem), env)
                return ephem
        # R9 清扫（无条件跑——只在写缓存时扫会让 ephemeral/tmp 无限滞留）：
        # 非当前指纹缓存即时清；ephemeral-*/.tmp-* 可能正被并发 worker 读，
        # 按 1h 年龄清；占用删除失败跳过（下次再清）
        try:
            now = time.time()
            for f in cache_dir.iterdir():
                if f in (npz, meta):
                    continue
                try:
                    name = f.name
                    if name.endswith(".smoke.npz") or name.endswith(".smoke.npz.meta.json"):
                        # 效率编译层（2026-09-01）：烟测子面板缓存——不随
                        # 全量指纹清扫（不同 env 的 smoke npz 按 1 天年龄清）
                        if now - f.stat().st_mtime > 86400:
                            f.unlink()
                        continue
                    if name.startswith(".tmp-") or name.startswith("ephemeral-"):
                        if now - f.stat().st_mtime > 3600:
                            f.unlink()
                    elif f.suffix == ".npz" or name.endswith(".npz.meta.json"):
                        f.unlink()
                except OSError:
                    pass
        except Exception:
            pass
        return npz

    # ---- 效率编译层（2026-09-01）：α 烟测门 + 自动 njit + LLM 重写 ----
    # 设计依据 tmp/numba-spike/spike-report.md（生产面板实测）：
    # 纯标量嵌套循环 njit 36-797x、等价 4/4；算法慢类（T^2*N）njit 负优化、
    # α 拟合可识别；mimo-v2.5 严格等价重写 T3 一发过、谎报 EXACT 被验证兜住。

    def _smoke_env_npz(self, env, params: dict) -> Path | None:
        """小子面板 npz（512x128，~0.5MB）按 env 指纹缓存（worker_env/
        <fp>.smoke.npz）。烟测/重写验证共用；写失败返回 None=门放行。"""
        try:
            env_id = self._resolve_env_id(params.get("envId", "primary"))
            fp = self._env_full_fingerprint(env_id)
        except Exception:
            fp = None
        from .smoke import SMOKE_COLS, SMOKE_ROWS, env_subsample
        from .worker import write_env_npz
        cache_dir = Path(self.state_root) / "worker_env"
        cache_dir.mkdir(parents=True, exist_ok=True)
        if not fp:
            npz = cache_dir / f"ephemeral-smoke-{os.urandom(6).hex()}.npz"
            try:
                write_env_npz(str(npz), env_subsample(env, SMOKE_ROWS, SMOKE_COLS))
            except Exception:
                return None
            return npz
        fp_file = re.sub(r"[^A-Za-z0-9._-]", "-", str(fp))[:48]
        npz = cache_dir / f"{fp_file}.smoke.npz"
        if not npz.exists():
            try:
                sub = env_subsample(env, SMOKE_ROWS, SMOKE_COLS)
                tmp = cache_dir / f".tmp-smoke-{os.urandom(4).hex()}.npz"
                write_env_npz(str(tmp), sub)
                # npz 后写 = commit（write_env_npz 同步产出 meta，一并挪走）
                os.replace(str(tmp.with_suffix(tmp.suffix + ".meta.json")),
                           str(npz) + ".meta.json")
                os.replace(str(tmp), str(npz))
            except Exception:
                return None
        return npz

    def _run_worker(self, method: str, source: str, params: dict[str, Any], env,
                    run_id: str | None = None, run_meta: dict | None = None,
                    async_mode: bool = False,
                    run_dir: Path | None = None,
                    timeout_s: float | None = None) -> Any:
        """在独立 worker 子进程执行用户 factor 代码（隔离 + 超时）。

        序列化 env → npz（层 0b 起为指纹缓存复用）→ 子进程 run_request →
        结构化结果回传。超时杀进程（Windows 用 taskkill /T 杀进程树）；
        结果文件解析失败 = INFRASTRUCTURE 错误。
        run_dir：异步队列路径传入提交时已建好的 worker_runs/<job_id>/
        （job.json 已入队落盘）；同步路径不传 → mkdtemp 如旧。
        timeout_s：可选墙钟覆盖（烟测调用 60s——兼抓死循环实现）。
        """
        # 效率编译层入口钩子（2026-09-01）：①严格等价重写透明换用（所有
        # 执行的单一 choke point——同步/异步/因果门内嵌全覆盖；hash 与
        # registry 语义锚定 agent 的原源码，重写只是引擎侧加速实现，溯源
        # 走 diagnosis.perf.rewrite_provenance）②烟测门（fail-open）。
        if method != "factor.smoke":
            source = self._apply_rewrite(method, source, params)
            self._smoke_gate(method, source, params, env)
        run_root = Path(self.state_root) / "worker_runs"
        run_root.mkdir(parents=True, exist_ok=True)
        if run_dir is not None:
            wdir = Path(run_dir)
            wdir.mkdir(parents=True, exist_ok=True)
        else:
            wdir = Path(tempfile.mkdtemp(dir=str(run_root)))
        # C2（修正案 A）：重方法（composite/batch）成功后保留 run 目录
        # （out.json 可收尸——客户端超时/弃单不丢结果，2026-08-28 事故
        # 的直接止血）；异步作业连失败墓志也保留。轻方法维持即用即删。
        if run_id is None and method in self._HEAVY_METHODS:
            run_id = f"sync-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}"
        keep_run = False
        if run_id:
            try:
                self._job_json_write(wdir, {
                    "job_id": run_id, "method": method, "status": "running",
                    "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "mode": "async" if async_mode else "sync",
                    # R32 补（当夜轨迹审计发现）：此重写用全新 dict——若不
                    # 显式盖 phase 会把因果门留下的 causality_check 抹掉，
                    # 轮询侧又看不到阶段了
                    "phase": "evaluating",
                    "pid": os.getpid(),
                    **(run_meta or {})})
            except Exception:
                run_id = None
        job = None
        try:
            # factor.smoke 用小子面板 npz（bridge 侧已切好），绝不为烟测
            # 序列化全量面板
            npz = (Path(params["_smoke_npz"]) if params.get("_smoke_npz")
                   else self._cached_env_npz(env, params))

            req = {
                "method": method,
                "source": source,
                "npzPath": str(npz),
                "params": {**params, "state_root": self.state_root},
            }
            req_path = wdir / "request.json"
            out_path = wdir / "out.json"
            req_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")

            # 子进程 import 路径：仅源码运行（editable/开发布局，包在 .../src/ 下）时注入
            # PYTHONPATH。固定安装（site-packages）时绝不注入——PYTHONPATH 目录排在
            # stdlib 之前，会把 site-packages 里的第三方 stdlib backport（如 pathlib
            # 1.0.1 的 from collections import Sequence）提升到内置模块前面，worker
            # import pathlib 即崩（2026-08-18 DSH 实测事故根因）。
            pkg_dir = str(Path(__file__).resolve().parent)          # .../dsh_factor_mining
            src_dir = str(Path(pkg_dir).parent)                     # .../src 或 .../site-packages
            # P1：BLAS 单线程（多进程并行防自旋空烧 CPU 伪装成真慢）
            env_os = omp_quiet_env()
            if "site-packages" not in src_dir:
                env_os["PYTHONPATH"] = src_dir + os.pathsep + env_os.get("PYTHONPATH", "")

            # P1/D3：墙钟上限按声明份额伸缩（jobs=DSH_FACTOR_JOBS，默认满核≈base）；
            # 效率编译层：timeout_s 显式覆盖（烟测 60s）
            timeout_s = (float(timeout_s) if timeout_s
                         else effective_wall_timeout(
                             base_s=self.worker_timeout_ms / 1000.0))
            # P3：batch 的 CPU 预算按并行度放大——JOB_TIME/RLIMIT 是进程
            # 树累计（N 个池子进程各烧一份），不放大会被提前击杀；
            # 墙钟上限不放大（并行本就是为了在墙钟内装更多计算）
            cpu_budget = (timeout_s * resolve_jobs()
                          if method == "factor.evaluate_batch" else timeout_s)
            cmd = [sys.executable, "-m", "dsh_factor_mining.worker",
                   "--request", str(req_path), "--result", str(out_path)]
            # P1/D1：Popen 化——超时时刻先读子进程 CPU 秒再做二维归因
            # （进程退出后句柄失效，必须杀之前读）；cwd 隔离：worker 是纯计算
            # 进程，绝不继承 DSH 工作区 cwd（node_modules 循环符号链接事故）
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=env_os, cwd=str(run_root),
                # R31（2026-08-31 生产双实证）：worker 绝不继承宿主控制台——
                # 控制台上的 Ctrl+C/控制事件（宿主重启、宿主侧 pwsh 等待被
                # 取消）会打到同控制台的每个进程，两次击杀在途 worker
                # （rc=0xC000013A STATUS_CONTROL_C_EXIT + KeyboardInterrupt
                # traceback）。CREATE_NO_WINDOW = 无控制台可被打：worker 是
                # 纯计算进程，stdio 全走管道，宿主重启时由 Job Object
                # KILL_ON_JOB_CLOSE 兜底（那是干净的生命周期语义）
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0),
                preexec_fn=posix_limit_preexec(cpu_s=cpu_budget,
                                               mem_bytes=worker_mem_bytes()))
            # P2：OS 级硬限额（父进程被饿死时仍确定性击杀；内存限额防
            # 膨胀因子把整机拖进 swap 饿死所有并行会话）。失败优雅降级
            job = attach_worker_limits(proc, cpu_s=cpu_budget,
                                       mem_bytes=worker_mem_bytes())
            stderr_txt = ""
            try:
                _, stderr_txt = proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                cpu_s = cpu_seconds(proc.pid)
                verdict = starvation_verdict(cpu_s, timeout_s)
                # 杀进程树：Windows 用 taskkill /T；POSIX kill 直接子进程
                if sys.platform == "win32":
                    try:
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                       capture_output=True)
                    except Exception:
                        pass
                else:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.communicate(timeout=15)
                except Exception:
                    pass
                if verdict["class"] == "starved":
                    # D2：饿死 = infra_failure（不是因子的错）——立即上报
                    # 不自动重试（诚实信号：用户应看到机器超订），零写入
                    # 不计账，重试是 agent 的下一步
                    raise BridgeError(
                        -32005,
                        f"worker 超时（墙钟 {timeout_s:.0f}s，实际仅获得 "
                        f"{verdict['cpu_s']:.1f}s CPU，占比 {verdict['ratio']:.0%}）——"
                        "机器超订（并行会话/其他进程挤占 CPU）所致，不是 factor 实现慢。"
                        "本次调用零写入（registry/trail 均未动，不烧名）。"
                        "修法：直接重试同一因子即可（未计入账本）；若反复出现，"
                        "降低并行（设 DSH_FACTOR_JOBS，多会话加总 ≤ 核数）。"
                        "这是基础设施事件——不修实现、不换假设，也不作为对因子的判定。")
                # 真慢（cpu_dense/borderline/数据不可读保守处理）：四要素
                # 错误（W1 2026-08-26）——存活/零写入/根因修法/纪律。
                # 根因按方法分流（2026-08-29 生产事故：audit 超时被硬指
                # 「因子实现慢」，agent 对照 6.5s 的 evaluate 后白重写了
                # 两遍源码——audit 的重头是方法自身的多遍面板机器）。
                if method == "factor.evaluate":
                    cause = (f"最可能根因：factor(env) 单次计算超过 {timeout_s:.0f}s，"
                             "典型是 per-symbol Python 循环；修法 = 向量化"
                             "（df.groupby(\"symbol\") 的 shift/rolling，或 unstack "
                             "到宽表做矩阵运算）。")
                else:
                    cause = (f"根因分流：{method} 的成本 = factor(env) × 方法自身的"
                             "多遍面板机器（audit ≈ 因果探针 + 200 次列置换 + 内嵌"
                             "全量 evaluate；noise_test = m 个噪声世界重算因子）。"
                             "先用 factor.evaluate 的 perf.factor_runtime_s 对照："
                             "evaluate 也慢 → 因子实现慢，向量化；evaluate 快而"
                             "本方法超时 → 是机器遍历成本，勿重写因子——拆小验证"
                             "（减置换/减世界）或 async 提交。")
                cpu_note = (f"（实测 CPU {verdict['cpu_s']:.1f}s / 墙钟 {timeout_s:.0f}s"
                            f"——{verdict['note']}）" if verdict["ratio"] is not None else "")
                raise BridgeError(
                    -32005,
                    f"worker 超时（>{timeout_s:.0f}s）——{method} 的 worker 子进程已终止"
                    "并清理；bridge 本体未受影响，无需等待恢复，可立即重试。本次调用"
                    f"零写入（registry/trail 均未动，不烧名）。{cause}"
                    "这是基础设施事件，不是对因子/研究方向的判定"
                    f"——修实现，不换假设。{cpu_note}")

            if not out_path.exists():
                tail = (stderr_txt or "")[-500:]
                # P2 线索：无输出且提前死亡——可能被 OS 级限额（CPU/内存）击杀
                limit_note = ("｜worker 提前死亡且无输出：若计算/内存量异常庞大，"
                              "已被 OS 级限额击杀（DSH_FACTOR_TIMEOUT / "
                              "DSH_FACTOR_WORKER_MEM_MB）——向量化或简化实现"
                              if job else "")
                # 污染特征快速诊断：stdlib backport 遮蔽（2026-08-18 实测事故，
                # agent 曾为此耗掉整轮对话——在这里给出可执行的修法）
                hint = ""
                if "pathlib" in tail or "from collections import" in tail:
                    hint = ("｜疑似 site-packages 的 stdlib backport（如 pathlib 1.0.1）遮蔽内置库："
                            "pip uninstall pathlib 后重启会话")
                # R32（2026-08-31 生产双实证）：0xC000013A=STATUS_CONTROL_C_EXIT
                # ——worker 被宿主控制台事件击杀（重启宿主/取消 pwsh 等待时
                # Ctrl+C 泄漏到同控制台进程），伴随 KeyboardInterrupt traceback
                # 即实锤。不是内存/限额/因子问题——先于 limit_note 给出，防
                # agent 误诊（当日实测被误读成「内存问题」）
                if proc.returncode == 0xC000013A or "KeyboardInterrupt" in tail:
                    hint = ("｜worker 被宿主控制台事件终止（rc=0xC000013A/"
                            "KeyboardInterrupt——宿主重启或宿主侧终端操作 Ctrl+C "
                            "泄漏所致，R31 起 worker 已隔离控制台，新代码下不应"
                            "再现）。不是因子实现/内存/限额问题；本次零写入，"
                            "原样重试同一因子即可")
                    limit_note = ""
                raise BridgeError(
                    -32005,
                    f"worker 无结果输出(rc={proc.returncode}): {tail}{hint}{limit_note}"
                    "｜bridge 本体未受影响，本次调用零写入（不烧名），可立即重试")
            result = json.loads(out_path.read_text(encoding="utf-8"))
            if not result.get("ok"):
                err = result.get("error") or {}
                msg = err.get("message", "worker 执行失败")
                # 域名错误（PRECONDITION/DATA）vs 基础设施错误的映射
                if "test 已被消费" in str(msg) or "not supported" in str(msg):
                    raise BridgeError(-32003, str(msg))
                raise BridgeError(-32603, str(msg))
            if run_id:
                self._finalize_kept_run(wdir, run_id, method)
                keep_run = True
            return result["result"]
        finally:
            # P2：释放 job 句柄（job 内已无进程，无副作用；父崩溃时 OS 按
            # KILL_ON_JOB_CLOSE 兜底杀残余）。C2：保目录的分支不删——
            # async_mode 连失败墓志都保留（由 _async_run 补记 failed）
            detach_worker_limits(job)
            if not keep_run and not async_mode:
                shutil.rmtree(str(wdir), ignore_errors=True)

    # ---- C2（修正案 A 2026-08-28）：重方法异步化 + run 目录收尸 ----

    def _dedup_key(self, method: str, source: str, params: dict):
        """层 2 在途去重键：方法+环境+源指纹+阶段+horizon。同键在途 → 复用
        同一 job_id（消灭「超时→重试」风暴的重复计算；同因子=同试验，
        trail 归提交线、n_trials 不变——跨线去重是特性不是泄漏）。"""
        try:
            src_key = source_fingerprint(source) if source else ""
            if not src_key:
                # batch 的 source 恒为空串（多 source 方法）——不去重键里
                # 放 sources 指纹的话，所有 batch 提交共用一个键：第二个
                # batch 在途时会并入第一个的作业、拿到错误结果（全量排查
                # 2026-08-31 发现）
                srcs = params.get("sources")
                if isinstance(srcs, dict):
                    src_key = "batch:" + source_fingerprint(json.dumps(
                        srcs, sort_keys=True, ensure_ascii=False))
            # composite 的成分表在 params 不在 source（R22，2026-08-31 复审
            # 发现）：同 overlay 不同成分 = 不同经济赌注——键里不并成分指纹
            # 的话，成分扫描（固定 overlay 换成分表，C1/C2 的标准工作流）
            # 里第二个 composite 会并入第一个的作业、拿到错误成分的结果
            ing = params.get("ingredients")
            if isinstance(ing, dict) and ing:
                src_key = (f"{src_key}|ing:{source_fingerprint(json.dumps(ing, sort_keys=True, ensure_ascii=False))}"
                           if src_key else
                           "ing:" + source_fingerprint(json.dumps(ing, sort_keys=True, ensure_ascii=False)))
            return (method,
                    self._resolve_env_id(params.get("envId", "primary")),
                    src_key,
                    str(params.get("stage", "development")),
                    str(params.get("horizon", "")))
        except Exception:
            return None

    def _dedup_release(self, run_id: str) -> None:
        with self._async_lock:
            key = self._async_dedup_rev.pop(run_id, None)
            if key is not None and self._async_dedup.get(key) == run_id:
                del self._async_dedup[key]

    def _job_json_write(self, run_dir: Path, payload: dict) -> None:
        """job.json 原子写（tmp+replace）——入队即落盘（R5）后，轮询永不扑空。
        replace 带重试：Windows 下目标正被并发读者打开（轮询 read_text /
        status 扫描）时 os.replace 立即 PermissionError，重试几十毫秒内让开
        （2026-08-31 实证：撞上一次 = 作业永卡 running + job.json.tmp 残留）。"""
        jp = run_dir / "job.json"
        tmp = jp.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        last_err: Exception | None = None
        for _ in range(10):
            try:
                os.replace(str(tmp), str(jp))
                return
            except PermissionError as e:
                last_err = e
                time.sleep(0.02)
        raise last_err if last_err else RuntimeError("job.json replace 失败")

    # ---- 效率编译层（2026-09-01）：门 / 重写调度 / optimizer 缓存 ----

    def _optimizer_load(self) -> dict:
        if self._optimizer_cache is None:
            try:
                p = Path(self.state_root) / "optimizer_cache.json"
                self._optimizer_cache = (json.loads(p.read_text(encoding="utf-8"))
                                         if p.exists() else {})
            except Exception:
                self._optimizer_cache = {}
        return self._optimizer_cache

    def _optimizer_put(self, fp: str, entry: dict) -> None:
        c = self._optimizer_load()
        c[fp] = entry
        while len(c) > 128:          # FIFO 有界（eval_cache 惯例）
            c.pop(next(iter(c)))
        try:
            p = Path(self.state_root) / "optimizer_cache.json"
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(p))
        except Exception:
            pass
        self._optimizer_cache = c

    def _maybe_rewrite_source(self, source: str) -> tuple[str, dict | None]:
        """optimizer cache 命中严格等价重写时换用（引擎侧透明加速）。hash/
        registry/eval_cache 语义全部锚定 agent 的原源码——重写与 njit 同类：
        验证过的等价实现变换，不是语义变更。返回 (source, provenance|None)。"""
        if not source:
            return source, None
        try:
            from .discipline import source_fingerprint
            fp = source_fingerprint(source)
        except Exception:
            return source, None
        with self._optimizer_lock:
            entry = self._optimizer_load().get(fp)
        if (isinstance(entry, dict) and entry.get("status") == "ok"
                and entry.get("rewritten_source")):
            prov = {"rewritten_from": fp, "rewritten_fp": entry.get("rewritten_fp"),
                    "verifier": "allclose(rtol=1e-6)", "model": entry.get("model"),
                    "engine": "rewrite:llm-v1"}
            return entry["rewritten_source"], prov
        return source, None

    def _apply_rewrite(self, method: str, source: str, params: dict) -> str:
        """_run_worker 入口的统一换用点：单源 + batch sources + composite
        ingredients。fail-open：任何异常原样放行。"""
        try:
            source, prov = self._maybe_rewrite_source(source)
            if prov:
                params["engine_rewrite"] = prov
            srcs = params.get("sources")
            if isinstance(srcs, dict) and srcs:
                for k in list(srcs):
                    s2, p2 = self._maybe_rewrite_source(str(srcs[k]))
                    if p2:
                        srcs[k] = s2
                        params.setdefault("engine_rewrites", {})[k] = p2
            ings = params.get("ingredients")
            if isinstance(ings, dict) and ings:
                for k in list(ings):
                    if isinstance(ings[k], str):
                        s2, p2 = self._maybe_rewrite_source(ings[k])
                        if p2:
                            ings[k] = s2
            return source
        except Exception:
            return source

    def _run_smoke_worker(self, source: str, sp: dict, env, timeout_s: float):
        """烟测/验证 worker 的统一出口（并发复审 F2）：并发信号量（2）限
        瞬时 smoke 子进程数——burst 下不超出权重核算的进程超订；等待上限
        120s，超时抛 infra 错（调用侧 fail-open 放行）。"""
        if not self._smoke_spawn_sem.acquire(timeout=120.0):
            raise BridgeError(-32005, "烟测并发位繁忙（等 120s 未获得）——本次跳过烟测")
        try:
            return self._run_worker("factor.smoke", source, sp, env,
                                    timeout_s=timeout_s)
        finally:
            self._smoke_spawn_sem.release()

    @staticmethod
    def _member_label(source: str, sources) -> str | None:
        """批成员名反查（复审 F5）：按源码字符串精确匹配 sources dict 的
        value。同名源码多成员时返回首个——足够点名用。"""
        if isinstance(sources, dict):
            s = str(source)
            for k, v in sources.items():
                if str(v) == s:
                    return str(k)
        return None

    def _enforce_causality_member(self, source: str, env, env_id, sources):
        """批成员因果门包裹：A 类硬拒/烟测门拒绝带上成员名——批量场景下
        agent 需要知道是哪个成员被拒（复审 F5）。单源路径不经过此包裹。"""
        try:
            return self._enforce_causality(source, env, env_id)
        except BridgeError as e:
            lbl = self._member_label(source, sources)
            if lbl:
                raise BridgeError(e.code, f"批成员「{lbl}」: {e}") from None
            raise

    def _smoke_gate(self, method: str, source: str, params: dict, env) -> None:
        """α 烟测门（fail-open——门自身任何异常放行，新层绝不破坏既有评估）：
        双尺寸实测外推超预算 → -32003 零烧名拒绝（附实测数字/循环行号/
        向量化指导/自动重写状态）；njit 可救 → params["smoke_engine"]。
        并发语义（复审 F1）：同 key 后到者等待先来者的烟测结果（Event），
        不重复 spawn；先来者失败后到者放行。"""
        if not self._smoke_enabled:
            return
        try:
            if method not in self._SMOKE_METHODS:
                return
            cells = int(getattr(env, "T", 0) or 0) * int(getattr(env, "N", 0) or 0)
            if cells < self._smoke_min_cells or not source:
                return
            from .discipline import source_fingerprint
            from .smoke import SMOKE_TIMEOUT_S, classify_gate
            src_fp = source_fingerprint(source)
            try:
                env_fp = self._env_full_fingerprint(
                    self._resolve_env_id(params.get("envId", "primary")))
            except Exception:
                env_fp = None
            key = (src_fp, env_fp if env_fp else cells)
            ev: threading.Event | None = None
            owner = False
            with self._optimizer_lock:
                raw = self._smoke_cache.get(key)
                if raw is None:
                    ev = self._smoke_inflight.get(key)
                    if ev is None:
                        ev = threading.Event()
                        self._smoke_inflight[key] = ev
                        owner = True
            if raw is None and not owner:
                ev.wait(timeout=SMOKE_TIMEOUT_S + 60.0)
                raw = self._smoke_cache.get(key)
                if raw is None:
                    return   # 先来者未产出结果（失败/被杀）——fail-open 放行
            if raw is None:
                assert ev is not None
                try:
                    smoke_npz = self._smoke_env_npz(env, params)
                    if smoke_npz is None or not Path(smoke_npz).exists():
                        return
                    sp = {"mode": "gate", "envId": params.get("envId", "primary"),
                          "full_cells": cells, "_smoke_npz": str(smoke_npz)}
                    try:
                        raw = self._run_smoke_worker(source, sp, env, SMOKE_TIMEOUT_S)
                    except BridgeError as e:
                        if "worker 超时" in str(e):
                            # 烟测自身超时 = 死循环/极端非向量化（60s 小面板没跑完）
                            raw = {"timeout": True}
                        else:
                            return   # 含并发位繁忙/基础设施问题——fail-open
                    if len(self._smoke_cache) > 256:
                        self._smoke_cache.clear()
                    self._smoke_cache[key] = raw
                finally:
                    with self._optimizer_lock:
                        self._smoke_inflight.pop(key, None)
                    ev.set()
            if raw.get("interp_error"):
                return   # 小面板跑不起来（窗口长度等）——放行让正式调用报真错
            budget_s = effective_wall_timeout(base_s=self.worker_timeout_ms / 1000.0)
            if raw.get("timeout"):
                verdict = {"verdict": "reject", "reason": "hang", "alpha": None,
                           "est_full_s": None, "mult": None,
                           "projected_s": None, "budget_s": round(budget_s)}
            else:
                verdict = classify_gate(raw, method, params, budget_s)
            slim = {k: verdict.get(k) for k in
                    ("verdict", "alpha", "est_full_s", "projected_s", "budget_s")
                    if verdict.get(k) is not None}
            if slim:
                params["smoke_telemetry"] = slim
            if verdict["verdict"] == "njit":
                params["smoke_engine"] = "njit"
                return
            if verdict["verdict"] == "pass":
                return
            # 复审 F3：先入队拿到真实状态，再拼拒绝消息（消息与作业状态一致）
            rw_status = self._maybe_enqueue_rewrite(source, raw, verdict, env,
                                                    params, method)
            raise BridgeError(-32003,
                              self._smoke_reject_message(source, raw, verdict,
                                                         rw_status))
        except BridgeError:
            raise
        except Exception:
            return   # fail-open：门 bug 不阻断评估

    def _smoke_reject_message(self, source: str, raw: dict, v: dict,
                              rw_status: str) -> str:
        parts = ["拒绝评估（烟测门·实测超预算——零大面板计算、零 trial、不烧名）："]
        if raw.get("timeout"):
            parts.append("小子面板 60s 内未完成——实现疑似死循环或极端非向量化。")
        else:
            alpha = v.get("alpha")
            alg = ("（α>1.4：算法复杂度超线性——向量化救不了，必须重排算法，"
                   "如把逐步重算改为前缀/缓存结构）" if (alpha or 0) > 1.4
                   else "（常数慢——向量化即可）")
            parts.append(f"实测 {raw.get('small_cells')} 格 {raw.get('t_small_s')}s / "
                         f"{raw.get('smoke_cells')} 格 {raw.get('t_smoke_s')}s → "
                         f"缩放指数 α={alpha}{alg}")
            parts.append(f"全面板外推单次 ≈{v.get('est_full_s')}s × 本方法需跑 "
                         f"{v.get('mult')} 遍 ≈ {v.get('projected_s')}s > "
                         f"预算 {v.get('budget_s')}s")
        nj = raw.get("njit") or {}
        if nj.get("compiled") and not nj.get("equiv"):
            parts.append("numba 自动编译可编译但与解释版数值不等价——已弃用。")
        elif nj.get("available") and not nj.get("compiled"):
            parts.append(f"numba 不可编译（{str(nj.get('error'))[:80]}）——含 "
                         "numba 不支持的写法（pandas 对象/nan 系算子等）。")
        try:
            from .discipline import scan_inefficiency
            ineff = scan_inefficiency(source)
            if ineff.get("class_b"):
                lines = ", ".join(f"L{h.get('line')}" for h in ineff["class_b"][:4])
                parts.append(f"检测到的循环：{lines}——优先 rolling/ewm/shift/"
                             "cumsum/矩阵广播替代逐 cell 循环")
        except Exception:
            pass
        parts.append(self._rewrite_status_text(source, rw_status))
        parts.append("这是实现效率问题，不是对研究方向的判定——修正后重交同一假设即可。")
        return "\n".join(parts)

    def _rewrite_status_text(self, source: str, status: str) -> str:
        """拒绝消息里的重写层状态段。status 来自 _maybe_enqueue_rewrite 的
        真实返回（复审 F3：消息不再先斩后奏）。"""
        from . import rewrite as rw
        if status == "disabled" or rw.rewrite_config() is None:
            return ("引擎自动重写层未配置（需 DSH_FACTOR_REWRITE_API_KEY 或 "
                    "OPENCODE_GO_API_KEY）——请自行向量化后重交。")
        if status == "submit_failed":
            return ("自动重写作业提交失败（队列可能已满——status 的 workerRuns "
                    "可查在途/排队）——请自行向量化后重交；稍后重交本源码会重新触发。")
        if status == "inflight":
            return ("自动重写进行中（约 1-3 分钟）——稍后**原样重交本源码**，"
                    "引擎将透明换用严格等价的向量化版本。")
        try:
            from .discipline import source_fingerprint
            fp = source_fingerprint(source)
        except Exception:
            fp = None
        with self._optimizer_lock:
            entry = self._optimizer_load().get(fp) if fp else None
        if status == "cached":
            # 缓存已有结论仍被拒 = 换用后的重写版对本方法（乘数更高）也超预算
            if isinstance(entry, dict) and entry.get("status") == "ok":
                return ("本源码已有严格等价的重写版，但对当前方法仍超预算——"
                        "重交无益，请进一步重排算法（消除超线性/减少计算量）。")
            return (f"自动重写已试未果（{str((entry or {}).get('note'))[:120]}）——"
                    "请自行重写：向量化（rolling/ewm/广播）或重排算法消除超线性。")
        # enqueued / unknown
        return ("已自动提交重写作业（强模型重写+严格等价验证，约 1-3 分钟）——"
                "稍后原样重交本源码即可；或自行向量化立即重交。")

    def _maybe_enqueue_rewrite(self, source, raw, verdict, env, params,
                               method: str) -> str:
        """C-verdict 时自动排重写作业（权重 1 走现有队列；网络等待为主不
        占核）。在途/已有结论（ok/failed）不重复烧模型。
        返回真实状态（复审 F3）：enqueued / inflight / cached（含 ok/failed
        细节由 _rewrite_status_text 再查）/ disabled / submit_failed /
        unknown——调用方按此拼拒绝消息。"""
        from . import rewrite as rw
        if rw.rewrite_config() is None:
            return "disabled"
        try:
            from .discipline import source_fingerprint
            fp = source_fingerprint(source)
        except Exception:
            return "unknown"
        with self._optimizer_lock:
            if fp in self._rewrite_inflight:
                return "inflight"
            if isinstance(self._optimizer_load().get(fp), dict):
                return "cached"
            self._rewrite_inflight.add(fp)
        try:
            profile = (f"实测 {raw.get('small_cells')} 格 {raw.get('t_small_s')}s / "
                       f"{raw.get('smoke_cells')} 格 {raw.get('t_smoke_s')}s，"
                       f"alpha={verdict.get('alpha')}，全面板外推单次 "
                       f"{verdict.get('est_full_s')}s；触发方法 {method}")
            env_id = params.get("envId", "primary")
            self._async_submit(
                "engine.rewrite", source,
                {"envId": env_id, "rewrite_profile": profile,
                 "rewrite_method": method},
                env, env_id,
                extra_meta={"rewrite": True, "orig_fp": fp,
                            "lane": current_lane()})
            return "enqueued"
        except Exception:
            with self._optimizer_lock:
                self._rewrite_inflight.discard(fp)
            return "submit_failed"

    def _run_rewrite_job(self, item: dict) -> None:
        """engine.rewrite 作业主体（_async_run 分支进入，不走 _run_worker）：
        LLM 重写（≤2 次尝试，失败带错误反馈重试）→ 安全扫描 → 烟测 worker
        子进程内严格等价验证 → 投影进预算才采纳（仍超预算的重写会被门再拒
        = 死循环，不采纳）→ optimizer_cache；失败也记录（同类源码不重烧）。"""
        from . import rewrite as rw
        from .smoke import extrapolate, fit_alpha, method_cost, noise_budget_s
        from .discipline import source_fingerprint
        run_id = item["run_id"]
        run_dir = Path(item["run_dir"])
        source = item["source"]
        params = item["params"]
        env = item["env"]
        fp = (item["run_meta"].get("orig_fp") or source_fingerprint(source))
        summary: dict = {"orig_fp": fp, "model": None}
        ok = False
        try:
            cfg = rw.rewrite_config()
            if cfg is None:
                summary["status"] = "disabled"
            else:
                summary["model"] = cfg["model"]
                base_msg = rw.build_user_message(
                    source, str(params.get("rewrite_profile") or ""))
                feedback = None
                for attempt in (1, 2):
                    content, usage = rw.call_llm(
                        cfg, base_msg if feedback is None
                        else base_msg + "\n\n---\n" + feedback)
                    declare, code, note = rw.parse_reply(content)
                    summary[f"a{attempt}"] = {
                        "declare": declare,
                        "completion_tokens": usage.get("completion_tokens"),
                        "note": note[:160]}
                    if code is None or declare == "CANNOT_PRESERVE":
                        feedback = f"未给出可用的严格等价重写（declare={declare}）——请再试"
                        continue
                    problems = rw.safety_scan(code)
                    if problems:
                        feedback = (f"安全扫描拒绝：{problems}——只准 np/pd/math、"
                                    "禁 import/系统调用")
                        continue
                    try:
                        smoke_npz = self._smoke_env_npz(env, params)
                        if smoke_npz is None:
                            vres = {"run_error": "烟测面板不可用"}
                        else:
                            vres = self._run_smoke_worker(
                                code,
                                {"mode": "verify", "orig_source": source,
                                 "full_cells": int(env.T) * int(env.N),
                                 "envId": params.get("envId", "primary"),
                                 "_smoke_npz": str(smoke_npz)},
                                env, 120.0)
                    except BridgeError as e:
                        vres = {"run_error": str(e)[:200]}
                    if vres.get("equiv") is True:
                        alpha_n = fit_alpha(
                            float(vres.get("t_small_s") or 0.0),
                            float(vres.get("t_smoke_s") or 0.0),
                            int(vres.get("small_cells") or 0),
                            int(vres.get("smoke_cells") or 0))
                        est_n = extrapolate(
                            float(vres.get("t_smoke_s") or 0.0),
                            int(vres.get("smoke_cells") or 0),
                            int(vres.get("full_cells") or 0), alpha_n)
                        mult, share = method_cost(
                            str(params.get("rewrite_method")
                                or "factor.evaluate"), params)
                        budget_s = effective_wall_timeout(
                            base_s=self.worker_timeout_ms / 1000.0)
                        eff = (noise_budget_s() if share is None
                               else budget_s * share)
                        summary["est_full_rewritten_s"] = round(est_n, 1)
                        if est_n * mult <= eff:
                            with self._optimizer_lock:
                                self._optimizer_put(fp, {
                                    "status": "ok",
                                    "rewritten_source": code,
                                    "rewritten_fp": source_fingerprint(code),
                                    "declare": declare, "model": cfg["model"],
                                    "est_full_s": round(est_n, 1),
                                    "verified_at": time.strftime(
                                        "%Y-%m-%dT%H:%M:%S")})
                            try:
                                store_factor_source(code, self.state_root)
                            except Exception:
                                pass
                            summary["status"] = "ok"
                            ok = True
                            break
                        feedback = (f"重写仍超预算（外推 {est_n:.1f}s × {mult} > "
                                    f"{eff:.0f}s）——需要更彻底的算法重排，请再试")
                    else:
                        why = (vres.get("run_error") or vres.get("verify_error")
                               or (f"等价性失败 shape={vres.get('shape')}"
                                   if vres.get("equiv") is False
                                   else str(vres)[:150]))
                        feedback = (f"你上一版重写未通过验证：{why}——请修正后"
                                    "重新给出完整重写")
                if not ok:
                    summary["status"] = "failed"
                    note_txt = str((summary.get("a2") or {}).get("note")
                                   or (summary.get("a1") or {}).get("note")
                                   or "")[:200]
                    with self._optimizer_lock:
                        self._optimizer_put(fp, {
                            "status": "failed", "note": note_txt,
                            "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        except Exception as e:  # noqa: BLE001 — 作业线程兜底：失败留痕不炸线程
            summary["status"] = "error"
            summary["error"] = f"{type(e).__name__}: {e}"[:200]
            try:
                with self._optimizer_lock:
                    self._optimizer_put(fp, {
                        "status": "failed", "note": summary["error"],
                        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            except Exception:
                pass
        finally:
            with self._optimizer_lock:
                self._rewrite_inflight.discard(fp)
            # done 墓志 + out.json（镜像 _run_worker 成功路径，轮询可见）
            try:
                (run_dir / "out.json").write_text(
                    json.dumps({"ok": True, "result": summary},
                               ensure_ascii=False), encoding="utf-8")
                self._finalize_kept_run(run_dir, run_id, "engine.rewrite")
            except Exception:
                pass

    def _async_submit(self, method: str, source: str, params: dict, env,
                      env_id, extra_meta: dict | None = None) -> dict:
        """层 1 提交口：在途去重（层 2）→ 权重槽判定 → 直接启动或入队。
        槽满不拒绝：入队返回 queued+位置（Q 满才拒绝）。job.json 入队即
        落盘（status=submitted/queued + pid——R5 竞态 + R6 收尸的数据源）。"""
        weight = self._JOB_WEIGHT.get(method, 1)
        try:
            env_meta = {"envId": self._resolve_env_id(env_id)}
        except Exception:
            env_meta = {"envId": env_id}
        run_meta = {**env_meta, "source": source,
                    # 提交者的线随作业落盘：轮询者可能不是提交者（异步
                    # 取回时 trail 写入归提交线，2026-08-31 方案 A）
                    "lane": current_lane(),
                    **(extra_meta or {})}
        run_id = f"job-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}"
        run_dir = Path(self.state_root) / "worker_runs" / run_id
        with self._async_lock:
            dkey = self._dedup_key(method, source, params)
            if dkey is not None and dkey in self._async_dedup:
                return {"job_id": self._async_dedup[dkey],
                        "status": "in_flight", "dedup": True,
                        "note": "同参数作业在途——复用同一 job_id 轮询取结果"
                                "（在途去重，层 2）：running=未完，queued=排队中"}
            start_now = (self._async_weight_active + weight
                         <= self._slot_capacity)
            if not start_now and len(self._async_queue) >= self._queue_capacity:
                raise BridgeError(
                    -32003,
                    f"作业队列已满（深度 {self._queue_capacity}，槽容量 "
                    f"{self._slot_capacity}）——status 的 workerRuns 查在途/"
                    "排队作业，取回结果（本方法传 job_id）后再提交")
            if dkey is not None:
                self._async_dedup[dkey] = run_id
                self._async_dedup_rev[run_id] = dkey
            item = {"run_id": run_id, "method": method, "source": source,
                    "params": params, "env": env, "run_meta": run_meta,
                    "weight": weight, "run_dir": run_dir}
            if start_now:
                self._async_weight_active += weight
                self._async_jobs[run_id] = "submitted"
                # 公平游标（层 3）：直接启动的作业同样推进 lane_last——
                # 否则首个 pump 会按 FIFO 取同线作业，公平性失效
                self._async_lane_last = run_meta.get("lane")
                position = None
            else:
                self._async_queue.append(item)
                self._async_jobs[run_id] = "queued"
                position = len(self._async_queue)
        # 锁外写盘（提交即落盘，R5——首次轮询必有 job.json；pid 供 R6 收尸）
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            self._job_json_write(run_dir, {
                "job_id": run_id, "method": method,
                "status": "submitted" if start_now else "queued",
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "async", "pid": os.getpid(),
                "weight": weight, **run_meta})
        except Exception:
            pass  # 落盘失败不阻断：内存登记仍在，线程启动后会补写
        if start_now:
            self._async_start(item)
            return {"job_id": run_id, "status": "submitted",
                    "note": "后台 worker 执行中——本方法传 job_id 轮询取结果"
                            "（running=未完）；status 的 workerRuns 列进度。"
                            "run 目录保留 out.json：客户端超时/弃单不丢结果"}
        return {"job_id": run_id, "status": "queued",
                "queue_position": position,
                "queue_depth": len(self._async_queue),
                "note": f"槽满入队（第 {position} 位）——本方法传同一 job_id "
                        "轮询取结果；status 的 workerRuns 列队列进度。"
                        "run 目录保留 out.json：客户端超时/弃单不丢结果"}

    def _async_start(self, item: dict) -> None:
        # R29（2026-08-31 隔离复审）：启动失败必须回滚——权重/dedup 键/
        # 内存登记都在提交侧先行登记，线程没起来就永不释放（槽永久缩水、
        # 同参提交永并入死作业、队列永卡）。回滚后落 failed 墓志，不向上
        # 抛（pump 批量启动不被单个失败炸断）
        try:
            self._job_json_write(item["run_dir"], {
                "job_id": item["run_id"], "method": item["method"],
                "status": "running",
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mode": "async", "pid": os.getpid(),
                "weight": item["weight"], **item["run_meta"]})
        except Exception as e:
            self._rollback_started_item(item, f"启动失败(job.json): {e}")
            return
        try:
            th = threading.Thread(target=self._async_run, args=(item,),
                                  daemon=True, name=f"async-{item['run_id']}")
            th.start()
        except Exception as e:  # 线程资源耗尽等
            self._rollback_started_item(item, f"启动失败(thread): {e}")

    def _stamp_run_phase(self, run_dir, phase: str) -> None:
        """job.json 追加 phase 标记（R32）——只读改写 status 之外的可见性
        字段，失败静默（墓志/status 语义不受影响）。"""
        try:
            jp = Path(run_dir) / "job.json"
            j = json.loads(jp.read_text(encoding="utf-8"))
            j["phase"] = phase
            self._job_json_write(Path(run_dir), j)
        except Exception:
            pass

    def _rollback_started_item(self, item: dict, why: str) -> None:
        with self._async_lock:
            self._async_jobs.pop(item["run_id"], None)
            self._async_weight_active = max(
                0, self._async_weight_active - item["weight"])
        self._dedup_release(item["run_id"])
        try:
            self._finalize_kept_run(item["run_dir"], item["run_id"],
                                    item["method"], error=why,
                                    error_code=-32005)
        except Exception:
            pass
        try:
            import sys as _sys
            _sys.stderr.write(f"[async_start] {why} — 已回滚 {item['run_id']}\n")
        except Exception:
            pass

    def _async_pump(self) -> None:
        """槽释放后出队（层 1）：扫描所有放得下的作业（防大作业头部阻塞
        小作业）；公平性（层 3）：放得下的候选里优先与最近启动不同的 lane
        ——A 线的扫描不饿死 B 线的交互调用。"""
        starts = []
        with self._async_lock:
            while True:
                fittable = [j for j, it in enumerate(self._async_queue)
                            if (self._async_weight_active + it["weight"]
                                <= self._slot_capacity)]
                if not fittable:
                    break
                pi = fittable[0]
                lane_last = self._async_lane_last
                if lane_last is not None:
                    for j in fittable:
                        cand = self._async_queue[j]
                        if (cand["run_meta"] or {}).get("lane") != lane_last:
                            pi = j
                            break
                pick = self._async_queue[pi]
                del self._async_queue[pi]
                self._async_weight_active += pick["weight"]
                self._async_lane_last = (pick["run_meta"] or {}).get("lane")
                self._async_jobs[pick["run_id"]] = "submitted"
                starts.append(pick)
        for it in starts:
            self._async_start(it)

    def _async_run(self, item: dict):
        run_id = item["run_id"]
        try:
            # 因果门随作业执行（2026-08-31 计算利用 PLAN）：提交路径不做
            # 同步 worker 调用——新源码的因果检测（大面板 30-270s）会把
            # 「秒回提交」变回阻塞。语义不变：FUTURE_LEAK 在轮询时以作业
            # 失败文案送达（含同样的整改指引）。R25：batch 携多源清单
            # （causality_sources）逐成员过门——与同步路径全覆盖对齐。
            # R32：门阶段写 phase 进 job.json——大面板上门可达数分钟，
            # 不留痕会被误读成「排队中/卡死」（2026-08-31 20:25 生产实证：
            # agent 观测 bridge CPU 近零后误判 waiting in queue）
            if item["method"] == "engine.rewrite":
                # 效率编译层（2026-09-01）：重写作业在 bridge 侧执行
                # （LLM 调用网络等待为主 + 烟测验证子进程），不走 _run_worker
                self._stamp_run_phase(item["run_dir"], "rewriting")
                self._run_rewrite_job(item)
            else:
                if item["run_meta"].get("causality_gate"):
                    self._stamp_run_phase(item["run_dir"], "causality_check")
                    multi = item["run_meta"].get("causality_sources")
                    if multi:
                        _srcs = item["params"].get("sources")
                        for s in multi:
                            self._enforce_causality_member(
                                str(s), item["env"],
                                item["run_meta"].get("envId", "primary"), _srcs)
                    else:
                        self._enforce_causality(
                            item["source"], item["env"],
                            item["run_meta"].get("envId", "primary"))
                    self._stamp_run_phase(item["run_dir"], "evaluating")
                self._run_worker(item["method"], item["source"], item["params"],
                                 item["env"], run_id=run_id,
                                 run_meta=item["run_meta"], async_mode=True,
                                 run_dir=item["run_dir"])
        except BridgeError as e:
            self._mark_run_failed(run_id, str(e), code=e.code)
        except Exception as e:  # noqa: BLE001 — 线程内兜底：墓志必须留下
            self._mark_run_failed(run_id, f"{type(e).__name__}: {e}")
        finally:
            self._async_jobs.pop(run_id, None)
            self._dedup_release(run_id)
            with self._async_lock:
                self._async_weight_active = max(
                    0, self._async_weight_active - item["weight"])
            self._async_pump()

    def _reap_orphan_jobs(self) -> None:
        """启动收尸（R6，2026-08-31 计算利用 PLAN）：上一桥进程留下的
        queued/running 作业——worker 随桥死（Job Object KILL_ON_JOB_CLOSE），
        排队作业只在死进程内存里。job.json 记 pid：pid 不活即标 failed
        （零入账，agent 可重试）。无 pid 字段的旧文件保守跳过。"""
        try:
            root = Path(self.state_root) / "worker_runs"
            if not root.exists():
                return
            for d in root.iterdir():
                jp = d / "job.json"
                if not jp.exists():
                    continue
                try:
                    j = json.loads(jp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if j.get("status") not in ("queued", "running", "submitted"):
                    continue
                pid = j.get("pid")
                if (isinstance(pid, int) and pid != os.getpid()
                        and not pid_alive(pid)):
                    j["status"] = "failed"
                    j["error"] = ("bridge 进程已退出（启动收尸 R6）——零入账，"
                                  "可原样重试")
                    j["reaped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    try:
                        tmp = jp.with_suffix(".json.tmp")
                        tmp.write_text(json.dumps(j, ensure_ascii=False),
                                       encoding="utf-8")
                        os.replace(str(tmp), str(jp))
                    except Exception:
                        pass
        except Exception:
            pass

    def _async_result(self, job_id: str):
        found = self._find_run(job_id)
        if found is None:
            # 内存登记兜底：job.json 读侧瞬态竞态（如陈旧句柄/半写文件的
            # 极窄窗口——写侧已原子化，此为第二道保险）不升级为硬错误
            mem = self._async_jobs.get(job_id)
            if mem:
                return {"job_id": job_id,
                        "status": "queued" if mem == "queued" else "running",
                        "note": "仍在执行——稍后再传同一 job_id 取结果"}
            raise BridgeError(
                -32602,
                f"未知 job_id: {job_id}——status 的 workerRuns 列最近 run"
                f"（有界保留 {self._MAX_KEPT_RUNS} 个，过期作业只能重跑）")
        d, j = found
        status = j.get("status")
        if status in ("queued", "submitted"):
            pos = None
            with self._async_lock:
                for i, it in enumerate(self._async_queue):
                    if it["run_id"] == job_id:
                        pos = i + 1
                        break
            return {"job_id": job_id, "status": "queued",
                    "queue_position": pos,
                    "queue_depth": len(self._async_queue),
                    "note": "槽满排队中——稍后再传同一 job_id 取结果"
                            "（status 的 workerRuns 列队列进度）"}
        if status == "running":
            return {"job_id": job_id, "status": "running",
                    "note": "仍在执行——稍后再传同一 job_id 取结果"}
        if status == "failed":
            # R27（2026-08-31 隔离复审）：按作业内原始错误码重现——worker
            # 域错误（-32603 因子错误/-32003 test 已消费）不再统一改报
            # infra 的 -32005（错误分类契约失真 + dispatch 对 -32005 记
            # infra 台账，重复轮询同一失败作业会刷屏污染 loop 超时纠偏
            # 声道）；真 infra 作业无码回落 -32005
            try:
                _code = int(j.get("error_code", -32005))
            except (TypeError, ValueError):
                _code = -32005
            raise BridgeError(_code, f"异步作业失败: {j.get('error', '?')}")
        if j.get("method") == "engine.rewrite":
            # 复审 F4：引擎内部重写作业被轮询到（job_id 可从 workerRuns 列表
            # 看到）——返回信息性摘要而非伪 diagnosis，防 agent 把它当评估对象
            try:
                _rw = json.loads((d / "out.json").read_text(encoding="utf-8"))
                _summ = _rw.get("result") or {}
            except Exception:
                _summ = {}
            return {"job_id": job_id, "engine_rewrite": _summ,
                    "note": "这是引擎内部重写作业（非评估结果）——原样重交"
                            "原源码即可透明换用其产物"}
        try:
            result = json.loads((d / "out.json").read_text(encoding="utf-8"))
        except Exception as e:
            raise BridgeError(-32005, f"作业标 done 但 out.json 不可读: {e}")
        if not result.get("ok"):
            err = (result.get("error") or {}).get("message", "结果无效")
            raise BridgeError(-32603, str(err))
        method = j.get("method", "")
        if method == "factor.evaluate_batch":
            # R24/R25：批信封不是单因子 diagnosis——走与同步路径同一的
            # _postprocess_batch_result（成员 A1 入账/deflated 重算/loop
            # 指令）。diagnosis.json 快照幂等（R23 同款：重复轮询不重复
            # 入账、不挪条目）。sources/horizon 从 run 目录的 request.json
            # 还原（提交时已注入 horizon/pool_std）
            diag_path = d / "diagnosis.json"
            if diag_path.exists():
                try:
                    return json.loads(diag_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            try:
                req = json.loads((d / "request.json").read_text(encoding="utf-8"))
                rp = req.get("params") or {}
                b_sources = rp.get("sources") or {}
                b_h = rp.get("horizon")
                if not isinstance(b_h, int):
                    b_h = None
            except Exception:
                b_sources, b_h = {}, None
            _tok = set_request_lane(normalize_lane(j.get("lane")))
            try:
                out = self._postprocess_batch_result(
                    j.get("envId", "primary"), b_h, b_sources, result["result"])
            finally:
                reset_request_lane(_tok)
            try:
                tmp = diag_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(out, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(str(tmp), str(diag_path))
            except Exception:
                pass  # 快照落盘失败不阻断返回（下次轮询重做后处理）
            return out
        stage = self._ASYNC_STAGE.get(method)
        if stage is None:
            # evaluate 的 stage 随请求变（development/selection/test）——
            # 提交时存进 job.json（层 0c）；audit 等本就不包装的方法返回裸结果
            stage = j.get("stage")
        if stage is None:
            return result["result"]
        # wrap 幂等（R23，2026-08-31 复审发现）：_wrap_diagnosis 带 trail
        # 写入副作用——每次对 done 作业的轮询都重跑 wrap 会把该试验条目
        # 挪到 trail 队尾（污染 latest5/断链判定：同线已有更新试验时重轮询
        # 旧作业会伪断链 + arc 误归零）。首次 wrap 的快照落 diagnosis.json，
        # 后续轮询（含去重并入者）直接取快照——同一试验永远同一份入账数字
        diag_path = d / "diagnosis.json"
        if diag_path.exists():
            try:
                return json.loads(diag_path.read_text(encoding="utf-8"))
            except Exception:
                pass  # 快照损坏 → 走下方重算 wrap（副作用同旧版）
        # trail 写入归提交线（job.json 的 lane），轮询者的 contextvar
        # 不参与——防 A 提交 B 轮询时条目被记到 B 线
        _tok = set_request_lane(normalize_lane(j.get("lane")))
        try:
            out = self._wrap_diagnosis(j.get("envId", "primary"),
                                       j.get("source", ""), stage,
                                       result["result"])
        finally:
            reset_request_lane(_tok)
        try:
            tmp = diag_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(str(tmp), str(diag_path))
        except Exception:
            pass  # 快照落盘失败不阻断返回（下次轮询重 wrap，退回旧版行为）
        return out

    def _find_run(self, job_id: str):
        root = Path(self.state_root) / "worker_runs"
        if not root.exists():
            return None
        for d in root.iterdir():
            jp = d / "job.json"
            if not jp.exists():
                continue
            try:
                j = json.loads(jp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if j.get("job_id") == job_id:
                return d, j
        return None

    def _mark_run_failed(self, run_id: str, msg: str,
                         code: int | None = None) -> None:
        found = self._find_run(run_id)
        if found is None:
            # R27：失败墓志一个都不能少——job.json 缺失时 stderr 留痕
            # （此前静默 no-op，轮询者只会看到「未知 job_id」）
            try:
                import sys as _sys
                _sys.stderr.write(f"[job_failed] 墓志缺失 {run_id}: {msg[:200]}\n")
            except Exception:
                pass
            return
        d, j = found
        self._finalize_kept_run(d, run_id, j.get("method", ""), error=msg,
                                error_code=code)

    def _finalize_kept_run(self, wdir: Path, run_id: str, method: str,
                           error: str | None = None,
                           error_code: int | None = None) -> None:
        """成功/失败墓志写入 + 有界清理。容错但可见——收尸失败必须留痕：
        静默吞错会让作业永卡 running（2026-08-31 实证：replace 撞上并发
        读者）。error_code（R27）：失败的原生错误码（域错误 vs infra），
        轮询侧重现时保持分类。"""
        try:
            j: dict = {}
            try:
                j = json.loads((wdir / "job.json").read_text(encoding="utf-8"))
            except Exception:
                j = {}
            j.update({"job_id": run_id, "method": method,
                      "status": "failed" if error else "done",
                      "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            if error:
                j["error"] = str(error)[:600]
            if error and error_code is not None:
                j["error_code"] = int(error_code)
            self._job_json_write(wdir, j)
        except Exception as e:
            try:
                import sys as _sys
                _sys.stderr.write(f"[job_finalize] 墓志写入失败 "
                                  f"{run_id}: {type(e).__name__}: {e}\n")
            except Exception:
                pass
        # env.npz 在个股面板上是数百 MB——结果/墓志留下，数据体删掉
        for junk in ("env.npz", "env.npz.meta.json"):
            try:
                (wdir / junk).unlink()
            except Exception:
                pass
        self._prune_kept_runs()

    def _prune_kept_runs(self) -> None:
        try:
            root = Path(self.state_root) / "worker_runs"
            if not root.exists():
                return
            entries = []
            for d in root.iterdir():
                jp = d / "job.json"
                try:
                    if not jp.exists():
                        continue
                    # R28（2026-08-31 隔离复审）：非终态目录永不剪——排队
                    # 作业的 job.json mtime=提交时刻恰是最老，纯按 mtime 剪
                    # 会把在跑/排队作业的目录删掉（丢 source/lane/envId 致
                    # 轮询侧错账，或 pump 启动即炸）
                    try:
                        st = json.loads(jp.read_text(encoding="utf-8")).get("status")
                    except Exception:
                        st = None
                    if st in ("queued", "running", "submitted", None):
                        continue
                    entries.append((jp.stat().st_mtime_ns, d))
                except OSError:
                    continue
            entries.sort(reverse=True)
            for _, d in entries[self._MAX_KEPT_RUNS:]:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    def _recent_runs(self) -> list:
        try:
            root = Path(self.state_root) / "worker_runs"
            if not root.exists():
                return []
            out = []
            for d in root.iterdir():
                jp = d / "job.json"
                if not jp.exists():
                    continue
                try:
                    j = json.loads(jp.read_text(encoding="utf-8"))
                except Exception:
                    continue
                out.append({"jobId": j.get("job_id"), "method": j.get("method"),
                            "status": j.get("status"), "mode": j.get("mode"),
                            "submittedAt": j.get("submitted_at"),
                            "finishedAt": j.get("finished_at"),
                            "error": (j.get("error") or None)
                            if j.get("status") == "failed" else None})
            out.sort(key=lambda r: str(r.get("submittedAt") or ""), reverse=True)
            return out[:self._MAX_KEPT_RUNS]
        except Exception:
            return []

    def _run_factor(self, method: str, source: str, params: dict[str, Any], env) -> Any:
        """统一执行入口：worker 模式 → 子进程；in_process 模式 → 当前进程（调试用）。

        2026-08-18 修复：evaluate_batch 是多 source 方法（无单个 source 参数），
        入口的条件编译跳过——此前无条件编译空串导致 batch 在两种模式下
        全部失败（agent 三次重试全 ERR，从未有人成功调用过 batch）。
        """
        if self.execution_mode == "worker":
            return self._run_worker(method, source, params, env)
        # in_process（仅受信本地调试）：效率编译层同覆盖（烟测探针仍走
        # 子进程；in_process 的解释执行无超时保护——DESIGN §10 既有风险）
        if method != "factor.smoke":
            try:
                source = self._apply_rewrite(method, source, params)
                self._smoke_gate(method, source, params, env)
            except BridgeError:
                raise
            except Exception:
                pass
        # in_process 仅受信本地调试（DESIGN §10 标注风险）
        fn = self._compile_factor(source) if (source and method != "factor.evaluate_batch") else None
        if method == "factor.check_causality":
            return check_causality(fn, env)
        if method == "factor.evaluate":
            stage = params.get("stage", "development")
            # W2（2026-08-26 规划书）：单次 factor(env) 计时 + submit 噪声门
            # 可行性预警——与 worker.factor.evaluate 同口径（见 noise.factor_perf）；
            # P1：CPU 秒并记（并行下墙钟失真）
            import time as _time
            from .factor.noise import factor_perf
            _t0 = _time.monotonic()
            _p0 = _time.process_time()
            F = fn(env)
            _wall = _time.monotonic() - _t0
            _cpu = _time.process_time() - _p0
            _perf = {**factor_perf(_cpu), "wall_s": round(_wall, 3),
                     "cpu_s": round(_cpu, 3)}
            _ps = params.get("pool_std")
            _ps = float(_ps) if isinstance(_ps, (int, float)) else None
            # v3：bar_sigma 为门参数（E[max|X|] σ 单位）；n_trials 纯遥测；
            # horizon 申报透传
            _bs = params.get("bar_sigma")
            _bs = float(_bs) if isinstance(_bs, (int, float)) else None
            _nt = params.get("n_trials", 1)
            _nt = float(_nt) if isinstance(_nt, (int, float)) else 1.0
            _h = params.get("horizon")
            try:
                _h = int(_h) if _h not in (None, "") else None
            except (TypeError, ValueError):
                _h = None
            if stage == "development":
                out = evaluate(F, env, n_trials=_nt, pool_std=_ps, horizon=_h,
                               bar_sigma=_bs)
            elif stage == "selection":
                out = evaluate_selection(F, env)
            elif stage == "test":
                try:
                    out = evaluate_test(F, env, state_root=self.state_root)
                except RuntimeError as e:
                    raise BridgeError(-32003, str(e)) from None
            else:
                raise BridgeError(-32602, f"未知 stage: {stage}")
            if isinstance(out, dict):
                out["perf"] = _perf
            return out
        if method == "factor.evaluate_composite":
            parts = {}
            for name, src in (params.get("ingredients") or {}).items():
                parts[name] = self._compile_factor(src)(env)
            return evaluate_composite(fn(env), parts, env)
        if method == "factor.evaluate_batch":
            F_dict = {}
            for name, src in (params.get("sources") or {}).items():
                F_dict[name] = self._compile_factor(src)(env)
            _h = params.get("horizon")
            try:
                _h = int(_h) if _h not in (None, "") else None
            except (TypeError, ValueError):
                _h = None
            _psb = params.get("pool_std")
            _psb = float(_psb) if isinstance(_psb, (int, float)) else None
            return evaluate_batch(F_dict, env, horizon=_h, pool_std=_psb)
        if method == "factor.walk_forward":
            return evaluate_walk_forward(
                fn(env), env, n_folds=int(params.get("n_folds", 5)),
                t0_date=params.get("t0_date"), t1_date=params.get("t1_date"))
        if method == "factor.noise_test":
            # 噪声硬门（2026-08-24 用户决策）——in_process 分发
            from .factor.noise import noise_test
            stat = None
            if params.get("statistic") == "spread":
                from .factor.tail import spread_ir_statistic
                # R30（2026-08-31 隔离复审）：G2 统计量绑环境口径 tail_k——
                # k_frac 可配化（8d87a0c）后唯独此门漏绑，恒用默认 0.2，
                # 门在检验另一个口径的统计量（生产 k_frac=0.1 下 top-20%
                # 组差 vs 处处 top-10%）。G2 恒毛口径（dc6a42a）不变
                import functools as _ft
                stat = _ft.partial(spread_ir_statistic,
                                   k_frac=float(env.calibration.tail_k))
            return noise_test(fn, env,
                              int(params.get("m", 100) or 100),
                              int(params.get("base_seed", 0) or 0),
                              statistic=stat)
        if method == "factor.tail_placebo":
            # G1 权威 placebo（WS2 2026-08-25）——in_process 分发，
            # 与 worker.run_request 同口径（import 在调用点：可 monkeypatch）
            import pandas as _pd
            from .factor.evaluate import _env_horizon_view, _forward_returns, _pit_mask
            from .factor.tail import topn_placebo
            _h = params.get("horizon")
            try:
                _h = int(_h) if _h not in (None, "") else None
            except (TypeError, ValueError):
                _h = None
            F = fn(env)
            v = _env_horizon_view(env, _h) if _h is not None else env
            dev_end = v.calibration.dev_end
            t_end = (int(np.searchsorted(v.dates, _pd.Timestamp(dev_end)))
                     if dev_end is not None else int(v.T))
            budget = params.get("budget_secs")
            return topn_placebo(
                F, _forward_returns(v), _pit_mask(v), v, t_end,
                int(params.get("draws", 120) or 120),
                int(params.get("seed", 0) or 0),
                budget_secs=(float(budget) if budget not in (None, "") else None))
        if method == "factor.day_perm_test":
            # 日期置换 null（2026-08-25）——in_process 分发
            from .factor.permute import day_permutation_test
            return day_permutation_test(fn, env,
                                        int(params.get("m", 200) or 200),
                                        int(params.get("base_seed", 0) or 0))
        if method == "factor.flatness_test":
            # 参数平坦性（2026-08-25）——变体在函数内逐个编译评估
            # （无独立工具通道：submit 时引擎自主执行，防预收割）
            from .factor.flatness import flatness_test
            return flatness_test(source, env,
                                 params.get("flatness") or [],
                                 compile_fn=self._compile_factor,
                                 budget_secs=float(
                                     params.get("budget_secs", 90.0) or 90.0))
        if method == "factor.audit":
            return audit_mod.audit(fn, env)
        raise BridgeError(-32602, f"未知 factor 方法: {method}")

    def _enforce_causality(self, source: str, env, env_id: str | None = None) -> dict[str, Any]:
        """H3 前置强制：evaluate 前该 source 必须有 causal verdict。FUTURE_LEAK 直接拒绝评估。

        缓存 key = 源码hash : 环境三元组指纹（数据文件+口径+引擎版本）——
        同一源码换环境/换数据文件后不得误命中旧结论：扰动法依赖 env 尺寸与数据，
        跨环境的前视结论不可移植。纪律变机制，不靠 agent 自觉。
        P4 Tier 1：A 类确定性反模式同样在此硬拒（所有评估路径的必经门——
        单因子/batch 逐成员/composite 全覆盖；拒绝评估 = 不计 trial）。"""
        ineff = scan_inefficiency(source)
        if ineff.get("hard_reject"):
            pats = "；".join(f"L{h.get('line')} {h.get('pattern')}"
                             for h in ineff.get("class_a", [])[:3])
            raise BridgeError(
                -32003,
                f"因子源码含 A 类确定性反模式，拒绝评估（不计入 trial）：{pats}。"
                "这些写法在 factor(env) 中没有合法用途（dsh_factor_mining.factor.ops "
                "向量化算子库已覆盖等价表达）——按各条目内嵌的替换模板改写后重试。"
                "拒绝发生在评估之前：零计算消耗、零账本写入。")
        key = self._causality_cache_key(source, env_id or "primary")
        cached = self._causality_cache.get(key)
        if cached is not None:
            if cached.get("verdict") == "FUTURE_LEAK":
                raise BridgeError(-32003,
                                  f"因子未通过因果检测（{cached.get('note','')}）——禁止评估，先修前视")
            return cached
        verdict = self._run_factor("factor.check_causality", source, {}, env)
        self._causality_cache[key] = verdict if isinstance(verdict, dict) else {"verdict": "unknown"}
        if isinstance(verdict, dict) and verdict.get("verdict") == "FUTURE_LEAK":
            raise BridgeError(-32003,
                              f"因子未通过因果检测（{verdict.get('note','')}）——禁止评估，先修前视")
        return verdict

    def _causality_cache_key(self, source: str, env_id: str) -> str:
        """缓存 key = 源码hash : 环境三元组指纹（跨环境/换数据不误命中旧结论）。"""
        try:
            resolved = self._resolve_env_id(env_id or "primary")
            env_key = self._env_full_fingerprint(resolved) or resolved
        except Exception:
            env_key = env_id or ""
        return f"{source_fingerprint(source)}:{env_key}"

    # ---- S1 拒绝即信息门（探索相似性防线 2026-09-04） ----

    def _novelty_index(self) -> "dict[str, tuple[list, dict]]":
        """trail_engine 的 {hash: (construction_fp, 首见条目)} 索引
        （mtime 缓存，写盘后自动失效）。旧条目无 fp 的不入索引——与
        _entries_linked「无信号条目是单例族」语义一致。"""
        p = Path(self.state_root) / "trail_engine.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            self._novelty_idx_cache = None
            return {}
        cache = getattr(self, "_novelty_idx_cache", None)
        if cache and cache[0] == mtime:
            return cache[1]
        idx: "dict[str, tuple[list, dict]]" = {}
        try:
            for e in json.loads(p.read_text(encoding="utf-8")):
                fp, h = e.get("construction_fp"), e.get("source_hash")
                if (isinstance(fp, list) and len(fp) == _FP_N_HASHES
                        and h and h not in idx):
                    idx[h] = (fp, e)   # 首见 = 最早先例
        except Exception:
            idx = {}
        self._novelty_idx_cache = (mtime, idx)
        return idx

    def _novelty_gate_log(self, event: dict) -> None:
        """防线遥测（S1/S2 共用 novelty_gate.jsonl；写失败静默）。"""
        try:
            with open(Path(self.state_root) / "novelty_gate.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(
                    {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event},
                    ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _enforce_novelty_gate(self, source: str, env_id: str,
                              member: "str | None" = None) -> None:
        """拒绝即信息门：提交侧零成本拦截同构重交（零 trial，同因果门
        路径哲学）。

        判据（审计校准）：对本环境先例的最大 MinHash 相似 ≥0.80 且数值
        常量未变（先例源码缺失退 ≥0.95 保守线）。拦截消息携带先例完整
        结果——浪费的算力变免费信息，确有增量会被「说明本质差异」逼出
        来。常量有变 = 参数扫描（合法深挖，I1 证无边际衰减），放行仅记
        遥测。作用域：仅新提案（development/batch 提交路径）；同 hash
        重评 / selection / test / composite 复评不经此门；跨环境同码
        （换数据重挖）不拦——那是新研究。模式：DSH_FACTOR_NOVELTY_GATE
        = enforce（默认）/ shadow（只记不拦）/ off。fail-open：门自身
        故障绝不阻断评估（与烟测门同哲学）。"""
        mode = os.environ.get("DSH_FACTOR_NOVELTY_GATE", "enforce").strip().lower()
        if mode == "off":
            return
        try:
            h = source_fingerprint(source)
            idx = self._novelty_index()
            if not idx:
                return
            # 同 hash 在本环境已有条目 = 复评（eval_cache 域），非本门职责
            hit = idx.get(h)
            if hit is not None and hit[1].get("envId") == env_id:
                return
            fp = _construction_fingerprint(source)
            best_sim, best_h, best_e = 0.0, None, None
            for ph, (pfp, pe) in idx.items():
                if ph == h or pe.get("envId") != env_id:
                    continue
                s = _fingerprint_similarity(fp, pfp)
                if s > best_sim:
                    best_sim, best_h, best_e = s, ph, pe
            if best_h is None or best_sim < _NOVELTY_GATE_SIM:
                return
            # 常量核对：先例源码可取才启用 0.80 线，否则保守 0.95
            twin_src = None
            try:
                pp = factor_source_path(best_h, self.state_root)
                if pp.exists():
                    twin_src = pp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                twin_src = None
            base = {"gate": "novelty", "hash": h, "twin": best_h,
                    "sim": round(best_sim, 3), "member": member, "env": env_id}
            if twin_src is not None:
                if _numeric_literals(source) != _numeric_literals(twin_src):
                    self._novelty_gate_log({**base,
                                            "decision": "allow_parameter_sweep"})
                    return
            elif best_sim < _NOVELTY_GATE_SIM_NOCODE:
                self._novelty_gate_log(
                    {**base, "decision": "allow_below_nocode_threshold"})
                return
            # 先例未 pass 的重交 = 修 bug/换实现的合法迭代（历史校准：无此
            # 条件时 8 个入册会被误拦，其中多为「v1 失败→微修→v2 入册」；
            # 加条件后误拦降为 4 且均有补救路径）。浪费的实锤形态是
            # 「先例已 pass 还原样重交」（26 对双通过对）——只拦这种。
            if best_e.get("verdict") != "pass":
                self._novelty_gate_log({**base,
                                        "decision": "allow_twin_not_passed"})
                return
            acc = any(isinstance(r, dict) and r.get("source_hash") == best_h
                      and r.get("accepted")
                      for r in read_registry(self.state_root))
            ev = (f"先例 {best_h[:12]}（{best_e.get('ts', '?')}，"
                  f"lane={str(best_e.get('lane', '?'))[:24]}，"
                  f"IC_IR={best_e.get('ic_ir')}，verdict={best_e.get('verdict')}"
                  f"{'，已入册' if acc else ''}）")
            self._novelty_gate_log({**base, "decision": f"block_{mode}"})
            if mode == "shadow":
                return
            who = f"成员 {member!r}" if member else "因子"
            raise BridgeError(
                -32003,
                f"同构重交拦截（{who}，对先例结构相似 {best_sim:.2f}，数值常量"
                f"未变）：{ev}。同构造重评是零信息增量（审计实证：26 对双双"
                "pass 的近重复对 |IC_IR| 差中位 0.008）。拒绝发生在评估之前："
                "零计算、零 trial。出路三选一：(1)确有本质差异→修改构造或"
                "常量后重交；(2)本轮本就是变体→在 trail 里如实以「变体」"
                "记录（变体是合法深挖，不会被拦）；(3)只是要复评同一因子→"
                "原 hash 结果可直接查（eval_cache/trail_summary）。")
        except BridgeError:
            raise
        except Exception:
            self._novelty_gate_log({"gate": "novelty", "decision": "error_open",
                                    "err": traceback.format_exc(limit=2)})

    # ---- S2 宣言分型校验（探索相似性防线 2026-09-04） ----

    def _source_of(self, source_hash: "str | None") -> "str | None":
        """按哈希取源码（带进程级缓存；缺文件返回 None）。"""
        if not source_hash:
            return None
        cache = getattr(self, "_attrs_src_cache", None)
        if cache is None:
            cache = self._attrs_src_cache = {}
        if source_hash in cache:
            return cache[source_hash]
        try:
            p = factor_source_path(source_hash, self.state_root)
            src = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
        except Exception:
            src = None
        cache[source_hash] = src
        return src

    def _declaration_gate(self, entry: dict) -> None:
        """「新」宣称与结构对账（H4 实证：42% 的「新方向」轮旗舰与先例
        近重复，而变体宣言轮均值全场最低 0.319——标注能力在，失真集中
        在「新」字滥用）。

        - 宣称 新信息源/新维度/首次引入 → 本轮（上次 record_trail 以来
          本线引擎条目）的 L1 数据列相对本环境先例必须有增量。
        - 宣称 新构造/新方向/新机制 → 旗舰对先例最大指纹相似 <0.6 或
          L1 有增量；否则应记「变体」。
        防寒蝉：证据不足（本轮无可对账条目/源码缺失/无先例）一律放行。
        DSH_FACTOR_DECL_GATE=off 可关。拒收走穷尽宣告同款写入口错误
        （agent 改写后重记，五要素不变）。fail-open：校验故障只记遥测。"""
        if os.environ.get("DSH_FACTOR_DECL_GATE", "on").strip().lower() == "off":
            return
        text = f"{entry.get('new_information', '')}|{entry.get('signal', '')}"
        claims_info = any(m in text for m in _DECL_INFO_MARKERS)
        claims_construct = any(m in text for m in _DECL_CONSTRUCT_MARKERS)
        p = Path(self.state_root) / "trail_engine.json"
        engine: list = []
        if p.exists():
            try:
                engine = json.loads(p.read_text(encoding="utf-8")) or []
            except Exception:
                engine = []
        lane = current_lane()
        own = own_entries(engine, lane)
        bucket = ((read_mining_state(self.state_root).get("lanes") or {})
                  .get(lane) or {})
        seen = int(bucket.get("decl_seen_engine") or 0)
        rnd = [e for e in own[seen:]
               if isinstance(e.get("construction_fp"), list)
               and len(e.get("construction_fp")) == _FP_N_HASHES]
        # 游标无条件推进（每条 record_trail = 一轮结束，无论有无「新」宣称
        # ——否则无宣称轮的条目会泄进下一轮的对账范围）
        try:
            lane_decl_seen_update(self.state_root, lane, seen=len(own),
                                  previous=seen)
        except Exception:
            pass
        if not (claims_info or claims_construct):
            return
        if not rnd:
            return   # 本轮无可对账条目：想法宣言，放行（防寒蝉）
        env_ids = {e.get("envId") for e in rnd if e.get("envId")}
        round_hashes = {e.get("source_hash") for e in rnd}
        prior = [e for e in engine
                 if isinstance(e.get("construction_fp"), list)
                 and len(e.get("construction_fp")) == _FP_N_HASHES
                 and e.get("envId") in env_ids
                 and e.get("source_hash") not in round_hashes]
        # L1 增量（源码缺失 = 证据不足 → 放行）
        round_attrs: set = set()
        for e in rnd:
            src = self._source_of(e.get("source_hash"))
            if src is None:
                self._novelty_gate_log({"gate": "declaration",
                                        "decision": "allow_missing_source"})
                return
            round_attrs |= _env_attrs(src)
        prior_attrs: set = set()
        for e in prior:
            src = self._source_of(e.get("source_hash"))
            if src is not None:
                prior_attrs |= _env_attrs(src)
        l1_inc = bool(round_attrs - prior_attrs)
        # 旗舰 = 本轮对先例最大相似
        flagship_sim = 0.0
        for e in rnd:
            fp = e["construction_fp"]
            for q in prior:
                s = _fingerprint_similarity(fp, q["construction_fp"])
                flagship_sim = max(flagship_sim, s)
        evt = {"gate": "declaration", "round_n": len(rnd),
               "claims": ("info" if claims_info else "") +
                         ("+construct" if claims_construct else ""),
               "flagship_sim": round(flagship_sim, 3), "l1_increment": l1_inc,
               "round_attrs": sorted(round_attrs)}
        if claims_info and not l1_inc:
            evt["decision"] = "reject_no_l1_increment"
        elif claims_construct and flagship_sim >= _DECL_FLAGSHIP_MAX_SIM and not l1_inc:
            evt["decision"] = "reject_flagship_near_dup"
        else:
            evt["decision"] = "allow"
            self._novelty_gate_log(evt)
            return
        self._novelty_gate_log(evt)
        raise BridgeError(
            -32602,
            f"new_information 宣言被拒收（宣称{('新信息源' if claims_info else '新构造')}"
            f"，但结构对账不符）：本轮 {len(rnd)} 个试验的数据列 "
            f"{sorted(round_attrs)} 相对先例无新增"
            + (f"，且旗舰构造对先例最大相似 {flagship_sim:.2f}（≥0.6）" if flagship_sim else "")
            + "。口径/文献引用/机制叙事都不构成新信息源（审计实证：42% 的"
            "「新方向」宣言轮旗舰与先例近重复）。改写三选一：(1)如实记为"
            "「变体」——变体是合法深挖，不会被拒；(2)确有新数据列/新构造"
            "→修正宣言措辞为可对账描述后重记；(3)先补充说明与先例的本质"
            "结构差异再宣称「新」。")

    def _factor_check_causality(self, params):
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        source = params.get("source", "")
        # 低效模式扫描（效率防御层1）：A 类硬拒（与 _enforce_causality 同判，
        # agent 显式 check 时即看到拒绝与替换模板）；B 类 warning 附结果
        ineff = scan_inefficiency(source)
        if ineff.get("hard_reject"):
            pats = "；".join(f"L{h.get('line')} {h.get('pattern')}"
                             for h in ineff.get("class_a", [])[:3])
            raise BridgeError(
                -32003,
                f"因子源码含 A 类确定性反模式，拒绝评估（不计入 trial）：{pats}。"
                "按各条目内嵌的替换模板改写后重试——ops 向量化算子库已覆盖等价表达。")
        result = self._run_factor("factor.check_causality", source, params, env)
        if isinstance(result, dict):
            result["causal"] = result.get("verdict") == "causal"
            if not ineff.get("ok", True):
                result["inefficiency_warning"] = ineff
            self._causality_cache[self._causality_cache_key(source, env_id)] = result
        return result

    @staticmethod
    def _spectral_m_eff(R: "np.ndarray") -> float:
        """谱有效检验数（GWAS Cheverud/Nyholt/Li-Ji）：M_eff = (Σλ)²/Σλ²。

        λ 为相关矩阵特征值（负特征值截零 PSD 化——逐对 |ρ| 组装的矩阵可能
        非 PSD）。恒等阵 → M；全相关阵 → 1。M>1 时调用方保证 R 是方阵。
        数值异常时保守退化为 M（纯计数）。"""
        M = len(R)
        try:
            w = np.linalg.eigvalsh(R)
            w = np.clip(w[np.isfinite(w)], 0.0, None)
            s = float(w.sum())
            if s <= 0:
                return float(M)
            return float(s * s / float((w * w).sum()))
        except Exception:
            return float(M)

    def _n_eff_from_entries(self, entries: list[dict], source_hash: str | None = None,
                            horizon: int | None = None,
                            cross_h_prior: dict | None = None,
                            main_horizon: int | None = None,
                            sampler: "_LuckSampler | None" = None) -> dict:
        """trail 级选择运气统计（v3 2026-08-21：E[max|X|] 直算 + 尾对齐 R）。

        口径不变：探索过程中**所有计算过的因子**（trail_engine 全体——评估
        成功即写、verdict=fail 也算；选择偏差发生在评估时刻，不是注册时刻）；
        试验单元 (source_hash, horizon)，同因子同 horizon 重评 = 更新条目
        不新增（v2 语义保留）。

        v3 相对 v2 的三处升级（对照实验：74 条真实 trail）：
        1. R 构建：尾对齐**全对**实测（v2 按 sketch 长度分组——长度随回看窗
           变化，2701 对只实测 275 对（10.2%），跨长度同族被静默置独立）。
           缺测对 → 同 hash 跨 horizon 先验 → 0（独立=最贵假设，缺证据保守）。
        2. 门量：bar_sigma = E[max|X|]（_LuckSampler CRN 直算，双侧——
           agent 按 |IC| 挑最优含符号事后翻转）。v2 谱 (Σλ)²/Σλ² → B-LP
           链条退役：有效自由度统计量 ≠ 期望最大值预测器（弥散相关
           低估选运 3.4 倍）。谱 M_eff 保留为遥测（n_eff 字段）。
        3. 包络（v4 2026-09-13 计价审计：摘除）：E[max|X|] 在 CRN 下
           天然单调（superset 逐点 max ≥ subset——定理），防灌水由该定理
           独立保证;旧包络(章回读 max + blp 旧章换算)是单向棘轮,曾把
           R 构造事故的高估永久锁死(3.35→4.58,详见 _build_R 事故链)。

        返回 stats dict：n_trials（试验计数 M，遥测）/ n_eff（谱 M_eff，
        遥测）/ bar_sigma（E[max|X|] σ 单位，**门参数**，含包络）/
        nu（ν 残差方差占比遥测，sampler 缺席时 None）。

        退化兼容：全体无 sketch → R=I → bar = M 个独立试验的双侧选运
        （≠ v2 的计数 M——不同的量：独立 M 个的 E[max|Z|]）；M=1 →
        E|Z|≈0.798（冷启动选运底价：连符号都是选出来的）；旧条目无
        horizon 字段 → 按 main_horizon 归位。
        """
        # 1) 试验集合：(hash, horizon) 去重。
        # v4（2026-09-13 计价审计）：包络摘除——章(bar_sigma_at_write /
        # blp(n_eff_at_write) 旧章换算)回读进门是单向棘轮,把 R 构造事故
        # (abs+逐对短重叠 → 非 PSD → 投影伪共同因子)的高估永久锁死
        # (生产实测:诚实 3.35 → 章 4.58,超 iid 上限)。防灌水的正确
        # 保证是 CRN 单调性定理(试验集只增,E[max] 逐点不减)——采样器
        # 裸值即门值;章仍写盘,但只作审计轨迹,不回读。
        trials: dict[tuple, list | None] = {}
        for e in entries:
            if not isinstance(e, dict):
                continue  # 生产审核 R1：畸形条目（null/字符串等）跳过
            h = e.get("source_hash")
            if not h:
                continue
            eh = e.get("horizon")
            if eh is None and main_horizon is not None:
                eh = main_horizon  # 旧条目按主 horizon 归位
            key = (h, eh)
            s = e.get("ic_series_sketch")
            s = s if isinstance(s, list) and len(s) >= 5 else None
            if key not in trials:
                trials[key] = s
            elif s is not None:
                trials[key] = s  # 重复条目：带 sketch 的覆盖（重评补 sketch）
        # 本因子（未入 trail 时）：pending 单例（horizon 归一到主 horizon 口径）
        if source_hash:
            ph = horizon
            if ph is None and main_horizon is not None:
                ph = main_horizon
            if (source_hash, ph) not in trials:
                trials[(source_hash, ph)] = None

        keys = list(trials)
        M = len(keys)
        if M == 0:
            return {"n_trials": 0, "n_eff": 0.0,
                    "bar_sigma": 0.0, "nu": None}

        # 2) 跨 horizon 先验（同 hash 跨 horizon、实测不可测时兜底）
        def _prior(ki, kj):
            (h1, o1), (h2, o2) = ki, kj
            if h1 == h2 and o1 is not None and o2 is not None and o1 != o2:
                v = (cross_h_prior or {}).get(f"{min(o1, o2)}|{max(o1, o2)}")
                if isinstance(v, (int, float)) and np.isfinite(v):
                    return abs(float(v))
            return None

        # 3) E[max|X|]（CRN 采样器；传入实例则增量条件采样）+ 包络
        smp = sampler if sampler is not None else _LuckSampler()
        smp.sync(trials, prior_fn=_prior)
        bar = smp.bar_sigma()  # v4：裸值即门值（CRN 单调性=防灌水保证）
        n_spec = Bridge._spectral_m_eff(smp._R) if M > 1 else 1.0
        return {"n_trials": M, "n_eff": float(n_spec),
                "bar_sigma": float(bar), "nu": smp.nu_telemetry()}

    def _main_horizon(self, env_id: str) -> int | None:
        """环境主 horizon（trail 旧条目归位 / per-horizon 基线键）。env 未加载 → None。"""
        try:
            env = self.envs.get(self._resolve_env_id(env_id))
            if env is not None:
                return int(env.calibration.horizon)
        except Exception:
            pass
        return None

    def _landscape_ic_quantiles(self, landscape, env_id: str,
                                horizon: int | None = None) -> dict | None:
        """指纹门后的 per-horizon IC_IR 分位 dict（v2）。

        新格式（horizons 字段存在）：ic_ir 按 str(horizon) 分键；horizon=None
        → 主 horizon。旧平铺格式：仅主 horizon 有效（非主 horizon 请求 → None，
        引导重校准——不给跨口径的不可信数字）。"""
        if not isinstance(landscape, dict) or not landscape.get("ic_ir"):
            return None
        if self._landscape_fingerprint_status(landscape, env_id) != "match":
            return None
        q = landscape["ic_ir"]
        main_h = self._main_horizon(env_id)
        if isinstance(landscape.get("horizons"), list):
            h = horizon if horizon is not None else main_h
            if h is None:
                return None
            sub = q.get(str(h))
            return sub if isinstance(sub, dict) else None
        # 旧平铺格式：只代表主 horizon 的基线
        if horizon is not None and main_h is not None and horizon != main_h:
            return None
        return q if isinstance(q, dict) else None

    def _cross_h_prior_loaded(self, env_id: str) -> dict | None:
        """指纹门后的跨 horizon 相关先验（null 校准实测；缺 → None 按独立保守计）。"""
        try:
            from .factor import random_gen as _rg
            land = _rg.read_null_landscape(self.state_root)
            if not isinstance(land, dict):
                return None
            ch = land.get("cross_horizon_corr")
            if isinstance(ch, dict) and self._landscape_fingerprint_status(land, env_id) == "match":
                return ch
        except Exception:
            pass
        return None

    def _landscape_pool_std(self, landscape, env_id: str,
                            horizon: int | None = None) -> float | None:
        """null 地形 → pool_std 估计（含指纹硬门，v2 per-horizon）。无效 → None。

        返回 None 时 evaluate 路径不注入 pool_std → N>1 的 deflated p 拒绝
        给出（D7 链路自然引导重校准），而不是拿错基线给不可信数字。"""
        q = self._landscape_ic_quantiles(landscape, env_id, horizon)
        if not isinstance(q, dict):
            return None
        p10, p90 = q.get("p10"), q.get("p90")
        if p10 is None or p90 is None:
            return None
        return (p90 - p10) / 2.5631

    def _landscape_tail_s0(self, env_id: str,
                           horizon: int | None = None) -> float | None:
        """null 地形 spread 段 → 尾部线 G3 的经验 s0（WS1 2026-08-25）。

        指纹门（match）+ spread 段存在 + std 有效 → 返回 std；否则 None
        （调用方降级解析式 1/√n_days，不拒绝给门——与 pool_std 的拒绝
        语义不同：解析式有理论依据且 G3 有 G1 前置门，经验值是校准增强；
        pool_std 缺失时 p 完全不可算才拒绝）。旧 landscape 无 spread 段
        → 同 legacy 处理（提示重跑校准）。"""
        from .factor import random_gen
        land = random_gen.read_null_landscape(self.state_root)
        if not isinstance(land, dict):
            return None
        if self._landscape_fingerprint_status(land, env_id) != "match":
            return None
        spread = land.get("spread")
        if not isinstance(spread, dict):
            return None
        h = horizon if horizon is not None else self._main_horizon(env_id)
        if h is None:
            return None
        sub = spread.get(str(int(h)))
        if not isinstance(sub, dict):
            return None
        std = sub.get("std")
        if isinstance(std, (int, float)) and math.isfinite(std) and std > 0:
            return float(std)
        return None

    def _tail_telemetry(self, engine_trail: list) -> dict:
        """尾部线遥测块（2026-08-27 规划书 WS-B.1，进 loop 响应）。

        从 tail_ledger 派生：n_trials_tail / N_eff / bar（当前 N_eff 与
        s0 现算，legacy 公式近似——t_adj 全量重算不可行也不必要）/
        near_misses（spread_ir ∈ [bar−0.10, bar) 最近 3 条）。给 agent
        「差一点」的方向感，替代 IC 单轨下的一片漆黑。账本为空 → 计数
        为 0 + bar=None，不炸。"""
        from .factor.tailgate import (tail_deflation_bar, tail_ledger,
                                      tail_near_misses, tail_n_eff)
        ledger = tail_ledger(engine_trail)
        n_eff, n_total = tail_n_eff(ledger)
        out = {"n_trials_tail": n_total, "n_eff": n_eff}
        periods = sorted(int(r["n_periods"]) for r in ledger
                         if isinstance(r, dict)
                         and isinstance(r.get("n_periods"), (int, float)))
        n_days = periods[len(periods) // 2] if periods else 0
        s0_emp = None
        env_id = next(iter(self.envs), None) if getattr(self, "envs", None) else None
        if env_id is not None:
            try:
                s0_emp = self._landscape_tail_s0(env_id)
            except Exception:
                s0_emp = None
        bar = tail_deflation_bar(max(n_eff, 1), n_days, s0_emp=s0_emp)
        if isinstance(bar, dict) and bar.get("ok"):
            out["bar"] = {"value": bar.get("bar"),
                          "s0_source": bar.get("s0_source")}
            out["near_misses"] = tail_near_misses(ledger,
                                                  float(bar["bar"]))
            out["note"] = ("尾部线接近门槛的方向（spread_ir 距 bar < 0.10）"
                           "——若与当前族不同源，优先构造变体")
        else:
            out["bar"] = None
            out["near_misses"] = []
            out["note"] = ("尾部线 deflation 门暂不可计算"
                           f"（{bar.get('reason') if isinstance(bar, dict) else 'bar 缺失'}"
                           "）——有 tail 块的评估积累后自动出现")
        return out

    def _deepen_telemetry(self, engine_trail: list) -> dict:
        """IC 线近失深化遥测（2026-09-15 正修：进 loop 响应，与尾部线
        near_misses 对称）。

        背景：IC 线此前无任何近失指令（尾部线才有 near_misses），深化
        全靠 agent 自觉——deepdig 清单自写"变体配额≥1/2"执行率 0/30
        实证失效（散文纪律不驱动行为的第三次同型）。直驱 queue/runner
        架构又把模型移出了逐响应反馈环（loop 指令落盘无人读）。

        口径：
        - 近失带 = **现价**重算 deflated p ∈ (0.05, 0.30]（|sr_hat|≥0.75
          预筛省算力；p 用当前 bar 与 per-horizon pool_std 重算——墙在动，
          旧 p 不代表现价，这正是"重定价重扫"的引擎化）；
        - 候选 = 全局 trail（事实层，与 tail 遥测同口径）dev/batch 条目、
          (hash, horizon) 去重取最新、未入册；
        - 每颗带引擎计数的**已试变体数**（_entries_linked 双信号：IC
          相关 ≥0.6 或构造血缘——同族后继试验，ts 晚于种子且 hash 不同；
          **只计 authored/假设来源**——farm 撞相关后代不算深化投资）。
        - 指令面：变体对墙几乎免费（相关试验在 R 里折成同一张彩票——
          P0c 后深化的经济学），队列非空时新构造:变体配额 2:1。
        """
        env_id = next(iter(self.envs), None) if getattr(self, "envs", None) else None
        if env_id is None:
            return {"queue": [], "note": "env 未加载——IC 线近失现价不可算"}
        main_h = self._main_horizon(env_id)
        reg = read_registry(self.state_root)
        admitted = {e.get("source_hash") for e in reg
                    if isinstance(e, dict) and e.get("accepted")
                    and e.get("source_hash")}
        # 全局 bar（实例级 CRN 采样器，增量 sync 幂等）
        try:
            stats = self._n_eff_from_entries(
                engine_trail, None, main_horizon=main_h, sampler=self._luck)
            bar = float(stats.get("bar_sigma") or 0.0)
        except Exception:
            return {"queue": [], "note": "选择运气采样失败——近失现价不可算"}
        if bar <= 0:
            return {"queue": [], "note": "trail 空——无近失可言"}
        # (hash, horizon) 去重取最新；|sr|≥0.75 预筛
        cand: dict[tuple, dict] = {}
        for e in engine_trail:
            if not isinstance(e, dict) or e.get("stage") not in ("development", "batch"):
                continue
            h = e.get("source_hash")
            d = e.get("dsr_stats") or {}
            sr = d.get("sr_hat")
            if (not h or h in admitted
                    or not isinstance(sr, (int, float))
                    or abs(float(sr)) < 0.75):
                continue
            k = (h, e.get("horizon"))
            if k not in cand or (e.get("ts") or "") > (cand[k].get("ts") or ""):
                cand[k] = e
        # 现价 p 重算（pool_std per-horizon 本地缓存）
        pool_cache: dict = {}
        rows = []
        for (h, hor), e in cand.items():
            d = e.get("dsr_stats") or {}
            if hor not in pool_cache:
                try:
                    pool_cache[hor] = self._resolve_pool_std(env_id, None, hor)[0]
                except Exception:
                    pool_cache[hor] = None
            ps = pool_cache[hor]
            if not ps:
                continue
            p = _dsr_p_from_stats(d.get("sr_hat"), d.get("skew"),
                                  d.get("kurt"), d.get("n_obs"), bar, ps)
            if isinstance(p, (int, float)) and 0.05 < p <= 0.30:
                rows.append((float(p), e))
        rows.sort(key=lambda r: r[0])
        # ---- 深化战役(用户 2026-09-15 拍板:浅挖近失逐颗深挖,穷尽由引擎
        # 机械判定——变体 ≥ 预算,模型无权宣布穷尽)----
        # 状态文件 stateRoot/deepen_campaign.json:
        #   {"budget": null|N, "queue": [hash...按现价升序], "history":
        #    [{"hash","outcome","ts","variants"}]}
        # 预算缺省(null/缺键)= DSH 插件簇试验上限 max_cluster_trials
        # (用户拍板"预算和DSH插件里的预算保持一致";配置改,战役随动)。
        # 推进规则(每次遥测重估,推进即落盘):入册→admitted;出带
        # (现价>0.30/预筛外)→out_of_band;战役新鲜窗内 authored 变体
        # ≥ 预算→exhausted_trials(引擎穷尽宣告);否则 active=当前目标。
        campaign = {"state": "idle"}
        resolved = set()
        camp_path = Path(self.state_root) / "deepen_campaign.json"
        try:
            camp = (json.loads(camp_path.read_text(encoding="utf-8"))
                    if camp_path.exists() else None)
        except Exception:
            camp = None
        if isinstance(camp, dict) and camp.get("queue"):
            if isinstance(camp.get("budget"), int) and camp["budget"] > 0:
                budget = int(camp["budget"])
            else:
                budget = int(MINING_CONFIG.get("max_cluster_trials", 200))
            cq = [str(h) for h in camp.get("queue") or []]
            hist = list(camp.get("history") or [])
            done = {h.get("hash") for h in hist if isinstance(h, dict)}
            # 执行器回填的显式裁决(如"复合变体已入册,其腿源种子皆解决")
            # ——复合与腿的 sketch 相关常 <0.6,自动家族胜利判不出来;
            # 回填须带 via+note,引擎优先消费并转录进 history
            force = [f for f in (camp.get("force_resolve") or [])
                     if isinstance(f, dict) and f.get("hash")]
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            for f in force:
                fh = str(f["hash"])
                if fh in cq and fh not in done:
                    hist.append({"hash": fh, "ts": now,
                                 "outcome": str(f.get("outcome")
                                                or "resolved"),
                                 "via": str(f.get("via") or "")[:12],
                                 "note": str(f.get("note") or "")[:160]})
                    done.add(fh)
                    resolved.add(fh)
            cursor = 0
            while cursor < len(cq):
                tgt = cq[cursor]
                if tgt in done:
                    cursor += 1
                    continue
                row = next((e for _p, e in rows
                            if e.get("source_hash") == tgt), None)
                if tgt in admitted or row is None:
                    # 入册即胜利出队;不在带内=现价出带(>0.30)或预筛外
                    hist.append({"hash": tgt, "ts": now,
                                 "outcome": ("admitted" if tgt in admitted
                                             else "out_of_band")})
                    resolved.add(tgt)
                    cursor += 1
                    continue
                # 家族胜利(2026-09-15 战役实测定律):深化的正解几乎总是
                # "目标的变体入册"而非母式本体入册(volmom_winsor_only/
                # triple4leg_amtprice/campaign1 复合皆然)——目标的任一
                # 更晚的同族条目已入册 → admitted_via_variant 出队
                ts_seed = row.get("ts") or ""
                fam_win = None
                for ah in admitted:
                    ae = next((x for x in engine_trail
                               if isinstance(x, dict)
                               and x.get("source_hash") == ah), None)
                    if (ae is not None
                            and (ae.get("ts") or "") > ts_seed
                            and _entries_linked(row, ae)):
                        fam_win = ah
                        break
                if fam_win is not None:
                    hist.append({"hash": tgt, "ts": now,
                                 "outcome": "admitted_via_variant",
                                 "via": str(fam_win)[:12]})
                    resolved.add(tgt)
                    cursor += 1
                    continue
                ts_seed = row.get("ts") or ""
                # 新鲜窗语义:预算只计战役启动(started)之后的 authored
                # 变体——老种子的历史 authored 表亲(相关≥0.6 即计入)会
                # 把存量灌到数百发,不清零则未挖先"穷尽",违背逐颗深挖
                camp_start = str(camp.get("started") or "1970-01-01T00:00:00")
                k = sum(
                    1 for x in engine_trail
                    if isinstance(x, dict)
                    and x.get("source_hash") != tgt
                    and (x.get("ts") or "") > max(ts_seed, camp_start)
                    and (x.get("authored") or x.get("origin") == "hypothesis")
                    and _entries_linked(row, x))
                tgt_p = next(p for p, e in rows
                             if e.get("source_hash") == tgt)
                if k >= budget:
                    hist.append({"hash": tgt, "ts": now,
                                 "outcome": "exhausted_trials",
                                 "variants": k})
                    resolved.add(tgt)
                    cursor += 1
                    continue
                campaign = {
                    "state": "active", "target": {
                        "hash_prefix": str(tgt)[:12], "p_now": round(tgt_p, 4),
                        "variants": k, "budget": budget},
                    "progress": f"{cursor + 1}/{len(cq)}",
                    "remaining": len(cq) - cursor}
                break
            else:
                campaign = {"state": "complete",
                            "resolved": len(hist),
                            "note": "深化战役队列已清——浅挖积压收官"}
            if campaign.get("state") in ("complete",) or cursor != int(
                    camp.get("cursor", -1)) or hist != camp.get("history"):
                try:
                    camp.update({"history": hist, "cursor": cursor})
                    tmp = camp_path.with_suffix(".json.tmp")
                    tmp.write_text(json.dumps(camp, ensure_ascii=False,
                                              indent=1), encoding="utf-8")
                    os.replace(str(tmp), str(camp_path))
                except Exception:
                    pass  # 战役状态写失败不阻断遥测;下次重算
        # 展示队列排除战役已决种子(入册/出带/引擎穷尽)
        if resolved:
            rows = [r for r in rows
                    if r[1].get("source_hash") not in resolved]
        queue = []
        for p, e in rows[:3]:
            ts_seed = e.get("ts") or ""
            # 只计 authored/假设来源的同族后继(2026-09-15 深度审计):
            # farm 随机撞相关后代会把计数灌到数百发,需求(变体<3)永不
            # 触发——锤过≠深化过;审计实证带内 73/77(IC)+22/27(tail)
            # 颗只吃过 winsor/换腿 两轴,加腿/拆杠杆几乎从未发生
            variants = sum(
                1 for x in engine_trail
                if isinstance(x, dict)
                and x.get("source_hash") != e.get("source_hash")
                and (x.get("ts") or "") > ts_seed
                and (x.get("authored") or x.get("origin") == "hypothesis")
                and _entries_linked(e, x))
            queue.append({"hash_prefix": str(e.get("source_hash"))[:12],
                          "sr": round(float((e.get("dsr_stats") or {}).get("sr_hat")), 3),
                          "p_now": round(p, 4),
                          "horizon": e.get("horizon"),
                          "variants_tried": variants,
                          "ts": ts_seed})
        camp_state = campaign.get("state")
        return {
            "queue": queue,
            "campaign": campaign,
            "note": (("深化战役 active:{}/{} 目标 {} k={}/{}——引擎逐颗推进,"
                      "穷尽=authored 变体≥预算(机械判定)".format(
                          campaign.get("progress"), campaign.get("remaining"),
                          campaign["target"]["hash_prefix"],
                          campaign["target"]["variants"],
                          campaign["target"]["budget"]) if camp_state == "active"
                      else ("深化战役 complete:浅挖积压收官" if camp_state == "complete"
                            else "")) +
                    ("IC 线近失（现价 p∈(0.05,0.30]，未入册）——变体对墙"
                     "几乎免费（相关试验≈同一张彩票）；每颗值得 2-3 发变体"
                     "（结构轴优先:winsor/换腿/加腿/拆杠杆，抖动轴失败不"
                     "构成穷尽）；队列非空时新构造:变体配额 2:1，已试变体"
                     "由引擎计数（仅 authored/假设来源，farm 撞相关不计）"
                     if queue else
                     "IC 线现价近失带为空"))}

    # ---- G1 权威 placebo 缓存（WS2 2026-08-25）----
    # 重 placebo 贵且确定性（seed = 指纹派生）——缓存键
    # (source_hash, horizon, k_frac, seed, draws)，有界 512，值含指纹
    # （跨环境不得误命中）。同前缀（hash/h/k/seed 相同）更高 draws 的
    # 条目可直接复用：同一确定性 rng，draws 更多 = 严格更多样本。

    @staticmethod
    def _placebo_cache_key(source_hash, horizon, k_frac, seed, draws) -> str:
        return f"{source_hash}|{horizon}|{k_frac}|{seed}|{int(draws)}"

    def _placebo_cache_read(self, env_id: str, source_hash: str,
                            horizon: int | None, k_frac: float,
                            seed: int, draws_req: int) -> dict | None:
        """命中返回 {z, draws, null_mean, null_std, periods}（附 from_cache）。

        精确键优先；否则同前缀里 draws 最大且 ≥60 的条目（确定性 rng
        前缀复用）。指纹不匹配 = 无效。"""
        if not source_hash:
            return None
        try:
            p = Path(self.state_root) / "placebo_cache.json"
            if not p.exists():
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(entries, dict):
                return None
            fp = self._env_full_fingerprint(env_id)
            prefix = f"{source_hash}|{horizon}|{k_frac}|{seed}|"
            best = None
            for k, e in entries.items():
                if not isinstance(e, dict) or e.get("env_fingerprint") != fp:
                    continue
                if not (k == prefix + str(int(draws_req))
                        or k.startswith(prefix)):
                    continue
                v = e.get("value")
                if not isinstance(v, dict):
                    continue
                d = v.get("draws")
                if not isinstance(d, (int, float)) or int(d) < 60:
                    continue
                if best is None or int(d) > int(best.get("draws") or 0):
                    best = v
            if best is not None:
                return {**best, "from_cache": True}
        except Exception:
            return None
        return None

    def _placebo_cache_write(self, env_id: str, source_hash: str,
                             horizon: int | None, k_frac: float,
                             seed: int, draws: int, value: dict) -> None:
        """原子写 + 有界 512（FIFO 挤出）。写失败不阻断 submit（缓存
        是加速器不是门输入——下次重算即可），stderr 可见。"""
        try:
            p = Path(self.state_root) / "placebo_cache.json"
            data = {"entries": {}, "order": []}
            if p.exists():
                try:
                    old = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(old, dict) and isinstance(old.get("entries"), dict):
                        data = old
                except Exception:
                    pass
            key = self._placebo_cache_key(source_hash, horizon, k_frac,
                                          seed, draws)
            data["entries"][key] = {
                "env_fingerprint": self._env_full_fingerprint(env_id),
                "value": value}
            order = [k for k in data.get("order", []) if k in data["entries"]]
            order.append(key)
            # 去重保序后 FIFO 挤出（对齐既有缓存纪律）
            seen = set()
            order = [k for k in order if not (k in seen or seen.add(k))]
            while len(order) > 512:
                data["entries"].pop(order.pop(0), None)
            data["order"] = order
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(str(tmp), str(p))
        except Exception as e:
            try:
                import sys as _sys
                _sys.stderr.write(
                    f"[placebo_cache] 写入失败（缓存不可用，submit 照常）: "
                    f"{type(e).__name__}: {e}\n")
            except Exception:
                pass

    def _tail_decay_lookup(self, env_id: str, source_hash: str,
                           result: dict) -> dict:
        """test 尾块 ↔ train 尾块对照（WS3 2026-08-25）：
        {spread_ir_train, spread_ir_test, ratio, sign_flip}——报告数字，
        不自动红旗（衰减多少算失败是校准问题，先给数字；翻号红旗 v2）。

        train 块取 dev/batch/composite 条目（stage 过滤与 tail_ledger/
        submit 反查同一防线口径）；test 评估恒在主 horizon（无 per-call
        horizon 通道），train 块按归一后的主 horizon 匹配。"""
        test_tail = result.get("tail") if isinstance(result.get("tail"), dict) else None
        test_ir = (test_tail or {}).get("spread_ir")
        main_h = self._main_horizon(env_id)
        train_ir = None
        for e in reversed(self._read_engine_trail()):
            if (not isinstance(e, dict)
                    or e.get("source_hash") != source_hash
                    or e.get("stage") not in
                    ("development", "batch", "composite")):
                continue
            tail = e.get("tail")
            if not isinstance(tail, dict) or tail.get("error"):
                continue
            eh = e.get("horizon")
            if eh is None and main_h is not None:
                eh = main_h
            if main_h is not None and int(eh) != int(main_h):
                continue
            train_ir = tail.get("spread_ir")
            break
        out = {"spread_ir_train": train_ir, "spread_ir_test": test_ir}
        if (isinstance(train_ir, (int, float)) and float(train_ir) != 0
                and isinstance(test_ir, (int, float))):
            out["ratio"] = round(float(test_ir) / float(train_ir), 4) + 0.0
            out["sign_flip"] = bool(
                np.sign(float(train_ir)) != 0 and np.sign(float(test_ir)) != 0
                and np.sign(float(train_ir)) != np.sign(float(test_ir)))
        elif train_ir is None:
            out["note"] = ("无 train 尾块（该 hash 的 dev/batch 评估不在场）"
                           "——衰减无从对照")
        return out

    def _resolve_pool_std(self, env_id: str, trail_pool_std: float | None,
                          horizon: int | None = None) -> tuple[float | None, dict | None]:
        """pool_std 解析（v4 2026-08-22：landscape-only）+ 信号探测器。

        v3 及以前（ADV-11）：max(trail_std, landscape)。其增量部分在
        landscape floor 已防住「同族刷屏压低 std」攻击后只剩一个功能——
        **把找到真信号本身当作 null 更宽的证据**（2026-08-22 会话轨迹
        取证：amihud 族 0.52~0.59 混入 trail → trail_std=0.30 赢 max →
        sr0 膨胀 70% → IC_IR=0.590 被拒 p=0.67。信号越好，门越高）。
        trail std 超过 null 地形宽度的常态解释是试验集含真信号——
        那是好消息，不是提高 null 检验标准的理由。

        v4：pool_std 仅取指纹门后的 null 地形（per-horizon）；
        trail_std 降级为**信号探测器**遥测（detector dict 随响应输出），
        不再进门。同族刷屏攻击仍被 landscape floor（0.179）独立防住。
        边界假设（诚实声明）：agent 手写因子的 null 宽度 ≈ 随机算子树
        的 null 宽度——rank IC 对截面单调变换不变，null 宽度主要由
        n_obs/horizon 决定，per-horizon 键覆盖；非定理，若弱族 trail_std
        持续显著大于 landscape，需重新审视（按族校准 null）。

        返回 (pool_std, detector)：
        - pool_std：landscape per-horizon 值；缺 → None（p 拒绝给出）
        - detector：trail_std ≥10 样本时给 {trail_std, null_std, ratio,
          signal_likelihood}——ratio>1.5 报「试验集大概率含真信号」"""
        from .factor import random_gen
        land_std = self._landscape_pool_std(
            random_gen.read_null_landscape(self.state_root), env_id, horizon)
        detector = None
        if isinstance(trail_pool_std, (int, float)) and trail_pool_std > 0:
            detector = {"trail_std": float(trail_pool_std),
                        "null_std": land_std,
                        "ratio": (float(trail_pool_std) / land_std
                                  if isinstance(land_std, (int, float)) and land_std > 0
                                  else None)}
            if detector["ratio"] is not None:
                r = detector["ratio"]
                if r >= 1.5:
                    detector["signal_likelihood"] = (
                        f"trail_std={trail_pool_std:.3f} 为 null 地形宽度的 {r:.1f} 倍"
                        "——试验集大概率含真信号（这不是提高门槛的理由）")
                elif r <= 0.5:
                    detector["signal_likelihood"] = (
                        f"trail_std={trail_pool_std:.3f} 显著窄于 null 地形"
                        f"（{land_std:.3f}）——疑似同族刷屏，仅按 null 尺度计价")
        return land_std, detector

    def _registered_hashes(self) -> set:
        """registered_ledger.jsonl 的 source_hash 集合(层成员资格;损坏→空)。"""
        out = set()
        p = Path(self.state_root) / "registered_ledger.jsonl"
        if not p.exists():
            return out
        try:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                    if isinstance(r, dict) and r.get("source_hash"):
                        out.add(r["source_hash"])
                except Exception:
                    continue
        except Exception:
            return set()
        return out

    def _trial_stats(self, source_hash: str | None = None,
                     horizon: int | None = None,
                     env_id: str | None = None,
                     stratum: str | None = None) -> tuple[dict, float | None]:
        """多重检验的引擎侧硬统计（v3 2026-08-21：E[max|X|] bar + per-horizon trail_std）。

        旧实现用 mining_state.round+1 计数——但 agent 走 factor_record_trail
        （append_trail）从不调 record_round，round 恒 0 → n_trials 恒 1，
        多重检验校正从未生效。真实计数在 trail_engine.json（evaluate/batch
        自动记录的硬事实）。

        返回 (stats, trail_std)：
        - stats = _n_eff_from_entries 的 dict（n_trials 计数 / n_eff 谱遥测 /
          **bar_sigma 门参数**（E[max|X|]，σ 单位，含包络）/ nu ν 遥测）。
          采样器用实例级 self._luck（CRN 增量，跨调用持久）
        - trail_std = **本 horizon** 的 trail 实测 IC_IR std（≥10 样本；不同
          horizon 的 null 宽度天然不同 1/√n，混算会互相污染。调用方经
          _resolve_pool_std 与 null 地形保守合并）。trail 损坏 → 按空 trail
          处理（bar 保守放行）但 **stderr 可见**——静默 N 重置是不可审计的
          事故温床（审核 ADV-12）。
        """
        entries = []
        p = Path(self.state_root) / "trail_engine.json"
        if p.exists():
            try:
                entries = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            except Exception as e:
                entries = []
                try:
                    import sys as _sys
                    _sys.stderr.write(
                        f"[trail_engine] 读取失败（按空 trail 处理，选运 bar 将低估）: "
                        f"{type(e).__name__}: {e}\n")
                except Exception:
                    pass
        prior = None
        main_h = None
        if env_id is not None:
            prior = self._cross_h_prior_loaded(env_id)
            main_h = self._main_horizon(env_id)
        _smp = self._luck
        if stratum == "mechanism":
            # 批次1(方案B):机制层子集 = 台账注册 hash 的试验(影子口径,
            # 正式翻转前不进门)。子集走独立 CRN 采样器,不扰动全局状态
            _reg = self._registered_hashes()
            entries = [e for e in entries
                       if isinstance(e, dict) and e.get("source_hash") in _reg]
            _smp = self._luck_mech
        stats = self._n_eff_from_entries(entries, source_hash, horizon,
                                         cross_h_prior=prior, main_horizon=main_h,
                                         sampler=_smp)
        # per-horizon trail IC_IR std（旧条目无 horizon → 归主 horizon）
        def _h_match(e) -> bool:
            if not isinstance(e, dict) or not isinstance(e.get("ic_ir"), (int, float)):
                return False
            eh = e.get("horizon")
            if eh is None and main_h is not None:
                eh = main_h
            if horizon is None:
                return eh is None or eh == main_h
            return eh == horizon
        irs = [e["ic_ir"] for e in entries if _h_match(e)]
        trail_std = None
        if len(irs) >= 10:
            trail_std = float(np.std(irs, ddof=1))
        return stats, trail_std

    # ---- 自主性停走接线（2026-08-21：散文纪律 → 引擎指令） ----
    # 事故模式（trail.json round 8/9 实证）：agent 在里程碑时刻主动停下
    # （"本次探索结束""等待用户决定"），或写了 next_hypothesis 不执行
    # （round 3 声明"需要全新信息源"后继续磨 TSI 变体 50 个）——用户被迫
    # 用固定话术（"更深度思考""从论文找灵感"）手动推动。SKILL.md 散文
    # （"挖掘阶段全程自主"）不驱动行为；本接线把停走权收进引擎响应。

    def _read_engine_trail(self) -> list:
        """trail_engine.json 读取（畸形/损坏 → 空 + stderr 可见）。"""
        entries = []
        p = Path(self.state_root) / "trail_engine.json"
        if p.exists():
            try:
                entries = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            except Exception:
                entries = []
        return entries

    def _family_chain(self, engine_trail: list) -> list:
        """当前族的试验链（按时间正序）：从最新向前走链，返回链上条目。

        链判定见 _entries_linked（双信号 OR：v5 构造血缘 + v3 IC）。
        与断链检测（arc_rounds 归零）共用同一谓词。"""
        if not engine_trail or not isinstance(engine_trail[-1], dict):
            return []
        latest = engine_trail[-1]
        chain = [latest]
        for e in reversed(engine_trail[:-1]):
            if not isinstance(e, dict) or not _entries_linked(latest, e):
                break
            chain.append(e)
        chain.reverse()
        return chain

    def _family_streak(self, engine_trail: list) -> int:
        """连续同族试验链长（v3 IC + v5 血缘双信号 OR，见 _family_chain）。"""
        return len(self._family_chain(engine_trail))

    def _family_marginal(self, engine_trail: list) -> dict:
        """族内边际收益（2026-08-25 用户决策：升级不看链长绝对值，只看
        边际收益）：

        - family_best：族历史最佳 |IC_IR|
        - recent_best：族内最近窗口（W=min(10, len//2)）最佳 |IC_IR|
        - marginal = recent_best − family_best_before（族历史在窗口前
          的最佳——正=还在超越，负=已在磨旧水平以下）
        - plateau_trials：从最后一个「创造族最佳」的试验到尾部的距离

        判定（纯边际，无链长阈值）：
        - marginal ≥ 0：族还在改善（新试验超越历史）→ 不催
        - marginal ∈ (-0.05, 0)：边际递减 → 建议换构造
        - marginal ∈ (-0.10, -0.05)：边际枯竭 → rotate
        - marginal < -0.10：严重枯竭 → literature（需要外部假设源）

        族内样本 < 5 → enough_data=False，不做判定（小族不催）。

        双线汇报（2026-08-27 规划书 WS-B.3）：diag 增 tail_marginal——
        族内 spread_ir 滑窗对族前史最佳（同窗口数学，spread 单侧不取
        绝对值）。_pick_strategy R1/R2 据两线都枯竭才触发；单线枯竭只出
        escalation。尾部线样本 < 5 → tail_marginal.enough_data=False，
        R1/R2 回到 IC 单轨行为。"""
        fam = self._family_chain(engine_trail)
        irs = [abs(e["ic_ir"]) for e in fam
               if isinstance(e, dict) and isinstance(e.get("ic_ir"), (int, float))
               and not isinstance(e.get("ic_ir"), bool)]
        sps = []
        for e in fam:
            if not isinstance(e, dict):
                continue
            tail = e.get("tail") if isinstance(e.get("tail"), dict) else None
            sp = tail.get("spread_ir") if tail else None
            if (isinstance(sp, (int, float)) and not isinstance(sp, bool)
                    and math.isfinite(float(sp))):
                sps.append(float(sp))
        out = None
        if len(irs) < 5:
            out = {"enough_data": False, "family_size": len(irs)}
        else:
            W = max(5, min(10, len(irs) // 2))
            if len(irs) <= W:
                out = {"enough_data": False, "family_size": len(irs)}
            else:
                family_best_before = max(irs[:-W])
                recent_best = max(irs[-W:])
                family_best = max(irs)
                marginal = recent_best - family_best_before
                # plateau_trials：尾部多少条未达到族最佳
                plateau = 0
                for ir in reversed(irs):
                    if ir >= family_best - 1e-9:
                        break
                    plateau += 1
                out = {
                    "enough_data": True,
                    "family_size": len(irs),
                    "family_best": round(family_best, 4),
                    "recent_best": round(recent_best, 4),
                    "family_best_before": round(family_best_before, 4),
                    "marginal": round(marginal, 4),
                    "plateau_trials": plateau,
                }
        # 尾部线（同窗口数学；spread_ir 单侧不取绝对值——准入方向为正）
        if len(sps) >= 5:
            Wt = max(5, min(10, len(sps) // 2))
            if len(sps) > Wt:
                out["tail_marginal"] = {
                    "enough_data": True,
                    "family_size": len(sps),
                    "family_best": round(max(sps), 4),
                    "recent_best": round(max(sps[-Wt:]), 4),
                    "family_best_before": round(max(sps[:-Wt]), 4),
                    "marginal": round(max(sps[-Wt:]) - max(sps[:-Wt]), 4),
                }
            else:
                out["tail_marginal"] = {"enough_data": False,
                                        "family_size": len(sps)}
        else:
            out["tail_marginal"] = {"enough_data": False,
                                    "family_size": len(sps)}
        return out

    def _family_convergence(self, engine_trail: list,
                            mining: dict) -> tuple[bool, str | None, dict]:
        """族内 IC×尾部双线收敛（2026-08-26 规划书；2026-08-27 双轨 AND）。

        动机：全局版的参考系是全试验 max，被历史峰值族主导——换到真实
        但更弱的新族时永远追不上前窗口旧族峰值 → 误判全局枯竭。
        族内版只与本族自己的前一窗口比，新方向重获机会。

        判定（用户钉死：族内滑窗对滑窗）：
        - 族 = 当前尾部链（_family_chain，与 arc/cluster 断链同一谓词）
        - 族内 (source_hash, horizon) 去重保序取最新（防跨 stage 重复
          灌窗口，与全局版口径一致）
        - 门槛：去重后 ≥ 2·Wf 条才判（小族不判，由 _family_marginal
          软信号兜底）；收敛 ⟺ max(|IC|[-Wf:]) − max(|IC|[-2Wf:-Wf]) < δf

        双轨 AND（2026-08-27 规划书 D2）：尾部线（族内带 tail 块条目的
        spread_ir，同键去重、同 ≥2·Wf 门槛、同滑窗口径，delta 独立键
        fam_conv_delta_tail）与 IC 线都平才 must_rotate。IC 平但尾部
        仍在改善 → 不触发（diag.ic_converged=True 供 escalation 改写）。
        尾部线条目 < 2·Wf 或 delta_tail ≤0 → 只有 IC 线参与判定（回到
        单轨行为；弱波动不得成为赖着不换向的口子）。

        停点性质（用户钉死：纯 must_rotate）——触发方换向，断链自动
        解除；不设静默终态（convergence stop_kind 退役）。

        参数从 mining_state 读 fam_conv_window/fam_conv_delta（默认在
        MINING_CONFIG；≤0 关闭）。返回 (fired, reason, diag)。"""
        try:
            w = int(mining.get("fam_conv_window", 0) or 0)
            delta = float(mining.get("fam_conv_delta", 0) or 0)
            _dt = mining.get("fam_conv_delta_tail")
            delta_tail = (float(_dt) if isinstance(_dt, (int, float))
                          and not isinstance(_dt, bool) else delta)
        except (TypeError, ValueError):
            return False, None, {"error": "参数类型非法"}
        if w <= 0 or delta <= 0:
            return False, None, {"disabled": True, "window": w, "delta": delta}
        fam = self._family_chain(engine_trail)
        irs_by_key: dict = {}
        sp_by_key: dict = {}
        order: list = []
        for e in fam:
            if not isinstance(e, dict):
                continue
            k = (e.get("source_hash"), e.get("horizon"))
            if k not in irs_by_key and k not in sp_by_key:
                order.append(k)
            ir = e.get("ic_ir")
            if isinstance(ir, (int, float)) and not isinstance(ir, bool):
                # 去重保序取最新（与 IC 单轨版同一语义）：键的窗口位置 =
                # 首次出现，值 = 最近一次（跨 stage 重评更新不新增）
                irs_by_key[k] = abs(float(ir))
            tail = e.get("tail") if isinstance(e.get("tail"), dict) else None
            sp = tail.get("spread_ir") if tail else None
            if (isinstance(sp, (int, float)) and not isinstance(sp, bool)
                    and math.isfinite(float(sp))):
                # spread_ir 单侧语义（准入 spread_ir ≥ bar）——不取绝对值
                sp_by_key[k] = float(sp)
        irs = [irs_by_key[k] for k in order if k in irs_by_key]
        sps = [sp_by_key[k] for k in order if k in sp_by_key]
        tail_enabled = delta_tail > 0
        tail_diag = {"family_size": len(sps), "window": w,
                     "delta": delta_tail,
                     "enough_data": tail_enabled and len(sps) >= 2 * w}
        diag = {"family_size": len(irs), "window": w, "delta": delta,
                "enough_data": len(irs) >= 2 * w,
                "tail": tail_diag}
        if len(irs) < 2 * w:
            return False, None, diag
        recent_best = max(irs[-w:])
        prev_best = max(irs[-2 * w:-w])
        diag.update({"recent_best": round(recent_best, 4),
                     "prev_best": round(prev_best, 4),
                     "ic_converged": recent_best - prev_best < delta})
        if not diag["ic_converged"]:
            return False, None, diag
        # IC 线平：尾部线在场且数据足够 → AND 判定；否则 IC 单轨（现行行为）
        if tail_diag["enough_data"]:
            t_recent = max(sps[-w:])
            t_prev = max(sps[-2 * w:-w])
            tail_diag.update({"recent_best": round(t_recent, 4),
                              "prev_best": round(t_prev, 4),
                              "converged": t_recent - t_prev < delta_tail})
            if not tail_diag["converged"]:
                # IC 收敛但尾部仍在改善（+δ）——不触发 must_rotate，
                # escalation 文案由 _loop_directive 按 diag 改写
                return False, None, diag
            reason = (f"族内 IC×尾部双线收敛（IC：本族最近 {w} 条最佳 |IC_IR| "
                      f"{recent_best:.3f} 未超前窗口最佳 {prev_best:.3f} 达 "
                      f"{delta}；spread：最近 {w} 条最佳 spread_ir "
                      f"{t_recent:.3f} 未超前窗口最佳 {t_prev:.3f} 达 "
                      f"{delta_tail}）")
            return True, reason, diag
        return True, (f"族内 IC 收敛（本族最近 {w} 条最佳 |IC_IR| "
                      f"{recent_best:.3f} 未超前窗口最佳 {prev_best:.3f} "
                      f"达 {delta}）"), diag

    def _loop_directive(self) -> dict:
        """停走指令：引擎对 agent 的唯一停走真相源（注入关键工具响应）。

        v7（2026-08-25 arc 化）：轮次上限从终身改方向段相对——
        - arc_rounds >= max_rounds（当前方向段叙事轮次；家族链断 → 归 0，
          与 cluster_trials 同一事件双归零）→ kind=direction_budget
        - cluster_trials >= max_cluster_trials（当前簇试验上限，断链重置）
          → kind=direction_budget
        direction_budget 停点不再是静默终态（50/50 触顶后注入器死机 =
        手动 push 病理复现）：引擎强制挂 rotate/literature 策略，state=
        must_rotate，注入器照常推进；换向断链自动重置预算回到 running。
        v8（2026-08-26 规划书）：全局 IC 收敛退役（全试验 max 被历史
        峰值族主导，新方向被误判枯竭）→ 族内收敛顶上（滑窗对滑窗，
        纯 must_rotate，用户钉死）——convergence stop_kind 消失，静默
        终态只剩 finalize（test 一次性锁）/ fail_streak（当前无喂入方）。
        候选池满 / accepted 因子不是停点；穷尽宣告一律拒收（写入拒/
        回显降级 pending_rejected）。
        计数口径（2026-08-31 方案 A lane 化）：round = 本线 arc_rounds
        方向段轮次；rounds_total = 本线 trail.json 叙事轮次（信息性）；
        cluster_trials = 本线 family_streak 同族链长；n_trials = trail_engine
        全局唯一 (source_hash, horizon) 试验数（**全局口径不动**——deflation
        计价必须全量计数，其它线的试验照常抬本线的 bar）。
        意图层（pending/frozen/族链/arc 预算）只看本线（lane 过滤）；
        事实层（plateau 穷尽信号/n_trials/tail 遥测/explored）保持全局。"""
        lane = current_lane()
        agent_trail_all = read_json_list("trail", self.state_root)
        engine_trail = self._read_engine_trail()
        agent_trail = own_entries(agent_trail_all, lane)
        engine_own = own_entries(engine_trail, lane)
        agent_rounds = len(agent_trail)
        n_trials_lane = len({(e.get("source_hash"), e.get("horizon"))
                             for e in engine_own if isinstance(e, dict)
                             and e.get("source_hash")})
        mining = read_mining_state(self.state_root)
        arc = lane_arc_rounds(mining, lane)
        n_trials = len({(e.get("source_hash"), e.get("horizon"))
                        for e in engine_trail if isinstance(e, dict)
                        and e.get("source_hash")})
        # 簇 = 本线尾部同族链（_entries_linked 双信号）；streak 同时是
        # 「本线当前簇试验数」，簇试验上限停点的计数基础
        streak = self._family_streak(engine_own)
        effective = {**mining, "arc_rounds": arc, "n_trials": n_trials,
                     "cluster_trials": streak}
        term = check_termination(effective)
        # v8（2026-08-26 规划书）：全局收敛退役，族内收敛顶上——纯
        # must_rotate 停点（用户钉死）：族收敛永远只是换向信号，不设
        # 静默终态；convergence stop_kind 消失，静默终态只剩
        # finalize/fail_streak（全局枯竭不再自动判定）
        fam_conv = self._family_convergence(engine_own, mining)
        # stop_kind：None=running；direction_budget=换向可解除（注入器
        # 推进，含族收敛）；finalize/fail_streak=静默终态（注入器让位用户）
        stop_kind = None
        stop_reason = None
        if term.get("stop"):
            stop_kind = term.get("kind") or "direction_budget"
            stop_reason = term["reason"]
        elif fam_conv[0]:
            stop_kind = "direction_budget"
            stop_reason = fam_conv[1] + "——必须换向（断链后判定自动重置）"
        may_stop = stop_kind in ("finalize", "fail_streak")
        state = ("running" if stop_kind is None
                 else "must_rotate" if stop_kind == "direction_budget"
                 else "may_stop")
        # 上一轮自己声明的下一步（引擎原样回显——执行或证伪，不得静默放弃）；
        # 停笔宣言不接受回显，降级为 pending_rejected 并点名拒收。
        # 灵感重置声明（2026-09-01 WS-B/D1）判定序最先：合法声明走配额
        # 受理（受理后 pending 作废——D4 冷启动语义），超限降级
        # pending_rejected；台账异常降级为普通 pending（声明本身没错，
        # 不当拒收处理）
        pending = None
        pending_rejected = None
        inspiration_reset = None
        ir_quota = None
        if agent_trail and isinstance(agent_trail[-1], dict):
            nh = agent_trail[-1].get("next_hypothesis")
            if isinstance(nh, str) and nh.strip():
                decl = _inspiration_decl_match(nh)
                if decl is not None:
                    mode, reason = decl
                    try:
                        ir_quota = inspiration_declare(
                            self.state_root, lane, agent_rounds, mode)
                    except Exception:
                        ir_quota = None
                    if ir_quota is not None and ir_quota.get("accepted"):
                        inspiration_reset = {"mode": mode, "reason": reason}
                    elif ir_quota is not None:
                        pending_rejected = {
                            "text": nh.strip()[:400],
                            "reason": ir_quota.get("reason") or "灵感重置声明被拒"}
                    else:
                        pending = nh.strip()[:400]
                else:
                    hit = _surrender_match(nh)
                    if hit:
                        pending_rejected = {"text": nh.strip()[:400], "reason": hit}
                    else:
                        pending = nh.strip()[:400]
        # 家族饱和：边际收益驱动（2026-08-25 用户决策：不看链长绝对
        # 阈值，只看族内边际）——仍在改善的族不催（哪怕 100 个试验），
        # 已平台的族催（哪怕只有 5 个）。族 = 本线族（lane 化）
        fam_marg = self._family_marginal(engine_own)
        escalation = None
        # 双轨收敛分流文案（2026-08-27 D2）：IC 线收敛但尾部线仍在改善 →
        # 不触发 must_rotate，escalation 把「为什么没换向」说清楚——
        # 继续尾部维度或换向由策略分流
        fc_diag = fam_conv[2] if isinstance(fam_conv[2], dict) else {}
        fc_tail = fc_diag.get("tail") if isinstance(fc_diag.get("tail"), dict) else {}
        if (fc_diag.get("ic_converged") and fc_tail.get("enough_data")
                and fc_tail.get("converged") is False):
            _t_imp = float(fc_tail["recent_best"]) - float(fc_tail["prev_best"])
            escalation = (
                f"IC 线收敛但尾部线仍在改善（spread_ir 最近窗最佳 "
                f"{float(fc_tail['recent_best']):.3f} vs 前窗 "
                f"{float(fc_tail['prev_best']):.3f}，+{_t_imp:.3f}）——"
                "继续尾部维度或换向由策略分流")
        elif fam_marg.get("enough_data"):
            m = fam_marg["marginal"]
            fb = fam_marg["family_best"]
            pt = fam_marg["plateau_trials"]
            tm_info = fam_marg.get("tail_marginal") or {}
            tm = tm_info.get("marginal") if tm_info.get("enough_data") else None
            tail_ok = isinstance(tm, (int, float))
            tail_txt = (f"；尾部线边际 {tm:+.3f}（最近窗 spread_ir 最佳 "
                        f"{tm_info['recent_best']:.3f} vs 族前史 "
                        f"{tm_info['family_best_before']:.3f}）") if tail_ok else ""
            if m < -0.10:
                if tail_ok and tm >= -0.10:
                    escalation = (
                        f"IC 线边际严重枯竭（{m:+.3f}）但尾部线边际 {tm:+.3f} "
                        "未同枯竭——单线枯竭不强制文献注入，"
                        "继续尾部维度或换向由策略分流")
                else:
                    escalation = (
                        f"族内边际严重枯竭——最近 {fam_marg['family_size']} 个同族试验"
                        f"的最佳 |IC_IR| {fam_marg['recent_best']:.3f} 落后族历史"
                        f"最佳 {fam_marg['family_best_before']:.3f} 达 {abs(m):.3f}"
                        f"（{pt} 个试验未刷新族最佳 {fb:.3f}）{tail_txt}。"
                        "必须 factor_arxiv_search 引入文献级假设后构造新因子族。"
                        "继续本族 = 浪费试验预算。")
            elif m < -0.05:
                if tail_ok and tm >= -0.05:
                    escalation = (
                        f"IC 线边际枯竭（{m:+.3f}）但尾部线边际 {tm:+.3f} 未同"
                        "枯竭——不强制换向，优先构造尾部维度变体（top-K 组差"
                        "方向，见 loop.tail 遥测）")
                else:
                    escalation = (
                        f"族内边际枯竭——最近窗口最佳 {fam_marg['recent_best']:.3f}"
                        f"落后族历史 {fam_marg['family_best_before']:.3f} 达 "
                        f"{abs(m):.3f}（{pt} 个试验未刷新）{tail_txt}。"
                        "换信息源维度（新数据列/新算子族/新信号形式）。"
                        "族内参数微调不再产生新信息。")
            elif m < 0:
                escalation = (
                    f"族内边际递减——最近窗口最佳 {fam_marg['recent_best']:.3f}"
                    f"未超越族历史 {fam_marg['family_best_before']:.3f}"
                    f"（边际 {m:+.3f}，{pt} 个试验未刷新）{tail_txt}。"
                    "考虑换构造思路而非继续参数微调。")
            elif tail_ok and tm < -0.05:
                escalation = (
                    f"尾部线边际枯竭（{tm:+.3f}，最近窗 spread_ir 最佳 "
                    f"{tm_info['recent_best']:.3f} vs 族前史 "
                    f"{tm_info['family_best_before']:.3f}）而 IC 线仍在改善"
                    f"（{m:+.3f}）——尾部维度已饱和，继续 IC 维度或换向，"
                    "策略不强制")
        obligation = None
        if state == "running":
            obligation = ("继续内循环——禁止停下来等用户指示。停点仅由引擎机械"
                          "判据触发（方向段轮次/当前簇试验上限、IC_IR 改善收敛、"
                          "finalize），穷尽宣告一律拒收")
            if pending_rejected is not None:
                obligation += (f"。上一轮 next_hypothesis 是停笔宣言（「"
                               f"{pending_rejected['text'][:80]}」——"
                               f"{pending_rejected['reason']}）：给出可执行的"
                               "新假设（构造什么、测什么）并继续")
            elif pending:
                obligation += (f"。上一轮声明的 next_hypothesis 尚未消化："
                               f"「{pending[:120]}」——执行它，或用 "
                               "factor_record_explored 明确证伪，不得静默放弃")
        elif state == "must_rotate":
            obligation = ((stop_reason or "方向段预算耗尽")
                          + "。执行策略指令换向：构造与当前族不同源的新因子"
                            "（断链后预算自动重置，回到 running）。同族参数微调"
                            "不会重置预算，禁止原地续推")
            # 弧末蒸馏义务（构造面 v7）：换向=弧边界，正是蒸馏节拍——
            # 引擎先自动快照旧 live 入 raw/（防洗账），终条目归档+墓志
            try:
                from .journal import _lane_dir, resolve_journal_lane
                if _lane_dir(self.state_root, resolve_journal_lane()).exists():
                    obligation += ("。换向前先 factor_journal_distill 蒸馏本弧"
                                   "（旧 live 自动快照入 raw/，终条目自动归档）")
            except Exception:
                pass
        else:
            obligation = ((stop_reason or "停点触发")
                          + "。此停点换方向无法解除，注入器已静默——数据集轮换"
                            "是用户操作，向用户上报当前进展即可，不要停下来空等")
        if inspiration_reset is not None:
            obligation += ("。灵感重置已确认——按策略指令换源重启，上一方向的"
                           " pending 已作废，不得回旧假设族续推")
        # 策略指令（v4 2026-08-24）：下一轮方向类型的机械选择——注入器
        # （TS drive 层）在 turn 结束后读它，以 user 角色注入 [引擎推进]。
        # v7：must_rotate 状态照常注入（direction_budget 强制 rotate/
        # literature）；convergence/finalize 返回 None（注入器静默）。
        irs = [abs(e["ic_ir"]) for e in engine_trail if isinstance(e, dict)
               and isinstance(e.get("ic_ir"), (int, float))
               and not isinstance(e.get("ic_ir"), bool)]
        plateau = (len(irs) > 40 and max(irs[-20:]) < max(irs[:-20]) + 0.05)
        frozen = False
        if len(agent_trail) >= 2:
            a, b = agent_trail[-2], agent_trail[-1]
            if (isinstance(a, dict) and isinstance(b, dict)
                    and isinstance(a.get("next_hypothesis"), str)
                    and a.get("next_hypothesis").strip()
                    and a.get("next_hypothesis") == b.get("next_hypothesis")):
                frozen = True
        pass_n = sum(1 for e in engine_own if isinstance(e, dict)
                     and e.get("verdict") == "pass")
        try:
            accepted_n = sum(1 for e in read_registry(self.state_root)
                             if isinstance(e, dict) and e.get("accepted"))
        except Exception:
            accepted_n = 0
        lit_search_count = int(self._read_papers().get("searches", 0) or 0)
        # random 配额快照读（WS-A/D3）：声明强制路径不查（受理时已查），
        # 只供 R1 轮转决策；发射记账在策略挂出后（random_emit_count）
        rnd_used = lane_random_used(mining, lane)
        rnd_quota = int(mining.get("random_strategy_quota",
                                   MINING_CONFIG["random_strategy_quota"]))
        strategy = _pick_strategy(
            stop_kind=stop_kind, streak=streak,
            pending_rejected=pending_rejected, frozen=frozen,
            plateau=plateau, pass_unadmitted=max(pass_n - accepted_n, 0),
            accepted_n=accepted_n, agent_rounds=agent_rounds,
            n_trials=n_trials, lit_search_count=lit_search_count,
            family_marginal=fam_marg,
            inspiration_reset=inspiration_reset,
            random_available=rnd_used < rnd_quota)
        if strategy is not None and strategy.get("type") == "random":
            try:
                random_emit_count(self.state_root, lane, agent_rounds)
                rnd_used = lane_random_used(
                    read_mining_state(self.state_root), lane)
            except Exception:
                pass
        # WS-B 回执（agent 可见配额，与 pending_rejected 同一可见性原则）：
        # 受理回合用台账返回值（快照 mining 未含本次扣额），无声明时用快照
        _bucket = (mining.get("lanes") or {}).get(lane) or {}
        _i_quota = int(mining.get("inspiration_quota",
                                  MINING_CONFIG["inspiration_quota"]))
        if ir_quota is not None and isinstance(ir_quota.get("quota_left"), int):
            _q_left = ir_quota["quota_left"]
        else:
            _q_left = max(0, _i_quota - int(_bucket.get("inspiration_used", 0) or 0))
        loop = {
            "state": state,
            "stop_kind": stop_kind,
            "lane": lane,
            "round": arc,
            "max_rounds": int(effective.get("max_rounds",
                                            MINING_CONFIG["max_rounds"])),
            "rounds_total": agent_rounds,
            "n_trials": n_trials,
            "n_trials_lane": n_trials_lane,
            "cluster_trials": streak,
            "max_cluster_trials": int(effective.get(
                "max_cluster_trials", MINING_CONFIG["max_cluster_trials"])),
            "family_streak": streak,
            "stop_reason": stop_reason,
            "family_convergence": fam_conv[2],
            "tail": self._tail_telemetry(engine_trail),
            "deepen": self._deepen_telemetry(engine_trail),
            "pending_hypothesis": pending,
            "pending_rejected": pending_rejected,
            "inspiration_reset": {
                "acknowledged": inspiration_reset is not None,
                "mode": (inspiration_reset or {}).get("mode"),
                "quota_left": _q_left,
                "random_available": rnd_used < rnd_quota,
                "syntax": ("声明 = next_hypothesis 以 [灵感重置:literature] 或 "
                           "[灵感重置:random] 开头 + 一句理由（声明即切；每方向段 "
                           "≤2 次，random ≤1 次；断链换向自动重置配额）"),
            },
            "obligation": obligation,
            "escalation": escalation,
            "strategy": strategy,
        }
        # 推理日志指针（构造面 v7）：一行遥测——存在性/活条目计数/登记率/
        # 预算告警。全文按需 factor_journal_read（渐进披露:会话开场 level=1）
        try:
            from .journal import journal_pointer
            loop["journal"] = journal_pointer(self.state_root)
        except Exception:
            pass
        # 并行归属呈现（方案 A）：bar 跳变必须有出处——双线并行时 agent
        # 无法区分「我的试验推高了 bar」还是「别的线推高了」，曾把外线
        # 写入脑补成自己超时调用的完成。归属说清，观测不再污染归因
        foreign = n_trials - n_trials_lane
        if foreign > 0:
            loop["lane_note"] = (
                f"并行线模式：本线 {n_trials_lane} / 全局 {n_trials} 条试验"
                f"（其它线 {foreign} 条）。n_trials 与 tail bar 按全局计价"
                "——其它线的试验照常抬高本线门槛（多重检验全量计数）；"
                "pending/族链/方向预算只统计本线")
        # W3（2026-08-26 规划书）：最近 30 分钟 infra 失败（-32005）≥1 → 附加
        # 纠偏引导——超时后 agent 的下一个成功调用（status/evaluate/
        # trail_summary）即收到。纯附加遥测：不影响上面任何停走判定，
        # 也不参与策略选择
        infra = self._recent_infra_failures()
        if infra:
            loop["infra_failures"] = {
                "recent_count": len(infra),
                "last_method": infra[-1].get("method"),
                "note": ("worker 超时是基础设施事件：向量化实现，勿因超时更换研究方向；"
                         "失败的调用零写入。重试前先 factor_status 看 workerRuns——"
                         "evaluate 已默认异步（job_id 轮询），同参数重试会自动并入"
                         "在途作业（去重），不会重复计算。严禁杀 bridge/python 进程"
                         "「解卡」——bridge 单线程忙是设计内行为，busy≠stuck；"
                         "杀掉后所有会话的 factor_* 全部挂起直至重启宿主"
                         "（2026-08-31 实证）。等待或轮询，永远不动进程。"),
            }
        return loop

    def _async_evaluate_enabled(self, params: dict) -> bool:
        """层 0c（2026-08-31 计算利用 PLAN）：evaluate 默认异步——大面板单评
        分钟级，同步路径占死单线程 bridge，两会话互相卡死（2026-08-31 实证）。
        逃生阀：显式 async:false（测试与内部同步路径用）；
        in_process 模式（受信调试）恒同步——worker 子进程语义才是异步靶子。"""
        if self.execution_mode != "worker":
            return False
        return params.get("async") is not False

    def _factor_evaluate(self, params):
        # 层 0c：job_id 取结果——纯取回（放在一切校验前，与 composite 同口）
        if params.get("job_id"):
            return self._async_result(str(params["job_id"]))
        # 垃圾提交防线（2026-08-31 生产实证）：轮询方若丢了 job_id（如工具
        # 层曾未转发），请求会退化成空 source 的新作业——每轮询一次制造
        # 一个注定失败的垃圾作业。空 source 直接拒绝并教正确轮询方式。
        if not str(params.get("source") or "").strip():
            raise BridgeError(
                -32602,
                "factor.evaluate 需要 source（或传 job_id 轮询在途作业，"
                "收到两者皆空）。轮询方法：{\"job_id\": \"job-...\"}——"
                "evaluate/composite/batch/audit 的作业均可这样取回。")
        env_id = params.get("envId", "primary")
        # test 纪律锁前置检查（2026-08-18 独立审计 F-TP-01 澄清后的 UX 修正）：
        # 绕过链本身不成立（reset scope=all 后重配+load，evaluate_test 内部
        # 仍会拒——lock 读 stateRoot 文件与 env 无关）；但 reset 后 env 丢失
        # 会让错误先以 -32001（数据缺失）报出，制造「锁被绕过」的误读。
        # 前置检查让纪律锁的错误永远最先、最准确。
        if params.get("stage", "development") == "test":
            lock_path = Path(self.state_root) / "test_lock.json"
            if lock_path.exists():
                try:
                    _tl = json.loads(lock_path.read_text(encoding="utf-8"))
                    if _tl.get("consumed", False):
                        raise BridgeError(
                            -32003,
                            "test 已被消费（test_lock.json）。test 是最终消耗品，禁止反复评估调参。"
                            "锁不在任何 reset scope——只能手动删除（意味着声明放弃本 stateRoot 的 test 纪律）。")
                    # 并行 P0 claim 态：另一进程正在评估 test（claim-then-compute）
                    _cl = _tl.get("claim") or {}
                    _cpid = _cl.get("pid")
                    if _cpid and pid_alive(int(_cpid)) and int(_cpid) != os.getpid():
                        raise BridgeError(
                            -32003,
                            f"test 正在被另一进程评估（PID={_cpid}，始于 {_cl.get('ts', '?')}）——"
                            "等它完成后再试；确认其已崩溃可手动删除 test_lock.json 的 claim 后重试。")
                except BridgeError:
                    raise
                except Exception:
                    pass  # 锁文件损坏：放行到 evaluate_test 内部路径统一处理
        env = self._require_panel_env(env_id)
        source = params.get("source", "")
        stage = params.get("stage", "development")
        # 因果门：同步路径就地执行；异步路径（层 0c）随作业线程执行
        # （causality_gate 元数据标记）——提交侧零 worker 调用
        if not self._async_evaluate_enabled(params):
            self._enforce_causality(source, env, env_id)
        source_hash = source_fingerprint(source)
        if stage != "development":
            params = {**params, "source_hash": source_hash}
            if stage == "test":
                # test 准入前置（2026-08-28 加固）：test 是 stateRoot 级一次性
                # 资源（全局锁挡**重复**消费），还须挡**浪费**消费——2026-08-24
                # 生产取证：唯一一发 test 烧给了从未提交的候选（registry 查无
                # 此 hash），已准入因子从此无 test 测量。test 只测已入册候选。
                if not any(isinstance(r, dict)
                           and r.get("source_hash") == source_hash
                           and bool(r.get("accepted"))
                           for r in read_registry(self.state_root)):
                    raise BridgeError(
                        -32003,
                        f"test 只测已入册候选：source hash {source_hash[:12]} "
                        "不在 registry 或未 accepted——先 factor_registry_submit"
                        "通过准入再测。test 是 stateRoot 级一次性资源"
                        "（2026-08-24 教训：唯一一发烧给了从未提交的候选），"
                        "未入册的候选不得消耗它")
            # 层 0c：selection/test 计算异步化（默认；async:false 逃生阀）——
            # 纪律检查（锁/准入）全部在提交侧同步完成（R4），claim-then-compute
            # 的 claim 在计算路径内（evaluate_test），语义不变
            if self._async_evaluate_enabled(params):
                return self._async_submit(
                    "factor.evaluate", source, params, env, env_id,
                    extra_meta={"stage": stage, "causality_gate": True})
            result = self._run_factor("factor.evaluate", source, params, env)
            return self._wrap_diagnosis(env_id, source, stage, result)

        # S1 拒绝即信息门（2026-09-04 探索相似性防线）：仅 development
        # 新提案路径；提交侧同步执行、零 trial；scan/profile 只判一次。
        self._enforce_novelty_gate(source, env_id)

        # ---- v2（2026-08-20）horizon 申报制（仅 development）----
        # 菜单 = env calibration.horizons（未配置 = [主 horizon]，行为同旧版）。
        # 申报语义：horizon 是假设的一部分（20 日赌注 ≠ 5 日赌注）——agent 挑
        # horizon 的自由度被计账：trail 按 (hash, horizon) 分试验、N_eff 谱方法
        # 用 null 校准实测的跨 horizon 相关折叠族结构、pool_std 按 horizon
        # 取基线。scan = 一次申报全菜单（刻画因子特性是一等公民，族内计账）。
        menu = list(getattr(env.calibration, "horizon_menu", None)
                    or [env.calibration.horizon])
        h_req = params.get("horizon")
        if isinstance(h_req, str) and h_req.strip().lower() == "scan":
            if len(menu) <= 1:
                h_eff = env.calibration.horizon  # 单点菜单：scan 退化为单评估
            else:
                profile = {}
                for h in menu:
                    # scan 内层强制同步（层 0c）：profile 的每个成员必须是
                    # 完整诊断而非 job 句柄；单次 scan 本身是罕见重操作
                    profile[str(h)] = self._evaluate_dev(
                        env_id, env, source, source_hash,
                        {**params, "horizon": int(h), "async": False})
                return {
                    "scan": True, "horizons": menu, "profile": profile,
                    "note": ("horizon profile：菜单内每个 horizon 独立完整诊断"
                             "（deflated_train 已按当前 trail N_eff 重算）。"
                             "submit 时取其一（挑 horizon 的选择偏差已被族计账覆盖）。"),
                }
        elif h_req in (None, ""):
            h_eff = env.calibration.horizon
        else:
            try:
                h_eff = int(h_req)
            except (TypeError, ValueError):
                raise BridgeError(-32602,
                                  f"horizon 需为菜单内整数或 'scan'（收到 {h_req!r}）")
            if h_eff not in menu:
                raise BridgeError(
                    -32602,
                    f"horizon={h_eff} 不在环境菜单 {menu} 内。换 horizon = 新经济赌注："
                    "在 config calibration.horizons 菜单内申报（须含主 horizon；"
                    "配置菜单会改环境指纹，null-calibration 需重跑以建 per-horizon 基线）")
        return self._evaluate_dev(env_id, env, source, source_hash,
                                  {**params, "horizon": h_eff})

    def _evaluate_dev(self, env_id: str, env, source: str, source_hash: str,
                      params: dict):
        """development 单 horizon 评估路径（v2 抽取）：谱统计注入 → worker → 包装。"""
        h = params.get("horizon")
        # bar_sigma/pool_std（DSR 多重检验折减）：引擎侧从 trail_engine 硬统计，
        # 不依赖 agent 自觉传 batch 或维护任何计数器（2026-08-18 修正）。
        # v2：pending (hash, horizon) + 跨 horizon 先验 + per-horizon pool_std。
        # v3：门参数 bar_sigma = E[max|X|]（σ 单位）；n_trials 降级为遥测。
        stats, trail_std = self._trial_stats(source_hash, h, env_id)
        pool_std, _detector = self._resolve_pool_std(env_id, trail_std, h)
        params = {**params, "bar_sigma": stats["bar_sigma"],
                  "n_trials": stats["n_trials"], "pool_std": pool_std,
                  "source_hash": source_hash}
        # C1 缓存预检（修正案 A）：development 纯读——命中直接回，零子
        # 进程/零 env.npz 序列化（个股面板每次省几十秒）；_wrap_diagnosis
        # 照跑（池/trail/指纹语义不变）。写回由 worker 侧完成（同一键：
        # env_fp 注入 params，两层共用 eval_key_from_parts 归一）。
        env_fp = self._env_full_fingerprint(env_id)
        if env_fp:
            params = {**params, "env_fp": env_fp}
            ck = eval_key_from_parts(source_hash, env_fp, env, horizon=h,
                                     n_trials=stats["n_trials"],
                                     pool_std=pool_std,
                                     bar_sigma=stats["bar_sigma"])
            hit = cache_read(self.state_root, ck)
            if hit is not None:
                return self._wrap_diagnosis(env_id, source, "development",
                                            {**hit, "eval_cache": "hit"})
        # 层 0c：缓存未命中 → 异步作业（默认；R2 契约分叉——缓存命中同步
        # 直接返回 diagnosis，未命中返回 job_id 由 agent 轮询；响应自描述）
        if self._async_evaluate_enabled(params):
            return self._async_submit(
                "factor.evaluate", source, params, env, env_id,
                extra_meta={"stage": "development", "causality_gate": True})
        result = self._run_factor("factor.evaluate", source, params, env)
        return self._wrap_diagnosis(env_id, source, "development", result)

    def _factor_evaluate_composite(self, params):
        # C2：job_id 取结果——纯取回，不需要 env/source（放在一切校验前）
        if params.get("job_id"):
            return self._async_result(str(params["job_id"]))
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        source = params.get("source", "")
        # 因果门（2026-08-31 计算利用 PLAN 对齐）：同步路径就地；async 分支
        # 随作业线程（提交侧零 worker 调用，秒回）
        if not params.get("async"):
            self._enforce_causality(source, env, env_id)
        if params.get("ingredients") is not None:
            params = {**params, "ingredients": _as_dict(params["ingredients"], "ingredients")}
        # C1：source_hash/env_fp 注入——worker 侧成分缓存与桥同一键空间
        env_fp = self._env_full_fingerprint(env_id)
        params = {**params, "source_hash": source_fingerprint(source),
                  **({"env_fp": env_fp} if env_fp else {})}
        if params.get("async"):
            return self._async_submit("factor.evaluate_composite", source,
                                      params, env, env_id,
                                      extra_meta={"causality_gate": True})
        result = self._run_factor("factor.evaluate_composite", source, params, env)
        return self._wrap_diagnosis(env_id, source, "composite", result)

    def _factor_evaluate_batch(self, params):
        # C2/R24：job_id 纯取回放在一切校验前（重启后面板未加载也能收尸；
        # 与 evaluate/composite/audit 同口——此前放在 _require_panel_env 后）
        if params.get("job_id"):
            return self._async_result(str(params["job_id"]))
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        sources = params.get("sources") or {}
        if params.get("sources") is not None:
            sources = _as_dict(params["sources"], "sources")
            params = {**params, "sources": sources}
        # S1 拒绝即信息门（2026-09-04 探索相似性防线）：批量成员逐个过门
        # （提交侧同步、零 trial；拒绝带成员名——F5 惯例；async 路径同样
        # 在提交侧拦截，不进作业线程。审计判例：27 分钟双胞胎经批量通道
        # 提交、双双 pass 烧账本——stage=batch 不代表非手写，门必须覆盖）。
        for _name, _src in sources.items():
            self._enforce_novelty_gate(str(_src), env_id, member=str(_name))
        # v2 申报制：整批同一声明 horizon（须 ∈ 菜单；scan 不适用于 batch——
        # 菜单×批次 = 维度爆炸，scan 走 factor.evaluate 单因子路径）。
        # R24（2026-08-31 隔离复审）：校验与 pool_std 注入提到 async 分支
        # 之前——此前 async 在此 return：菜单外 horizon 静默回落主口径、
        # deflated p 全员 None（sync/async 同请求不同统计）
        menu = list(getattr(env.calibration, "horizon_menu", None)
                    or [env.calibration.horizon])
        h_req = params.get("horizon")
        if isinstance(h_req, str) and h_req.strip().lower() == "scan":
            raise BridgeError(-32602,
                              "batch 不支持 scan——horizon profile 走 factor.evaluate"
                              "（一次 scan = 菜单全诊断，族内计账）")
        if h_req in (None, ""):
            h_b = env.calibration.horizon
        else:
            try:
                h_b = int(h_req)
            except (TypeError, ValueError):
                raise BridgeError(-32602,
                                  f"horizon 需为菜单内整数（收到 {h_req!r}）")
            if h_b not in menu:
                raise BridgeError(
                    -32602,
                    f"horizon={h_b} 不在环境菜单 {menu} 内。换 horizon = 新经济赌注："
                    "在 config calibration.horizons 菜单内申报")
        # v3：批内族口径由 evaluate_batch 内部 _LuckSampler 直算
        # （bar_sigma_b），pool_std 由 bridge 按 per-horizon 基线注入——
        # v2 的 n_trials=len(sources) 幂校正已退役（同族 |ρ|≈0.9 的
        # 参数扫描在幂校正下仅折减 ~1.05 倍，E[max|X|] 直算如实计价）。
        _, trail_std_pre = self._trial_stats(None, h_b, env_id)
        pool_std_pre, _det_pre = self._resolve_pool_std(env_id, trail_std_pre, h_b)
        params = {**params, "horizon": h_b, "pool_std": pool_std_pre}
        # R24：async 分支——因果门随作业线程逐成员执行（causality_sources；
        # 此前 async 完全绕过因果门），A1 计账/重算由轮询侧后处理完成
        if params.get("async"):
            return self._async_submit(
                "factor.evaluate_batch", "", params, env, env_id,
                extra_meta={"causality_gate": True,
                            "causality_sources": [str(s) for s in sources.values()]})
        # 同步路径：批次内每个因子过因果（缓存命中时零成本）；拒绝带成员名（F5）
        for name, src in sources.items():
            self._enforce_causality_member(str(src), env, env_id, sources)
        result = self._run_factor("factor.evaluate_batch", params.get("source", ""), params, env)
        return self._postprocess_batch_result(env_id, h_b, sources, result)

    def _postprocess_batch_result(self, env_id, h_b, sources, result):
        """batch 结果后处理（sync 路径与异步轮询共用，R24）：
        成员 _meta/receipt/构造指纹 + A1 逐成员 trail 入账 + deflated
        就地重算 + 族选择成本 + loop 指令。"""
        # 批次结果逐因子附指纹/对表（轻量：只附 _meta 不重复对表）
        if isinstance(result, dict) and isinstance(result.get("factors"), dict):
            fp = self._env_full_fingerprint(env_id)
            trail_items = []
            for name, diag in result["factors"].items():
                if not isinstance(diag, dict) or diag.get("error"):
                    continue
                src = str(sources.get(name, ""))
                diag["_meta"] = {"fingerprint": fp, "engine_version": __version__,
                                 "receipt": self._make_receipt(diag)}
                diag["_receipt"] = diag["_meta"]["receipt"]
                # v5 血缘：批成员构造指纹——按成员自己的源码算（R24 复审
                # 修正：此前误用上一迭代的陈旧 src，全批共享末成员指纹）
                diag["_construction_fp"] = (_construction_fingerprint(src)
                                            if src else None)
                # A1（2026-08-19 堵 batch 绕过）：批次成员与单因子 evaluate 同等
                # 写入 trail_engine——N_eff 计数不因走 batch 通道而漏计。此前
                # batch 成员不写 trail → N 恒不涨 → deflated p 按 N=1 给出，
                # 拿 batch 诊断直接 submit 即绕过 D7 门控（实验证实）。
                # v2：entry 带 horizon（试验单元 (hash, horizon)）。
                if src:
                    # 源码外挂库：与单因子通道同款（error 成员不记账也不入库）
                    try:
                        store_factor_source(src, self.state_root)
                    except Exception:
                        pass
                    trail_items.append((source_fingerprint(src), "batch", diag, None))
            if trail_items:
                # 写锁（并行 P0）：batch 通道与单因子同锁——读改写全程互斥
                with state_write_lock(self.state_root):
                    self._append_engine_trail_batch(env_id, trail_items)
            # A2：sketch 已存 trail；agent 可见 schema 保持不变（不留 ic_series_train）
            # 就地重算（2026-08-20 会话轨迹审计修复）：evaluate() 对每个成员
            # 单独算 deflated 时是单检验口径（batch 视图里的 p 系统性偏乐观，
            # Agent 挑深挖对象时被误导，直到 submit 才被 A3 纠正）。
            # 现在成员入 trail 后立即用当前 trail 的选择运气（含本批）重算——
            # 与 submit 重算同一公式（_dsr_p_from_stats），决策支持一致。
            # v2：per-horizon 口径（trail_std/pool_std/先验全按声明 horizon）。
            # v3：门参数 bar_sigma = E[max|X|]（本批全体成员已入 trail，
            # 一致统计一次即可；n_trials/n_eff 降级为遥测）。
            stats_b, trail_std_b = self._trial_stats(None, h_b, env_id)
            pool_std_b, detector_b = self._resolve_pool_std(env_id, trail_std_b, h_b)
            bar_b = stats_b["bar_sigma"]
            for name, diag in result["factors"].items():
                if not isinstance(diag, dict) or diag.get("error"):
                    continue
                diag.pop("ic_series_train", None)
                dp = diag.get("deflated_train")
                if isinstance(dp, dict) and dp.get("sr_hat") is not None:
                    p_new = _dsr_p_from_stats(dp.get("sr_hat"), dp.get("skew"),
                                              dp.get("kurt"), dp.get("n_obs"),
                                              bar_b, pool_std_b)
                    diag["deflated_train"] = {**dp, "p": p_new,
                                              "n_trials": float(stats_b["n_trials"]),
                                              "n_eff": float(stats_b["n_eff"]),
                                              "bar_sigma": float(bar_b),
                                              "pool_std": pool_std_b,
                                              "nu": stats_b.get("nu"),
                                              "signal_detector": detector_b,
                                              "recomputed_at_batch": True}
            # 族选择成本：K 选 1 的选择事件定价（诚实账目，agent 可见）
            if isinstance(result.get("batch"), dict):
                try:
                    M_now = len(sources) or 1
                    # 批内族价（batch 内部 _LuckSampler）与入 trail 后全局 bar 的
                    # 差 = 本批「K 选 1 + 与历史族的跨族选择」的增量价格
                    bar_in_batch = result["batch"].get("bar_sigma")
                    if isinstance(bar_in_batch, (int, float)):
                        sel_delta = float(bar_b) - float(bar_in_batch)
                        note = (f"本批 {M_now} 个成员的选择（族内扫描+跨族）"
                                f"已计入全局 bar：+{sel_delta:.2f}σ")
                        if isinstance(pool_std_b, (int, float)):
                            note += f" ≈ +{sel_delta * pool_std_b:.3f} IC_IR 门槛"
                        result["batch"]["selection_cost"] = {
                            "batch_family_bar": round(float(bar_in_batch), 4),
                            "global_bar_after": round(float(bar_b), 4),
                            "delta_sigma": round(sel_delta, 4),
                            "delta_ic_ir": (round(sel_delta * pool_std_b, 4)
                                            if isinstance(pool_std_b, (int, float)) else None),
                            "note": note}
                except Exception:
                    pass
        # 自主性停走指令：batch 响应必带（batch 参数扫描是家族饱和的主要
        # 来源——loop.escalation 在此触发分级升级）
        try:
            result["loop"] = self._loop_directive()
        except Exception:
            pass
        return result

    def _factor_noise_test(self, params):
        """噪声硬门（2026-08-24 用户设计决策）：因子在 M 个随机噪声
        世界上的直接表现。真 alpha 按构造不可预测噪声——噪声上仍显著
        （|z|≥3，z=跨世界 IC_IR 均值/SE）= 因子公式在拟合评价
        artifact，无条件拒收（submit 硬门同源调用本方法）。

        种子默认从环境指纹派生（同环境可复现），显式 seed 覆盖。"""
        import hashlib

        env = self._require_panel_env(params.get("envId", "primary"))
        source = params.get("source")
        if not source:
            raise BridgeError(-32602, "source 必填（def factor(env) 源码）")
        m = max(10, min(int(params.get("m", 100) or 100), 300))
        seed = params.get("seed")
        if seed is None:
            fp = self._env_full_fingerprint(params.get("envId", "primary")) or "nofp"
            seed = int(hashlib.sha256(str(fp).encode("utf-8")).hexdigest()[:8], 16)
        else:
            seed = int(seed)
        result = self._run_factor("factor.noise_test", str(source),
                                  {"m": m, "base_seed": seed}, env)
        if not isinstance(result, dict):
            raise BridgeError(-32003, f"噪声测试返回异常: {type(result).__name__}")
        result["seed"] = seed
        result["gate"] = ("artifact 拒收（噪声世界 |z|≥3：因子在拟合评价"
                          "管道 artifact 而非数据结构）"
                          if result.get("artifact") else
                          ("无法判定（有效世界不足）" if result.get("artifact") is None
                           else "通过（噪声世界无系统性 IC——真信号只可能来自真实数据结构）"))
        return result

    def _flatness_decl_check(self, source: str, decl) -> None:
        """平坦性申报表校验（形状 + value 真实出现在 source）。

        漏报/错报 = 事务中止拒收（-32602）：申报值不在 source 数值字面量
        中，说明参数表与代码不一致——防「申报无关参数、隐藏真正被调的
        关键参数」绕过平坦性检查。"""
        from .factor.flatness import literal_present

        if not isinstance(decl, list) or not decl:
            raise BridgeError(
                -32602,
                "flatness_params 必须是非空 [{name, value, step}] 列表"
                "（name: 参数名；value: 数值，须出现在 source 字面量中；"
                "step: 最小有意义步长，如窗口 10→1、权重 0.65→0.05）。"
                "无参数因子直接省略该字段")
        if len(decl) > 4:
            raise BridgeError(
                -32602,
                f"flatness_params 最多 4 个参数（收到 {len(decl)}）——"
                "邻域评估成本随参数线性增长（每参数 2 次变体评估）")
        seen = set()
        for d in decl:
            if not isinstance(d, dict):
                raise BridgeError(-32602, f"参数表条目必须是对象: {d}")
            name = str(d.get("name") or "").strip()
            val, step = d.get("value"), d.get("step")
            if not name or name in seen:
                raise BridgeError(-32602, f"参数 name 非法或重复: {d}")
            seen.add(name)
            if (isinstance(val, bool) or not isinstance(val, (int, float))
                    or isinstance(step, bool)
                    or not isinstance(step, (int, float)) or step <= 0):
                raise BridgeError(
                    -32602,
                    f"参数 {name} 需要 数值 value + 正数 step: {d}")
            if not literal_present(source, val):
                raise BridgeError(
                    -32602,
                    f"参数 {name} 的 value={val} 未出现在 source 数值字面量"
                    "中——申报不完整即拒（参数表必须与代码一致；防隐藏"
                    "真正被调的关键参数绕过平坦性检查）")

    def _factor_day_perm_test(self, params):
        """日期置换 null（2026-08-25 用户决策：真实结构上的时序置换）。

        真实因子截面 × 真实收益截面，只随机重排配对——一切真实市场
        结构保留，专测时序对齐分量（与 column-perm 的截面分配分量
        正交互补）。**report-only**：alignment_dependent 标注不拒收——
        p 小 = 真短周期因子与时序性过拟合的并集（样本内不可分），
        门方向与阈值由 Phase 6 校准（registry 已入册 × 证伪 preset
        混淆矩阵）后启用。submit 门同源调用本方法。

        种子默认从环境指纹派生（同环境可复现），显式 seed 覆盖。"""
        import hashlib

        env = self._require_panel_env(params.get("envId", "primary"))
        source = params.get("source")
        if not source:
            raise BridgeError(-32602, "source 必填（def factor(env) 源码）")
        m = max(20, min(int(params.get("m", 200) or 200), 500))
        seed = params.get("seed")
        if seed is None:
            fp = self._env_full_fingerprint(params.get("envId", "primary")) or "nofp"
            seed = int(hashlib.sha256(str(fp).encode("utf-8")).hexdigest()[:8], 16)
        else:
            seed = int(seed)
        result = self._run_factor("factor.day_perm_test", str(source),
                                  {"m": m, "base_seed": seed}, env)
        if not isinstance(result, dict):
            raise BridgeError(-32003, f"日期置换测试返回异常: {type(result).__name__}")
        result["seed"] = seed
        result["gate"] = ("report-only（对齐依赖标注，不拒收——门方向待校准）。"
                          "p_two 小=时序对齐分量显著（真短周期因子或时序性过拟合"
                          "的并集，样本内不可分）；p_two 大=持久倾斜结构主导"
                          "（合法截面 alpha 形态，由 column-perm 认证）")
        return result

    def _factor_walk_forward(self, params):
        env = self._require_panel_env(params.get("envId", "primary"))
        # 2026-08-18 生产审计修正（test 泄漏）：walk_forward 曾默认跑到数据尾，
        # fold4/fold5 直接把 test 区间的 IC 表现暴露给入册决策——test_lock 只
        # 护 evaluate_test，这条路绕过了它。现强制 region 感知：
        #   - 无 t1 → 默认 sel_end（selection 区，诊断用）
        #   - 显式 t1 > sel_end → 拒绝（test 只经 factor_evaluate(stage='test')
        #     的 finalize 流程一次性消费）
        sel_end = env.calibration.sel_end
        t1 = params.get("t1_date")
        if t1 is None:
            t1 = sel_end
        elif sel_end:
            import pandas as _pd
            try:
                t1_over = _pd.Timestamp(str(t1)) > _pd.Timestamp(str(sel_end))
            except Exception:
                t1_over = True  # 解析不了的日期越界处理（保守拒绝）
            if t1_over:
                raise BridgeError(
                    -32003,
                    f"walk_forward 的 t1_date={t1} 越过 sel_end={sel_end}——test 区只经 "
                    "factor_evaluate(stage='test') 的 finalize 流程一次性消费，"
                    "不得经 walk_forward 窥视。省略 t1_date 即默认限制在 selection 区。")
        params = {**params, "t1_date": t1}
        result = self._run_factor("factor.walk_forward", params.get("source", ""), params, env)
        if isinstance(result, dict):
            result["region_note"] = (f"t1 已限制在 sel_end={sel_end}（selection 区）——"
                                     "test 区不经 walk_forward 暴露")
        return result

    def _factor_audit(self, params):
        # C2 延伸（2026-08-29 生产事故）：audit 在个股面板上是重方法
        # （200 次列置换 + 内嵌全量 evaluate）——job_id 取结果 / async 提交
        if params.get("job_id"):
            return self._async_result(str(params["job_id"]))
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        if params.get("async"):
            return self._async_submit("factor.audit", params.get("source", ""),
                                      params, env, env_id)
        return self._run_factor("factor.audit", params.get("source", ""), params, env)

    # ---- 随机种子生成器（用户决策 2026-08-17：随机校准替代固定面板）----
    def _factor_random_generate(self, params):
        """随机因子生成。mode: explore（生成+轻量IC+top_k 源码）| null-calibration（null 地形）。

        2026-08-18 生产审计修正（seed 重放）：explore 显式 seed 与 null 校准
        seed 相同时，前 n 棵树与 null 校准完全重放（信息量为零，但 duplicate
        检测只报「疑似重复」，agent 会当作新发现叙事——实测发生过）。现：
        - 冲突 → 拒绝并说明
        - 未传 seed → 运行时随机派生并在返回中记录 seed_used（可复现）
        """
        from .factor import random_gen

        mode = params.get("mode", "explore")
        n = int(params.get("n", 50))
        opset = random_gen.effective_operator_set(self.state_root)

        if mode == "null-calibration":
            env_id = params.get("envId", "primary")
            env = self._require_panel_env(env_id)
            seed = int(params.get("seed", 42))
            # 指纹硬门（2026-08-19）：写盘时绑定环境三元组指纹，evaluate 读时校验
            # v2（2026-08-20）：菜单内全部 horizon 各建一份分位基线 + 实测跨
            # horizon 相关先验（N_eff 谱方法用）。成本 × 菜单宽度。
            try:
                fp_env = self._resolve_env_id(env_id)
            except Exception:
                fp_env = env_id
            menu = list(getattr(env.calibration, "horizon_menu", None)
                        or [env.calibration.horizon])
            return random_gen.run_null_calibration(
                env, self.state_root, n=n, seed=seed, opset=opset,
                env_fingerprint=self._env_full_fingerprint(fp_env),
                horizons=menu,
                # 换手定价（2026-08-28）：成本口径戳进 landscape（v1 毛段，
                # net 重校时换版本串 + spread_cost）
                cost_model_version=MINING_CONFIG.get(
                    "cost_model_version", "flat:v1"),
                on_progress=lambda done, total: self._progress(
                    "null-calibration", done, total))

        # explore：生成 n → 轻量 IC 排序 → top_k 源码 + 表达式
        env = self._require_panel_env(params.get("envId", "primary"))
        top_k = int(params.get("top_k", 5))
        landscape = random_gen.read_null_landscape(self.state_root)
        null_seed = (landscape or {}).get("seed") if (landscape or {}).get("calibrated", True) else None
        if "seed" in params and params["seed"] is not None:
            seed = int(params["seed"])
            if null_seed is not None and seed == int(null_seed):
                raise BridgeError(
                    -32602,
                    f"explore seed={seed} 与 null 校准的 seed 相同——生成的前 {n} 棵树"
                    f"将与 null 校准（n={landscape.get('n_generated')}）的前缀完全重放，"
                    "信息量为零。换一个 seed，或省略 seed 参数走运行时自动派生。")
            seed_note = f"seed={seed}（显式指定）"
        else:
            import secrets as _secrets
            seed = _secrets.randbelow(10 ** 9)
            seed_note = (f"seed_used={seed}（运行时自动派生，与 null 校准 seed={null_seed} "
                         "不冲突；复现本批结果时用该 seed）")
        rng = np.random.default_rng(seed)
        # spread 分位参照（WS-C 2026-08-27）：landscape 指纹匹配时取主
        # horizon 的 spread 段——light_ic_scan 里做查表插值（spread_pct）；
        # 无 landscape / 指纹不匹配 → None（spread_pct 缺省，不炸）
        spread_ref = None
        if isinstance(landscape, dict):
            try:
                fp_ok = self._landscape_fingerprint_status(
                    landscape, params.get("envId", "primary")) == "match"
            except Exception:
                fp_ok = False
            if fp_ok and isinstance(landscape.get("spread"), dict):
                spread_ref = landscape["spread"].get(
                    str(int(env.calibration.horizon)))
        results = []
        for i in range(n):
            tree = random_gen.generate_tree(rng, opset)
            try:
                F = random_gen._eval_tree(tree, env)
                diag = random_gen.light_ic_scan(F, env, spread_ref=spread_ref)
            except Exception as e:
                diag = {"ic_mean": None, "ic_ir": None, "n": 0,
                        "spread_ir": None, "spread_pct": None,
                        "error": str(e)[:120]}
            results.append({
                "index": i,
                "expression": tree.to_expression(),
                "light_ic": diag,
                "tree": tree,
            })
        # P0a（2026-09-13 反同质化奖励）：novelty 贪心替代纯 argmax。
        # 生产端标定 λ=0.3（54%/40% 选种去相关，付 10%/7% IC 牺牲幅度）；
        # 历史参照 = explore_seeds.json 近 20 条已选 IC 序列（跨 farm 周期
        # 防吸引子）。novelty_lambda=0 可关（回到旧行为）。
        lam = float(params.get("novelty_lambda", 0.3))
        env_key = str(params.get("envId", "primary"))
        seeds_path = Path(self.state_root) / "explore_seeds.json"
        hist = []
        try:
            if seeds_path.exists():
                _sd = json.loads(seeds_path.read_text(encoding="utf-8"))
                hist = [list(x) for x in ((_sd.get(env_key) or [])[-20:])
                        if isinstance(x, list)]
        except Exception:
            hist = []

        def _abs_ic(r):
            v = r["light_ic"].get("ic_ir")
            return abs(float(v)) if isinstance(v, (int, float)) else None

        def _series(r):
            s = r["light_ic"].get("ic_series")
            return s if isinstance(s, list) and len(s) >= 20 else None

        top_sel, top_div = _novelty_greedy(
            results, _abs_ic, _series, top_k, hist, lam)
        top = []
        for r in top_sel:
            src, _imports = random_gen.render_factor_source(r["tree"], f"random_factor_{r['index']}")
            top.append({
                "index": r["index"],
                "expression": r["expression"],
                "light_ic": r["light_ic"],
                "source": src,
                "note": "随机幸存=选择非结论：拿 source 走标准管线 causality→evaluate→evaluate_batch(deflate)",
            })
        # 尾部线列表（WS-C / D3 双列表）：按 spread_ir 排序——尾部强、
        # IC 平庸的树首次可见。无 spread_ir 的树不进该列表（计算失败/
        # 截面样本不足）。与 IC 列表的重叠如实报告
        def _sp(r):
            v = r["light_ic"].get("spread_ir")
            return float(v) if isinstance(v, (int, float)) else None

        tail_ranked = sorted(
            (r for r in results if _sp(r) is not None),
            key=lambda r: -_sp(r))
        tail_sel, tail_div = _novelty_greedy(
            tail_ranked, _sp, _series, top_k,
            hist + [s for s in (_series(r) for r in top_sel) if s], lam)             if lam > 0 else (tail_ranked[:top_k], None)
        top_tail = []
        for r in tail_sel:
            src, _imports = random_gen.render_factor_source(r["tree"], f"random_factor_{r['index']}")
            top_tail.append({
                "index": r["index"],
                "expression": r["expression"],
                "light_ic": r["light_ic"],
                "source": src,
                "note": ("尾部线幸存（spread_ir 排序，IC 可能平庸）——top-K "
                         "组差方向的候选；admit_basis=tail 轨同样可走标准管线"
                         "评估提交"),
            })
        # 成本平局裁决列表（2026-08-28 换手率定价 WS-T3）：按
        # break_even_cost c*（每单位换手毛利，单边 bps）排序——免假设，
        # 不需要先拍成本数字即可比：统计平局间偏好低换手是偏好陈述，
        # 不是判据篡改。无 c*（毛均 ≤0/零换手/截面不足）的树不进该列表
        def _cs(r):
            v = r["light_ic"].get("break_even_cost")
            return float(v) if isinstance(v, (int, float)) else None

        cost_ranked = sorted(
            (r for r in results if _cs(r) is not None),
            key=lambda r: -_cs(r))
        top_net = []
        for r in cost_ranked[:top_k]:
            src, _imports = random_gen.render_factor_source(r["tree"], f"random_factor_{r['index']}")
            top_net.append({
                "index": r["index"],
                "expression": r["expression"],
                "light_ic": r["light_ic"],
                "source": src,
                "note": ("成本平局裁决幸存（break_even_cost 排序——每单位"
                         "换手毛利 bps，高 = 经得起贵交易）——选择不是结论，"
                         "走标准管线验证"),
            })
        # 选种历史持久化(top+top_tail 的 IC 序列,界 20)+ novelty 遥测
        try:
            _picked = [s for s in (_series(r) for r in top_sel) if s] +                       [s for s in (_series(r) for r in tail_sel) if s]
            if _picked:
                _sd = {}
                if seeds_path.exists():
                    _sd = json.loads(seeds_path.read_text(encoding="utf-8"))
                _sd[env_key] = (list(_sd.get(env_key) or []) + _picked)[-20:]
                tmp = seeds_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(_sd, ensure_ascii=False), encoding="utf-8")
                tmp.replace(seeds_path)
        except Exception:
            pass
        overlap_idx = sorted({r["index"] for r in top_sel}
                             & {r["index"] for r in top_tail})
        dist = [abs(r["light_ic"].get("ic_ir") or 0.0) for r in results]
        sp_dist = [_sp(r) for r in results if _sp(r) is not None]
        out = {
            "mode": "explore", "n": n, "seed": seed, "seed_note": seed_note, "top_k": top_k,
            "top": top,
            "top_tail": top_tail,
            "top_net": top_net,
            "overlap_indexes": overlap_idx,
            "novelty": {
                "lambda": lam,
                "top_inner_abs_corr": top_div,
                "tail_inner_abs_corr": tail_div,
                "note": "P0a 反同质化选种:组内平均两两|ρ|(λ=0.3 生产标定);历史参照 explore_seeds.json 近20条",
            },
            "abs_ic_ir_distribution": {
                "median": float(np.median(dist)) if dist else None,
                "p95": float(np.percentile(dist, 95)) if dist else None,
                "max": float(np.max(dist)) if dist else None,
            },
            "null_hint": "top 因子的 |IC_IR| 若未超 null p95，大概率是噪声（见 factor.null_landscape）",
        }
        if sp_dist:
            out["spread_ir_distribution"] = {
                "median": float(np.median(sp_dist)),
                "p95": float(np.percentile(sp_dist, 95)),
                "max": float(np.max(sp_dist)),
            }
        out["dual_list_note"] = (
            "三列表（IC 线 top / 尾部线 top_tail / 成本平局 top_net，IC∩tail "
            "重叠见 overlap_indexes）：top_tail 按 spread_ir（top-K 组差 IR）"
            "排序——尾部强、IC 平庸的构造在这份列表可见；top_net 按 "
            "break_even_cost（每单位换手毛利 bps）排序——统计平局间偏好"
            "低换手（成本定价，无奖励系数）；三列表都是选择不是结论，走"
            "标准管线验证")
        # WS-C（2026-09-01）：三列表幸存者哈希入本线台账——这些原文直进
        # 管线时引擎 trail 标 origin=random（agent 改造后哈希不同，如实
        # 回落 hypothesis）。并集追加（多批 explore 都算），台账侧截尾
        try:
            lane_explore_hashes_extend(
                self.state_root, current_lane(),
                sorted({source_fingerprint(x["source"])
                        for lst in (top, top_tail, top_net) for x in lst}))
        except Exception:
            pass
        return out

    def _factor_operators(self, params):
        """查看/配置生效算子集。action: get | set（set 覆盖式写 operator_set.json）。"""
        from .factor import random_gen
        action = params.get("action", "get")
        if action == "set":
            override = _as_dict(params.get("override") or {}, "override")
            return random_gen.write_operator_override(override, self.state_root)
        return random_gen.effective_operator_set(self.state_root)

    def _factor_null_landscape(self, params):
        """查询已持久化的 null 地形（无则 None + 提示先生成）。

        指纹硬门（2026-08-19）：fingerprint_match 报告地形与当前环境的绑定
        状态。mismatch / legacy_no_field 的地形已判无效——pool_std 回退不再
        使用它（deflated p 会拒绝给出），需重跑 null-calibration 覆盖写。
        """
        from .factor import random_gen
        landscape = random_gen.read_null_landscape(self.state_root)
        if landscape is None:
            return {"calibrated": False,
                    "hint": "尚未校准。调 factor.random_generate(mode='null-calibration', n=50) 生成经验 null 分布"}
        env_id = params.get("envId", "primary")
        status = self._landscape_fingerprint_status(landscape, env_id)
        out = {"calibrated": True, "fingerprint_match": status, **landscape}
        if status != "match":
            reason = ("旧版地形无指纹字段" if status == "legacy_no_field"
                      else "地形指纹与当前环境不匹配（数据/口径/引擎已变更）")
            out["hint"] = (f"{reason}——该地形已判无效，pool_std 估计不再使用。"
                           "重跑 factor.random_generate(mode='null-calibration') "
                           "覆盖写新地形（几分钟）。")
        return out

    # ---- library / paths / registry / state ----
    def _library_query(self, params):
        return self.library.query(params.get("query", ""), int(params.get("top_k", 5)))

    def _library_list(self, params):
        return {"configured": self.library.configured, "entries": self.library.list()}

    def _paths_query(self, params):
        layer = params.get("layer")
        if layer not in ("explored", "search_paths"):
            raise BridgeError(-32602, "layer 必须是 explored 或 search_paths")
        entries = read_json_list(layer, self.state_root)
        result: dict[str, Any] = {"layer": layer}
        # explored_preset（开源用户预置的已证伪历史）：只读合并，标注来源。
        # 加载失败必须可见（静默吞错=用户以为挂载成功其实没有）
        if layer == "explored" and self.data_config is not None and self.data_config.explored_preset:
            try:
                preset = json.loads(Path(self.data_config.explored_preset)
                                    .read_text(encoding="utf-8"))
                if isinstance(preset, list):
                    for e in preset:
                        if isinstance(e, dict):
                            e = {**e, "source": "preset"}
                            entries.append(e)
            except Exception as e:
                result["preset_error"] = (f"explored_preset 挂载失败 "
                                          f"({self.data_config.explored_preset}): "
                                          f"{type(e).__name__}: {e}")[:200]
        result["hits"] = query_entries(entries, params.get("query", ""), int(params.get("top_k", 5)))
        return result

    # 记录层 schema（老 harness 纪律的代码化：证伪三条件 / trail 五要素）
    _RECORD_SCHEMAS = {
        "explored": ("exploration", "evidence", "root_cause"),   # 证伪三条件（HARNESS §9）
        "search_paths": ("direction", "variant", "result"),
        "trail": ("round", "signal", "attribution", "next_hypothesis", "new_information"),
    }

    def _paths_append(self, params):
        layer = params.get("layer")
        entry = _as_dict(params.get("entry") or {}, "entry")
        # lane 打标（方案 A 2026-08-31）：会话身份来自 dispatch contextvar，
        # 覆盖 entry 里模型自填的同名字段——归属是基础设施事实，不是
        # 模型可声明的元数据。三层都打（explored/search_paths 读端仍
        # 全局共享，标签只作溯源）
        entry["lane"] = current_lane()
        required = self._RECORD_SCHEMAS.get(layer)
        if required:
            missing = [f for f in required if not str(entry.get(f, "") or "").strip()]
            if missing:
                raise BridgeError(
                    -32602,
                    f"{layer} 记录缺少必填字段 {missing}。"
                    + ("explored 必须满足证伪三条件：精确定义(exploration)/复现证据(evidence，引用具体数字)/根因(root_cause)"
                       if layer == "explored" else
                       "trail 必须含 new_information（这一轮引入了什么新信息源——第一优先级纪律）"
                       if layer == "trail" else
                       "search_paths 需要 direction/variant/result"))
        if layer == "explored":
            # F4 引用溯源（2026-08-24）：证伪条目携带 papers 字段 →
            # 台账标 exhausted（该论文推导的思路已证伪，后续检索
            # 直接排除——防跨纪元从同一篇论文再推导同一思路）
            result = append_explored(entry, self.state_root)
            papers = entry.get("papers")
            if papers is not None:
                if not isinstance(papers, list) or not all(
                        isinstance(x, str) for x in papers):
                    raise BridgeError(-32602,
                                      "papers 必须是字符串 arxiv_id 列表"
                                      "（如 [\"2108.05721\"]，完整 URL 也可）")
                ids = [i for i in (self._normalize_arxiv_id(x) for x in papers) if i]
                bad = [x for x, i in zip(papers, (self._normalize_arxiv_id(x) for x in papers)) if not i]
                if bad:
                    raise BridgeError(
                        -32602, f"papers 含非法 arxiv_id: {bad[:3]}。"
                        "格式：2108.05721 / abs/2108.05721 / 完整 URL")
                if ids:
                    d = self._read_papers()
                    for pid in ids:
                        e = d["papers"].setdefault(pid, {
                            "title": "", "first_seen": d.get("searches", 0),
                            "times_returned": 0, "queries": [],
                            "cited_rounds": [], "exhausted": False})
                        e["exhausted"] = True
                    self._write_papers(d)
                    result["note"] = (result.get("note", "")
                                      + f"；{len(ids)} 篇论文标记 exhausted")
            src = str(entry.get("source") or entry.get("factor_source") or "")
            # 正式证伪的因子入 falsified 池（防重复挖坟；有指纹缓存则带数值指纹）
            if src:
                # 源码外挂库：证伪源码全文入库（池只存签名，全文可回溯）
                try:
                    store_factor_source(src, self.state_root)
                except Exception:
                    pass
                key = source_fingerprint(src)
                cached = self._fp_cache.get(key)
                try:
                    if cached is not None:
                        self._pool().offer_falsified(src, str(entry.get("exploration", ""))[:60],
                                                     F=cached[1], sig_idx=cached[0])
                    else:
                        self._pool().offer_falsified(src, str(entry.get("exploration", ""))[:60])
                except Exception:
                    pass
            result["note"] = (result.get("note", "") +
                              "；已同步入证伪记忆池（falsified set）")
            return result
        if layer == "search_paths":
            return append_search_path(entry, self.state_root)
        if layer == "trail":
            # S2 宣言分型校验（2026-09-04 探索相似性防线）：「新」宣称与
            # 结构/L1 对账，不符则拒收重写（fail-open：校验故障不阻断写入）
            self._declaration_gate(entry)
            # 穷尽宣告拒收（2026-08-24）：停笔宣言混进 next_hypothesis 会被
            # 引擎回显背书成状态（session.jsonl 实测 12/12 假穷尽）——写入口
            # 直接拒绝，让停点定义权留在引擎机械判据手里。
            # 灵感重置声明（2026-09-01 WS-B）先于停笔判定：换源宣言不是
            # 停笔宣言，理由里带「无新假设」等措辞不连坐（配额由
            # _loop_directive 侧受理与限频，写入口只放行形态合法者）
            nh = str(entry.get("next_hypothesis", "") or "")
            if _inspiration_decl_match(nh) is None:
                hit = _surrender_match(nh)
                if hit:
                    raise BridgeError(
                        -32602,
                        f"next_hypothesis 被拒收（「{nh[:80]}」）：{hit}。"
                        "停点仅由引擎机械判据触发（轮次/试验上限、IC_IR 改善收敛、"
                        "finalize）。把 next_hypothesis 改写为可执行的新假设"
                        "（构造什么、测什么），然后重写本条 trail")
            # F4 引用溯源：文献驱动的假设必须携带 papers（paper→hypothesis
            # 链条可验证——「直接迁移自论文X」的宣称不再不可审计）
            papers = entry.get("papers")
            if papers is not None:
                if not isinstance(papers, list) or not papers:
                    raise BridgeError(
                        -32602,
                        "papers 必须是非空 arxiv_id 列表（如 [\"2108.05721\"]，"
                        "完整 URL 也可）。文献驱动的假设必填 papers——"
                        "不引用具体论文的「迁移」宣称不可审计")
                ids = [self._normalize_arxiv_id(x) for x in papers]
                if not all(isinstance(x, str) for x in papers) or not all(ids):
                    raise BridgeError(
                        -32602, f"papers 含非法 arxiv_id: {papers[:3]}。"
                        "格式：2108.05721 / abs/2108.05721 / 完整 URL")
                d = self._read_papers()
                rnd = entry.get("round")
                for pid in ids:
                    e = d["papers"].setdefault(pid, {
                        "title": "", "first_seen": d.get("searches", 0),
                        "times_returned": 0, "queries": [],
                        "cited_rounds": [], "exhausted": False})
                    if rnd is not None:
                        e["cited_rounds"].append(rnd)
                self._write_papers(d)
            res = append_trail(entry, self.state_root)
            # 方向段轮次计数（2026-08-25 arc 化；2026-08-31 lane 化）：
            # 一条叙事 trail = 本线当前方向段一轮。上限按 arc 计不按
            # 终身计——机械换向（本线家族链断）自动归零。计数失败不阻断
            # 叙事写入（少计只会更保守）。
            try:
                arc_rounds_bump(self.state_root, lane=current_lane())
            except Exception:
                pass
            return res
        raise BridgeError(-32602, "layer 必须是 explored/search_paths/trail")

    def _state_trail_summary(self, params):
        """聚合视图：引擎层 trail（硬事实）+ agent 层 trail（叙事）+ 挖掘状态。"""
        import time as _t
        engine_trail = []
        p = Path(self.state_root) / "trail_engine.json"
        if p.exists():
            try:
                engine_trail = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                engine_trail = []
        agent_trail = read_json_list("trail", self.state_root)
        agent_own = own_entries(agent_trail, current_lane())
        explored = read_json_list("explored", self.state_root)
        mining = read_mining_state(self.state_root)
        # 与 _loop_directive 同口径注入读时计算的计数（2026-08-25 修正：
        # 此前直接传 mining，cluster_trials 从不在盘上 → termination 恒显
        # 示「簇试验 0/200」，与 loop 指令口径不一致）。2026-08-31 lane
        # 化：streak/arc 本线，n_trials 全局（计价口径与 loop 一致）
        engine_own = own_entries(engine_trail, current_lane())
        streak = self._family_streak(engine_own)
        n_trials = len({(e.get("source_hash"), e.get("horizon"))
                        for e in engine_trail if isinstance(e, dict)
                        and e.get("source_hash")})
        effective = {**mining, "arc_rounds": lane_arc_rounds(mining,
                                                             current_lane()),
                     "n_trials": n_trials, "cluster_trials": streak}
        verdicts = {}
        red_flagged = []
        for e in engine_trail:
            v = e.get("verdict") or "unknown"
            verdicts[v] = verdicts.get(v, 0) + 1
            if e.get("red_flags"):
                red_flagged.append({"source_hash": e.get("source_hash"),
                                    "stage": e.get("stage"),
                                    "red_flags": e["red_flags"][:3]})
        # last_engine_trail 条目投影（2026-08-27 WS-A）：全字段每条 ~2.4KB
        # （ic_series_sketch/suspects/construction_fp/tail 全量），且排在
        # 响应 dict 末位——DH 呈现层按字符截断时最先死，agent 看不见最近
        # 历史。投影每条 ≈150B，并前移到 loop 之后（即使未来再被截，最近
        # 历史最先存活）。
        def _trail_entry_compact(e) -> dict:
            if not isinstance(e, dict):
                return {"ts": None, "source_hash": None}
            tail = e.get("tail") if isinstance(e.get("tail"), dict) else {}
            topn = tail.get("topn") if isinstance(tail.get("topn"), dict) else {}
            return {
                "ts": e.get("ts"),
                "source_hash": str(e.get("source_hash") or "")[:12],
                "horizon": e.get("horizon"),
                "stage": e.get("stage"),
                "origin": e.get("origin"),
                "ic_ir": e.get("ic_ir"),
                "verdict": e.get("verdict"),
                "red_flags": list(e.get("red_flags") or [])[:2],
                "tail": {"spread_ir": tail.get("spread_ir"),
                         "placebo_z": topn.get("placebo_z")},
            }

        return {
            "generated_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            "mining": {k: mining.get(k) for k in ("round", "global_fail_streak", "finalized")},
            "termination": check_termination(effective),
            # 自主性停走指令（2026-08-21）：恢复会话/定方向前先看这里——
            # loop.state=running 必须继续内循环，不得停下来问用户
            "loop": self._loop_directive(),
            "last_engine_trail": [_trail_entry_compact(e) for e in engine_own[-5:]],
            "evaluations": {"total": len(engine_trail),
                            "lane": len(engine_own),
                            "foreign": len(engine_trail) - len(engine_own),
                            "unique_sources": len({e.get("source_hash") for e in engine_trail}),
                            "verdict_counts": verdicts},
            # 源码外挂库（2026-08-29）：source_hash 即键，按
            # sources/<hash[:2]>/<hash>.py 取全文
            "source_store": factor_source_stats(self.state_root),
            "agent_rounds": len(agent_own),
            "explored_count": len(explored),
            "red_flagged": red_flagged[-10:],
            "pool": self._pool().stats(),
            "note": ("引擎层 trail 为 evaluate 自动记录（硬事实，不可瞒报）；"
                     "last_engine_trail 为紧凑投影（每条 ≈150B，防呈现层截断）"
                     "且只列本线最近 5 条（evaluations.lane/foreign 为分线计数，"
                     "verdict_counts 仍全局）；完整条目人读 stateRoot/trail_engine.json；"
                     "agent 层叙事见 factor_query_paths(layer='trail' 不支持时读文件)"),
        }

    def _state_mining_get(self, params):
        return read_mining_state(self.state_root)

    def _state_mining_record(self, params):
        return record_round(_as_dict(params.get("entry") or {}, "entry"), self.state_root)

    def _state_mining_reset(self, params):
        return reset_mining_state(self.state_root)

    # ---- 状态重置（2026-08-18 删库事故的产品化对策：带 scope + 自动备份） ----
    # "pool" 是目录（双池 json + fp/*.npy 数值指纹）：备份用 copytree、删除用 rmtree
    RESET_TARGETS: dict[str, list[str]] = {
        # trail_engine.json 是引擎层硬事实 trail，与 agent 叙事层 trail 同属 mining 轨迹；
        # pool/ 是有界双池记忆（active/falsified + .npy 指纹），同属挖掘记忆；
        # papers.json 是文献台账（F1，2026-08-24），同属挖掘记忆
        "mining": ["trail.json", "trail_engine.json", "explored_paths.json",
                   "search_paths.json", "mining_state.json", "pool",
                   "papers.json", "sources", "explore_seeds.json"],
        # 不在任何 scope:registered_ledger.jsonl(分层计价台账,同 ledger.json
        # 待遇——机制层 M 的依据,不可由任何 reset 抹除)与 journal/(模型自有
        # 解释层,独立生命周期,走 journal 工具自管)
        "landscape": ["null_landscape.json", "operator_set.json"],
        "registry": ["registry.json"],
        "config": ["data-config.json"],
    }

    def _state_reset(self, params):
        """按 scope 重置状态；删除前自动备份到 stateRoot/backups/<时间戳>/。

        test_lock.json 刻意不在任何 scope：纪律锁（test 区一次性）不可被工具重置，
        只能手动删除——防止换口径/换数据反复消费 test。
        逐文件 try：备份失败（磁盘满/权限）的文件跳过不删并记入 skipped——
        不允许出现「已删但备份残缺」的无跟踪状态。
        """
        import shutil
        import time as _time

        scope = params.get("scope", "mining")
        # reset 硬拦（2026-08-22 事故：agent 在 IC_IR=0.590 被 deflation 拒后
        # 自行 scope=mining 洗掉 133 次试验重评——marginal miss 时刻的理性
        # 作弊路径）。有分量的挖掘轨迹 + 非空 registry 时，自助 reset 需
        # 用户显式授权（confirm=True + reason 留痕）；agent 无授权调用直接拒。
        # ledger.json 刻意不在任何 scope：审计账本只随 registry 清空而失去
        # 意义，mining/landscape/reset 都不动它。
        if scope in ("mining", "all"):
            reason = params.get("reason") or ""
            confirm = params.get("confirm") is True
            try:
                n_entries = len(self._read_engine_trail())
            except Exception:
                n_entries = 0
            try:
                n_reg = sum(1 for e in read_registry(self.state_root)
                            if isinstance(e, dict))
            except Exception:
                n_reg = 0
            if (n_entries >= 50 or n_reg > 0) and not (confirm and str(reason).strip()):
                raise BridgeError(
                    -32003,
                    f"拒绝自助 reset（scope={scope}）：现有 {n_entries} 条试验轨迹"
                    f" + registry {n_reg} 条——搜索史不可由 agent 单方面抹除"
                    "（洗 trail 降 N 是假门：过门不构成统计证据）。"
                    "如确需重置（换数据集/换口径开新研究），请用户显式传 "
                    "confirm=true 并写 reason。")
        if scope == "all":
            targets = []
            for files in self.RESET_TARGETS.values():
                targets.extend(f for f in files if f not in targets)
        elif scope in self.RESET_TARGETS:
            targets = list(self.RESET_TARGETS[scope])
        else:
            raise BridgeError(-32602,
                              f"未知 scope '{scope}'（可用: mining | landscape | registry | config | all）")
        backup_dir = None
        removed = []
        skipped = []
        # 写锁内删除（并行 P0）：与并发追加互斥，避免删到一半被写入
        with state_write_lock(self.state_root):
            for name in targets:
                p = Path(self.state_root) / name
                if not p.exists():
                    continue
                try:
                    if backup_dir is None:
                        # 加毫秒防同秒两次 reset 互相覆盖备份
                        backup_dir = Path(self.state_root) / "backups" / (
                            _time.strftime("%Y%m%d-%H%M%S") + f"-{int(_time.time() * 1000) % 1000:03d}")
                        backup_dir.mkdir(parents=True, exist_ok=True)
                    if p.is_dir():
                        # 目录目标（pool/）：整目录备份 + 整目录删除
                        shutil.copytree(p, backup_dir / name)
                        shutil.rmtree(p)
                    else:
                        shutil.copy2(p, backup_dir / name)
                        p.unlink()
                    removed.append(name)
                except Exception as e:
                    skipped.append({"file": name, "error": f"{type(e).__name__}: {e}"[:150]})
        if scope in ("config", "all") and "data-config.json" in removed:
            # 配置被清：内存状态同步归零（文件即事实源）
            self.data_config = None
            self._config_mtime_ns = None
            self._config_error = None
            self.envs.clear()
            self.minute_features.clear()
            self._apply_library()
        result = {
            "ok": not skipped, "scope": scope, "removed": removed,
            "backup": str(backup_dir) if backup_dir is not None else None,
            "note": ("test_lock 不在任何 scope（纪律锁只能手动删）；备份在 stateRoot/backups/ 下"
                     "可随时还原"),
        }
        if skipped:
            result["skipped"] = skipped
            result["note"] += f"；{len(skipped)} 个文件备份失败已跳过未删（见 skipped）"
        return result

    def _registry_get(self, params):
        """紧凑投影（2026-08-27 规划书 WS-A / D1）。

        取证：全量响应 232KB（29 条 × ~8KB，diagnosis/noise_gate/flatness
        等全字段）经 TS stringify 再膨胀 ~40%，DH 呈现层按字符截断——
        registry 按时间序追加，截掉的正是最新条目（拒收理由、tracks
        标注在每条 entry 尾部），agent 实际活在「看不见账本」状态。

        本投影每条 ≤ ~200B，29 条 ≈ 6KB：截断永不触发。无 agent 侧全量
        入口（全量 = 人读 stateRoot/registry.json；TS detail 参数是后续
        可选增强，需 TS 管线）。"""
        registry = read_registry(self.state_root)
        entries = []
        by_track = {"ic": 0, "tail": 0, "dual": 0}
        accepted_n = 0
        for e in registry:
            if not isinstance(e, dict):
                continue
            acc = bool(e.get("accepted"))
            if acc:
                accepted_n += 1
            tracks = e.get("tracks") if isinstance(e.get("tracks"), dict) else {}
            ic_ok = bool((tracks.get("ic") or {}).get("accepted"))
            tail_ok = bool((tracks.get("tail") or {}).get("accepted"))
            basis = e.get("admit_basis") if e.get("admit_basis") in ("ic", "tail") \
                else "ic"
            if ic_ok and tail_ok:
                by_track["dual"] += 1
            elif acc:
                by_track[basis] += 1
            diag = e.get("diagnosis") if isinstance(e.get("diagnosis"), dict) else {}
            diag_tail = diag.get("tail") if isinstance(diag.get("tail"), dict) else {}
            item = {
                "name": e.get("name"),
                "ts": e.get("ts"),
                "accepted": acc,
                "admit_basis": basis,
                "tracks": {"ic": ic_ok, "tail": tail_ok},
                "ic_ir": e.get("ic_ir_train"),
                "spread_ir": diag_tail.get("spread_ir"),
                # 换手定价（2026-08-28 WS-T1）：c* = 每单位换手毛利
                # （单边 bps，免假设可比）+ 换手 + net 陪跑——旧条目 None
                "net_spread_ir": diag_tail.get("net_spread_ir"),
                "turn": diag_tail.get("turn_tail"),
                "break_even_cost": diag_tail.get("break_even_cost"),
            }
            if not acc:
                reason = e.get("reason")
                item["reject_kind"] = e.get("reject_kind")
                item["reject_reason"] = str(reason)[:120] if reason else None
            entries.append(item)
        return {
            "count": len(entries),
            "accepted": accepted_n,
            "by_track": by_track,
            "entries": entries,
            "note": ("紧凑视图（防呈现层截断）；投影字段覆盖正交性审计与避坑"
                     "所需；完整诊断为引擎内部数据，人读 stateRoot/registry.json"),
        }

    def _authoritative_dsr_stats(self, source_hash: str | None,
                                 horizon: int | None) -> dict | None:
        """trail_engine 中该 source_hash 最新评估的 DSR 充分统计量。

        receipt 缓存随进程丢失，trail 是盘上硬事实——submit 重算优先用
        引擎侧数字，diagnosis 自报的 (sr_hat/skew/kurt/n_obs) 只在无
        trail 条目时兜底。horizon 匹配：双方都在场时须相等；任一侧
        缺失（旧条目无 horizon / 直调诊断）→ 容忍取最新。"""
        if not source_hash:
            return None
        best = None
        for e in self._read_engine_trail():
            if (not isinstance(e, dict)
                    or e.get("source_hash") != source_hash):
                continue
            st = e.get("dsr_stats")
            if not isinstance(st, dict) or st.get("sr_hat") is None:
                continue
            eh = e.get("horizon")
            if (horizon is not None and eh is not None
                    and int(eh) != int(horizon)):
                continue
            best = st  # trail 时间正序，最后一个即最新
        return best

    def _run_gates_parallel(self, gates: dict):
        """submit 门并发（方案 C，2026-08-29 用户拍板）。

        提交全部门 → **等全部完成** → 返回 {name: Future}。调用方按
        原判定序 .result() 合并，异常语义由各门原有的 try/except 包装
        逐字保留——本方法只负责并发，不触碰判定与事务语义。各门是独立
        worker 子进程（单线程 BLAS），线程只阻塞在 communicate 上等
        待，无共享可变状态。等全部完成而非首个失败即返回：worker 无法
        安全中断，失败路径的成本 = 最慢门的墙，可接受（语义优先）。"""
        import concurrent.futures as _cf

        with _cf.ThreadPoolExecutor(max_workers=max(2, len(gates))) as pool:
            return {name: pool.submit(fn) for name, fn in gates.items()}

    def _registry_submit(self, params):
        """提交候选到 registry。收 factor_evaluate 的完整诊断对象做结构校验
        与充分统计量重算，随后引擎自主执行四个过拟合门（噪声世界 / day-perm /
        平坦性邻域 / spread 噪声）与 G1 权威 placebo 重算（WS2：样本加厚
        m≥60 + 预算自适应，evaluate 轻量值只作 degraded 兜底）——慢因子
        一次 submit 可达数分钟，属正常。

        批次1a 强化：
        - receipt 校验（H2）：diagnosis 带 _receipt 则逐位核对关键数字
          （含 deflated_train 充分统计量）——不匹配 = 编造/删改痕迹，
          拒收；无 receipt / 缓存丢失 → verified:false 降级（不冤枉、
          可追溯），p 重算改用 trail_engine 的权威统计量
        - 铁律代码化：同一 source_hash 不得以不同名字重复登记
        - 版本溯源：entry 自动附 fingerprint / engine_version / source_hash
        """
        name = params.get("name") or params.get("signal") or "unnamed"
        signal = params.get("signal", "")
        diagnosis = params.get("diagnosis") or params.get("result")
        if not isinstance(diagnosis, dict) or "ic_ir_train" not in diagnosis:
            raise BridgeError(-32602, "registry_submit 需要 diagnosis（factor_evaluate 的完整诊断对象，含 ic_ir_train）")
        # 坑10 加固（2026-09-06）：提交入口即加载面板。此前的 A3 提交期
        # 重算在面板未加载的进程内会把环境指纹退回 spec 声明口径，与 null
        # 地形绑定的加载后口径必然 mismatch → pool_std=None → "不可算"
        # 拒绝（2026-09-04/05 五连实证；同进程 data.load 后即恢复）。前置
        # _require_panel_env 一次性修复整条重算链。
        _entry_env = self._require_panel_env(params.get("envId", "primary"))
        verified = self._verify_receipt(diagnosis)
        if verified is False:
            # receipt 在场但不匹配 = 关键数字（IC/充分统计量）被改动——
            # 编造痕迹，拒收（2026-08-25 之前只降级不拦截：手构高 sr_hat
            # 可直推 acceptance）。诚实提交是逐字复制的，不会命中这里。
            raise BridgeError(
                -32003,
                "receipt 校验失败：diagnosis 关键数字与引擎 receipt 记录不一致"
                "（编造/删改痕迹）。diagnosis 必须原样来自 factor_evaluate ——"
                "改数字后再提交不会重算出你想要的结果（submit 重算优先用 "
                "trail_engine 权威统计量）")
        source = str(params.get("source", ""))
        source_hash = source_fingerprint(source) if source else None
        # source 一致性硬校验（2026-08-26 rank_persistence_w30 事故根因：
        # agent 提交时重打的 source 与评估时字节不一致 → hash 漂移 →
        # 尾块反查失败 → 程序性拒绝烧名）。诊断的 _meta.source_hash 是
        # evaluate 时真实评估对象的指纹——在场且不一致 = 提交物与评审物
        # 不是同一份代码，事务性拒绝（不落盘），错误信息给出两侧 hash
        # 引导原样复制。
        _diag_hash = None
        if isinstance(diagnosis.get("_meta"), dict):
            _dh = diagnosis["_meta"].get("source_hash")
            if isinstance(_dh, str) and _dh:
                _diag_hash = _dh
        if (source and source_hash and _diag_hash
                and _diag_hash != source_hash):
            raise BridgeError(
                -32602,
                f"提交的 source 与 diagnosis 的 source 不一致（提交 "
                f"{source_hash[:12]} vs 诊断 {_diag_hash[:12]}）——diagnosis "
                "必须来自同一份 source 的 factor_evaluate。原样复制 evaluate "
                "时传入的 source 字符串（字节级一致，含空格/换行/注释）"
                "重新提交，不要重新输入或微调")
        existing = read_registry(self.state_root)
        if source_hash:
            # hash 撞铁律 + 程序性治愈（2026-08-26）：同 hash 异名的旧条目
            # 若全部是程序性拒绝（因子从未被评审）→ 删除放行重试；
            # 任何 accepted/实质性拒绝在场 → 维持铁律
            _hash_dups = [e for e in existing
                          if isinstance(e, dict)
                          and e.get("source_hash") == source_hash
                          and e.get("name") != name]
            if _hash_dups:
                if all(e.get("accepted") is False and _is_procedural_reject(e)
                       for e in _hash_dups):
                    # 治愈写入：锁内重读过滤（读在锁外有丢并发更新窗口）
                    with state_write_lock(self.state_root):
                        cur = read_registry(self.state_root)
                        cur = [e for e in cur if e not in _hash_dups]
                        write_registry(cur, self.state_root)
                else:
                    raise BridgeError(
                        -32003,
                        f"该因子源码已以名字「{_hash_dups[0].get('name')}」登记过"
                        "（铁律：同一因子不得重复登记为新发现）")
        # 2026-08-18 生产审计补充（同名重复入册事故）：同一名字只允许一条 entry——
        # 实测 agent 想修正描述却反复 submit（同 hash 同名 / 改源码同名各一次），
        # registry 被同一因子灌 3 条。两种情况都拒绝并引导 update / 换名。
        dup = next((e for e in existing if e.get("name") == name), None)
        if dup is not None:
            # 程序性拒绝治愈（2026-08-25 pw15 事故首倡，2026-08-26 扩展为
            # 拒绝分类制）：旧 entry 被拒但因子从未被真正评审（尾块反查
            # 失败/基础设施失败）= 名字被无意义烧掉——删除放行重试。
            # 实质性拒绝（门真判了）照旧烧名。
            if dup.get("accepted") is False and _is_procedural_reject(dup):
                # 治愈写入：锁内重读过滤（同上）
                with state_write_lock(self.state_root):
                    cur = read_registry(self.state_root)
                    cur = [e for e in cur if not (isinstance(e, dict)
                                                  and e.get("name") == name)]
                    write_registry(cur, self.state_root)
            elif source_hash and dup.get("source_hash") == source_hash:
                raise BridgeError(
                    -32003,
                    f"名字「{name}」已登记过同一因子（铁律：不得重复入册）。"
                    "修正描述/追加备注用 factor_registry_update(name, signal/note)——"
                    "不要重复 submit。")
            else:
                raise BridgeError(
                    -32003,
                    f"名字「{name}」已被另一个因子占用（源码不同）。"
                    f"若这是新变体请换一个名字重新 submit；若只是想修正「{name}」的描述，"
                    "用 factor_registry_update——改源码换汤不换药的重复登记会被拒绝。")
        # A3（2026-08-19 堵冻结诊断绕过）：提交时刻以**当前 trail 的 N_eff**
        # 重算 deflated p——诊断生成后到提交之间的新试验（含 batch 通道、
        # 其他因子的 evaluate）全部计入。此前 submit 直接用诊断里冻结的
        # deflated_train.p：batch 成员不写 trail → N 恒 1 → 拿 batch 诊断
        # 直接 submit 即绕过 D7 门控（实验证实：dev 路径 p=0.674 拒、
        # batch 路径 p=0.0003 过，同一因子同一数据）。
        # 充分统计量 (sr_hat/skew/kurt/n_obs) 来自诊断本身——纯算术重算，
        # 不 require env / 不编译 / 不重评估（submit 去耦合设计不破）。
        #
        # 生产审核硬化（2026-08-19，ADV-2/3/4）：
        # - deflated_train 必须是非空 dict（删字段提交 = 拒）——evaluate 产出的
        #   诊断永远带它，缺失即删改痕迹
        # - 带 p 但缺 sr_hat 充分统计量 = 拒（伪造 p 无法重算验证）
        # - 无 source/_meta 的诊断也重算（N_eff 按当前 trail，本因子不计入）：
        #   伪造诊断不得因缺 hash 而逃脱 trail 口径
        dp = diagnosis.get("deflated_train")
        if not isinstance(dp, dict) or not dp:
            raise BridgeError(-32602,
                              "diagnosis 缺 deflated_train——诊断必须原样来自 "
                              "factor.evaluate，不得删改后提交")
        if dp.get("sr_hat") is None and dp.get("p") is not None:
            raise BridgeError(-32602,
                              "deflated_train 带 p 但缺 sr_hat 充分统计量——无法做"
                              "提交时刻重算（防伪造 p）。诊断必须原样提交")
        # 充分统计量完整性（2026-08-21 tsi_ad 事故）：agent 手工构造诊断时
        # 只抄了 sr_hat/n_obs，丢了 skew/kurt → 重算 float(None) 静默 None →
        # 错误消息误报"缺池分布基线"，把 agent 引去无效的 null 重校准。
        # sr_hat 在场但四项统计量不齐 = 删改痕迹，明确拒绝并指出缺什么。
        if dp.get("sr_hat") is not None:
            _missing_stats = [f for f in ("skew", "kurt", "n_obs")
                              if dp.get(f) is None]
            if _missing_stats:
                raise BridgeError(
                    -32602,
                    f"deflated_train 缺充分统计量 {_missing_stats}——提交时刻重算"
                    "需要完整的 (sr_hat, skew, kurt, n_obs)。诊断必须原样来自 "
                    "factor.evaluate / factor_evaluate_batch（带全部字段），"
                    "不得手工构造或摘要后提交")
        if dp.get("sr_hat") is not None:
            src_hash_dp = source_hash or (diagnosis.get("_meta") or {}).get("source_hash")
            hor_dp = diagnosis.get("horizon")
            if not isinstance(hor_dp, (int, float)) or int(hor_dp) <= 0:
                hor_dp = None
            sub_env = params.get("envId", "primary")
            # v3：门参数 bar_sigma = E[max|X|]（含单调包络；n_trials/n_eff
            # 降级为遥测）。_trial_stats 返回 (stats_dict, trail_std)。
            stats, trail_std = self._trial_stats(
                str(src_hash_dp) if src_hash_dp else None, hor_dp, sub_env)
            pool_std, _sub_detector = self._resolve_pool_std(sub_env, trail_std, hor_dp)
            # 充分统计量权威源（2026-08-25 防编造）：trail_engine 里有本因子
            # 的引擎侧记录时一律用引擎数字——diagnosis 自报的 sr_hat 只在
            # 无记录时兜底（此时 receipt_verified=False 已标注不可信）。
            auth = self._authoritative_dsr_stats(
                str(src_hash_dp) if src_hash_dp else None, hor_dp)
            if auth is not None:
                sr_u, g3_u, g4_u, n_u = (auth.get("sr_hat"), auth.get("skew"),
                                         auth.get("kurt"), auth.get("n_obs"))
            else:
                sr_u, g3_u, g4_u, n_u = (dp.get("sr_hat"), dp.get("skew"),
                                         dp.get("kurt"), dp.get("n_obs"))
            p_new = _dsr_p_from_stats(sr_u, g3_u, g4_u, n_u,
                                      stats["bar_sigma"], pool_std)
            diagnosis["deflated_train"] = {**dp, "p": p_new,
                                           "n_trials": float(stats["n_trials"]),
                                           "n_eff": float(stats["n_eff"]),
                                           "bar_sigma": float(stats["bar_sigma"]),
                                           "pool_std": pool_std,
                                           "nu": stats.get("nu"),
                                           "stats_from_trail": auth is not None,
                                           "recomputed_at_submit": True}
        # 通道⑤ 豁免(批次4,2026-09-13):孵化幸存者豁免 dev deflated p——
        # 复活证据=新鲜窗口 OOS t>z(cohort 校正),选择影子不跨样本,dev 的
        # 选择运气价不再重复征收。仅豁免多重检验红旗这一项;噪声/正交/
        # 同构/其他 red flag 照旧。验证口=incubation/survivors.jsonl(引擎写)
        incubated_marker = None
        if params.get("incubated_cohort"):
            from .incubate import is_survivor
            sv = is_survivor(self.state_root, src_hash_dp or "")
            if sv is not None:
                incubated_marker = {"cohort_id": sv.get("cohort_id"),
                                    "t": sv.get("t")}
                _rf = [f for f in (diagnosis.get("red_flags") or [])
                       if not ("deflated p" in str(f) or "池分布基线" in str(f))]
                if len(_rf) != len(diagnosis.get("red_flags") or []):
                    diagnosis["red_flags"] = _rf
                    if not _rf:
                        diagnosis["verdict"] = "pass"
                # passes_acceptance 同样门控 p(evaluate.py 单检验函数)——
                # 豁免口径:原 p 存档审计,判定走复活证据
                _dp2 = diagnosis.get("deflated_train")
                if isinstance(_dp2, dict) and _dp2.get("p") is not None:
                    _dp2["p_dev_original"] = _dp2.get("p")
                    _dp2["p"] = 0.001
                    _dp2["p_incubation_waived"] = True
        accepted, reason = _passes_acceptance(diagnosis)
        if diagnosis.get("red_flags"):
            accepted = False
            reason = f"red_flags 未清：{diagnosis['red_flags'][:2]}"
        if incubated_marker is not None and accepted:
            reason = (f"{reason} | incubated: dev deflated p 已豁免"
                      "(OOS 复活证据在 incubation/survivors.jsonl)")
        # 噪声硬门（2026-08-24 用户设计决策）：因子在随机噪声世界上的
        # 直接表现。真 alpha 按构造不可预测噪声；噪声上仍显著 = 因子
        # 公式在拟合评价 artifact（管道偏差/重叠窗口构造），无条件拒收。
        # 2026-08-25 pw15_compD_5050 事故修正语义：基础设施失败（超时/
        # worker 崩溃）= **事务中止（raise，不落盘）**——旧版写成
        # accepted=false 条目导致铁律烧名，重试被「不得重复入册」挡死。
        # 只有实质性判定（artifact 确认）才落拒绝条目。
        # ===== 五门并行（2026-08-29 方案 C）=====
        # IC 噪声 / day-perm / 平坦性 / spread 噪声 / G1 placebo 互不依赖
        # （同一 source+env 的独立诊断，结果只汇入最终判定，无交叉喂入
        # ——day-perm report-only、噪声/spread/placebo 只产 verdict）。
        # 个股面板上顺序总和（~240+20+90+78+180s）必超客户端 360s 期限
        # ——并行后墙钟 = max(门)。各门仍各享独立 worker 墙与预算自适应；
        # _run_gates_parallel 等全部完成后按**原判定序**合并，事务语义
        # 与顺序版逐字一致（任一门基础设施失败 = 中止不落盘可重试）。
        _ms = read_mining_state(self.state_root)
        _sub_env_id = params.get("envId", "primary")
        _sub_env = self._require_panel_env(_sub_env_id)
        import hashlib as _hl
        _fp_ns = self._env_full_fingerprint(_sub_env_id) or "nofp"
        _seed_ns = int(_hl.sha256(
            str(_fp_ns).encode("utf-8")).hexdigest()[:8], 16)
        # 换手率定价（2026-08-28 规划书 WS-T2 v1 双报）：判定基开关——
        # false（默认）= G2/G3 判毛口径，net 三件套只陪跑；true = 三门
        # 全 net 口径（G1 本就 net）。可被 mining_state.tail_net_basis 覆盖
        _net_basis = bool(_ms.get("tail_net_basis",
                                  MINING_CONFIG["tail_net_basis"]))
        # 尾块按 (source_hash, horizon) 匹配用的口径 + placebo 门同源
        hor_tb = diagnosis.get("horizon")
        if not isinstance(hor_tb, (int, float)) or int(hor_tb) <= 0:
            hor_tb = None
        admit_basis = str(params.get("admit_basis") or "ic")
        if admit_basis not in ("ic", "tail"):
            raise BridgeError(-32602,
                              f"admit_basis 必须是 ic | tail（收到 {admit_basis!r}）")
        fp_decl = params.get("flatness_params")
        if fp_decl is not None:
            # 申报校验前置：失败快拒，不起 worker
            self._flatness_decl_check(source, fp_decl)

        def _gate_noise_ic():
            return self._factor_noise_test({
                "envId": _sub_env_id, "source": source,
                "m": int(_ms.get("noise_gate_m", 50) or 50)})

        def _gate_dayperm():
            return self._factor_day_perm_test({
                "envId": _sub_env_id, "source": source,
                "m": int(_ms.get("day_perm_m", 200) or 200)})

        _gates = {"noise": _gate_noise_ic, "dayperm": _gate_dayperm}
        if fp_decl is not None:
            def _gate_flatness():
                return self._run_factor(
                    "factor.flatness_test", str(source),
                    {"flatness": fp_decl}, _sub_env)
            _gates["flatness"] = _gate_flatness

        def _gate_spread_noise():
            # 2026-08-31 修正：G2 恒用毛统计量。z 判据的 null=「统计量围绕
            # 0」只在毛口径成立——net 口径下随机世界均值 = −2·cost·E[turn]
            # 的确定性成本拖累，z 全员爆负（实测 13/13 |z|≥7.5）≠ artifact。
            # artifact 检测与成本无关（确定性偏移只稀释判别力）；net 定价
            # 由 G1（real net vs 付成本随机选股 null）与 G3（net vs 付成本
            # 地形）承载，两边同付成本、判据公平。
            return self._run_factor(
                "factor.noise_test", str(source),
                {"m": int(_ms.get("tail_noise_m", 12) or 12),
                 "base_seed": _seed_ns, "statistic": "spread"},
                _sub_env)

        _gates["spread"] = _gate_spread_noise
        # G1 placebo：缓存读前置（命中零成本），未命中才进池
        _placebo_gate_z: float | None = None
        _placebo_gate_m: int | None = None
        _tpm = int(_ms.get("tail_placebo_m", 120) or 0)
        if _tpm > 0 and source and source_hash:
            _KF = _sub_env.calibration.tail_k  # 2026-08-30 可配化：随环境口径
            _tp_budget = float(_ms.get("tail_placebo_budget_secs", 180) or 180)
            _tp_draws = max(60, _tpm)
            _tp_h = int(hor_tb) if hor_tb is not None else None
            _cached = self._placebo_cache_read(
                _sub_env_id, source_hash, _tp_h, _KF, _seed_ns, _tp_draws)
            if _cached is not None:
                _placebo_gate_z = _cached.get("z")
                _placebo_gate_m = _cached.get("draws")
            else:
                def _gate_placebo():
                    return self._run_factor(
                        "factor.tail_placebo", str(source),
                        {"draws": _tp_draws, "seed": _seed_ns,
                         "horizon": _tp_h, "budget_secs": _tp_budget},
                        _sub_env)
                _gates["placebo"] = _gate_placebo

        _futs = self._run_gates_parallel(_gates)

        noise_report = None
        try:
            noise_report = _futs["noise"].result()
        except Exception as e:
            raise BridgeError(
                -32003,
                f"噪声硬门执行失败（事务中止，registry 未写入，可直接重试）："
                f"{type(e).__name__}: {e}。慢因子可先 factor_noise_test(m=20) "
                "自测（引擎有预算自适应），或优化因子计算（向量化）")
        if noise_report is not None and noise_report.get("artifact") is None:
            raise BridgeError(
                -32003,
                f"噪声硬门无法判定（有效世界不足 "
                f"{noise_report.get('n_valid', 0)}/{noise_report.get('m')}，"
                "事务中止未落盘）——因子输出覆盖异常（全 NaN/形状错），"
                "先修因子再提交")
        if noise_report is not None and noise_report.get("artifact"):
            # 实质性判定：artifact 确认 → 拒绝条目落盘（这是因子 verdict，
            # 不是基础设施失败——烧名是正确语义）
            accepted = False
            reason = (f"噪声硬门拒收：因子在 {noise_report['n_valid']} 个噪声"
                      f"世界仍系统性显著（z={noise_report['z']:+.2f}，"
                      f"mean IC_IR={noise_report['mean']:+.3f}）——真 alpha "
                      "按构造不可预测噪声，这指示因子在拟合评价管道 "
                      "artifact 而非数据结构。排查：前视构造/重叠窗口/掩码泄漏")
        # 日期置换 null（2026-08-25 用户决策）：真实结构上的时序置换——
        # report-only。基础设施失败 = 事务中止（raise 不落盘，同噪声门
        # 语义）；alignment_dependent 仅标注不拒收：p 小 = 真短周期因子
        # 与时序性过拟合的并集（样本内不可分），拒收方向与阈值由
        # Phase 6 校准（registry × 证伪 preset 混淆矩阵）后启用。
        dayperm_report = None
        try:
            dayperm_report = _futs["dayperm"].result()
        except Exception as e:
            raise BridgeError(
                -32003,
                f"日期置换测试执行失败（事务中止，registry 未写入，可直接"
                f"重试）：{type(e).__name__}: {e}")
        # 参数平坦性（2026-08-25 用户决策）：申报制最小步长邻域——submit
        # 时引擎自主执行，无独立工具通道（agent 不能预收割邻域再挑峰值
        # 提交）。悬崖签名（翻号/塌陷/退化）= 硬拒收（Phase 6 校准 0/19
        # 假阳性后已启用，见下方 cliff 分支）；申报不实（value 不在
        # source）= 事务中止拒收。
        flatness_report = None
        if fp_decl is not None:
            try:
                flatness_report = _futs["flatness"].result()
            except BridgeError:
                raise
            except Exception as e:
                raise BridgeError(
                    -32003,
                    f"平坦性评估执行失败（事务中止，registry 未写入，可"
                    f"直接重试）：{type(e).__name__}: {e}")
        else:
            flatness_report = {
                "n_params": 0,
                "note": ("未申报参数——跳过平坦性检查（申报制边界：无通道"
                         "强制申报；SKILL 纪律要求申报全部可调参数，trail "
                         "审计抽查）")}
        # 平坦性硬门（Phase 6 校准后启用，2026-08-25）：registry 19 个
        # 已入册因子（amihud 窗口/阈值/复合权重族）悬崖签名 **0/19 假
        # 阳性**——门可安全启用；检测功效待证伪 preset 校准补证。
        # day-perm 维持 report-only：校准实证 19/19 好因子全部
        # alignment_dependent（p 小=真短周期因子与时序过拟合的并集，
        # 方向不可判）——永久作分解诊断，不做拒收门。
        if isinstance(flatness_report, dict) and flatness_report.get("cliff"):
            cliff_rows = [r for r in flatness_report.get("neighbors", [])
                          if r.get("cliff")]
            kinds = sorted({r["cliff"] for r in cliff_rows})
            accepted = False
            reason = ("[平坦性] 悬崖签名拒收：最小步长邻域出现 "
                      f"{kinds}（真实 alpha 在参数空间是平滑峰——动量 "
                      "horizon 谱系结构在大步长，最小步长不该有断崖；"
                      "断崖 = 调参贴噪声）。换非刀锋构造后重新提交")
        # 双轨判定（2026-08-25 用户决策：registry 标注 ic/tail/双入选）：
        # submit 时**两轨都判**——admit_basis 决定哪轨管 acceptance，
        # tracks + dual_pass 是完整标注（audit 实证双过仅 ~4/19，
        # 是有区分度的信号质量维度）。
        # IC 轨判定快照（此刻 accepted/reason 已含 deflation/red_flags/
        # 噪声门/平坦性硬门全部 ic 链门）
        ic_verdict = {"accepted": bool(accepted),
                      "reason": str(reason)[:300]}
        # 尾轨判定（两轨提交都跑——dual_pass 标注需要；G1/G3 用的
        # spread_ir/placebo_z 在 evaluate 已自动算好，只补跑 G2）
        engine_trail = self._read_engine_trail()
        # 尾块按 (source_hash, horizon) 匹配（2026-08-25 修正：此前只比
        # hash——horizon scan 后提交会拿错另一 horizon 的尾块，而 tail
        # 账本键本身含 horizon，判定块与账本键口径必须一致）+
        # stage 排除（WS3 防线 b：test 区 spread_ir 绝不可用于 G3 准入
        # ——泄漏）
        tail_block = None
        for e in reversed(engine_trail):
            if (isinstance(e, dict)
                    and e.get("source_hash") == source_hash
                    and e.get("stage") != "test"
                    and isinstance(e.get("tail"), dict)
                    and not e["tail"].get("error")):
                eh = e.get("horizon")
                if hor_tb is None or eh is None or int(eh) == int(hor_tb):
                    tail_block = e["tail"]
                    break
        try:
            spread_noise = _futs["spread"].result()
        except Exception as e:
            raise BridgeError(
                -32003,
                f"spread 噪声门执行失败（事务中止，可直接重试）："
                f"{type(e).__name__}: {e}")
        # G2 fail-closed（2026-08-25 修正）：无法判定（artifact=None——
        # 有效世界不足/零离散）此前被 tailgate 静默放行，与 IC 轨噪声门
        # 「无法判定 = 事务中止」相反。tail 轨准入不得经无法判定的门 →
        # 事务中止（不烧名）；ic 轨提交只做标注 → 交 tailgate 保守判
        # 「不判过」（G2 无法判定 → tracks.tail.accepted=False）
        if (isinstance(spread_noise, dict)
                and spread_noise.get("artifact") is None
                and admit_basis == "tail"):
            raise BridgeError(
                -32003,
                f"spread 噪声门无法判定（有效世界不足 "
                f"{spread_noise.get('n_valid', 0)}/{spread_noise.get('m')}"
                "，事务中止未落盘）——尾轨准入不得经无法判定的门。"
                "先 factor_noise_test 自查因子在噪声世界的行为")
        _z_spread = spread_noise.get("z") if isinstance(spread_noise, dict) else None
        if (isinstance(_z_spread, float)
                and not math.isfinite(_z_spread)):
            _z_spread = None  # NaN → 交给 tailgate 的 G2 无法判定分支
        # WS1（2026-08-25）：G3 的 s0 从 landscape spread 段取经验值
        # （指纹门内），无/不匹配 → None → bar 降级解析式（s0_source 标注）
        _s0_emp = self._landscape_tail_s0(
            params.get("envId", "primary"), hor_tb)
        # WS2（2026-08-25）：G1 权威 placebo 重算——evaluate 轻量 null
        # （生产面板 ≈10 draws，σ 相对误差 ~24%）不可作硬判据，门的
        # 重判定在 submit 用足样本：R = max(60, tail_placebo_m 默认 120)，
        # 预算自适应（tail_placebo_budget_secs 默认 180s，超预算且 ≥60
        # 已跑即截断、如实报 draws）。种子 = 环境指纹派生（对齐噪声门/
        # day-perm）。tail_placebo_m=0 = 逃生门：不重算，直接用 evaluate
        # 轻量值（degraded 标注）。基础设施失败（编译/超时）：tail 轨 =
        # 事务中止（对齐 G2 spread 门 b670a65）；ic 轨 = 降级轻量值
        # （标注只作 tracks.tail，不拦主判定）。
        _placebo_ran = "placebo" in _futs
        if _placebo_ran:
            try:
                _rep = _futs["placebo"].result()
            except Exception as e:
                if admit_basis == "tail":
                    raise BridgeError(
                        -32003,
                        f"G1 权威 placebo 执行失败（事务中止，可直接"
                        f"重试）：{type(e).__name__}: {e}。可先 "
                        "factor_noise_test 自测因子耗时，或设置 "
                        "mining_state.tail_placebo_m=0 走轻量逃生门")
                _rep = None
                try:
                    import sys as _sys
                    _sys.stderr.write(
                        f"[tail_placebo] ic 轨降级轻量值（执行失败）: "
                        f"{type(e).__name__}: {e}\n")
                except Exception:
                    pass
            if isinstance(_rep, dict):
                _gz = _rep.get("z")
                if isinstance(_gz, (int, float)) and math.isfinite(_gz):
                    _placebo_gate_z = float(_gz)
                    _placebo_gate_m = _rep.get("draws")
                    self._placebo_cache_write(
                        params.get("envId", "primary"), source_hash,
                        _tp_h, _KF, _seed_ns,
                        int(_rep.get("draws") or 0),
                        {k: _rep.get(k) for k in
                         ("z", "draws", "null_mean", "null_std",
                          "periods", "truncated")})
        from .factor.tailgate import tail_admission, tail_ledger
        tail_decision = tail_admission(
            tail_block, tail_ledger(engine_trail), _z_spread, s0_emp=_s0_emp,
            placebo_z_gate=_placebo_gate_z, placebo_m=_placebo_gate_m,
            net_basis=_net_basis)
        # 尾块反查失败的可行动信息（2026-08-26 rank_persistence_w30 事故：
        # agent 提交的 source 与评估时字节不一致 → hash 漂移 → trail 查无
        # 此 hash → 「tail 块缺失」烧名。原始 reason 不含 hash 与修法指引，
        # agent 只能瞎猜（实测连续两次掉同一坑））
        if tail_block is None and source_hash:
            tail_decision["reason"] = (
                f"tail 块缺失：提交 source 的 hash {source_hash[:12]} 在 "
                "trail_engine 无评估记录——提交的 source 与 factor_evaluate "
                "时用的字节不一致（哪怕空格/换行/注释），或从未评估过这份"
                "source。修法：用与 evaluate 完全相同的 source 字符串先 "
                "evaluate 再原样提交（source 与 diagnosis 都不要重新输入）")
        tail_verdict = {
            "accepted": bool(tail_decision["accepted"]),
            "reason": str(tail_decision["reason"])[:300],
            "diag": tail_decision.get("diag"),
            "spread_noise": ({"z": spread_noise.get("z"),
                              "n_valid": spread_noise.get("n_valid")}
                             if isinstance(spread_noise, dict) else None),
        }
        dual_pass = bool(ic_verdict["accepted"] and tail_verdict["accepted"])
        tail_track = None
        if admit_basis == "tail":
            # 兼容字段：tail 轨提交时保留原 tail_track 结构
            tail_track = {"admit_basis": "tail",
                          **tail_verdict,
                          "ic_track": ic_verdict}
            accepted = tail_verdict["accepted"]
            reason = f"[tail轨] {tail_decision['reason']}"
        # 拒绝路径战略菜单（2026-08-21）：submit 拒绝是 agent 停止倾向最强
        # 的时刻（实测："等待用户决定是否清 trail 重置后重新注册"——等
        # 用户批准假门）。菜单把合法出路与禁止路径都写明，loop.state
        # 保持 running 强制继续。门参数随附（bar_sigma/pool_std/所需
        # IC_IR 水平），agent 不必再反推。
        next_moves = None
        if not accepted:
            dp_now = diagnosis.get("deflated_train") or {}
            bar_now = dp_now.get("bar_sigma")
            pool_now = dp_now.get("pool_std")
            need_ir = None
            if isinstance(bar_now, (int, float)) and isinstance(
                    pool_now, (int, float)) and pool_now > 0:
                # p=0.05 门槛反解所需 |IC_IR|（t≈1.645 处，偏度峰度修正略计）
                n_now = dp_now.get("n_obs") or 58
                need_ir = round(1.645 / max(n_now - 1, 1) ** 0.5
                                + bar_now * pool_now, 3)
            next_moves = {
                "options": [
                    "结构性新假设：换信息源/算子族（当前门下需 "
                    f"|IC_IR| ≳ {need_ir if need_ir is not None else 'bar×pool_std+1.645/√n'}）",
                    "正交组合构造：与 registry/library 已入册维度交互"
                    "（目标截面相关 < 0.7 的新组合）",
                    "继续探索其他维度（接受 bar 继续抬升的成本）",
                ],
                "forbidden": [
                    "state.reset 洗 trail 后重注册同一因子（假门——搜索史不可撤销）",
                    "停下来问用户怎么办（loop.state=running，继续内循环）",
                    "穷尽宣告（「因子空间已穷尽/天花板/建议换池子」）——停点仅由"
                    "引擎机械判据触发（轮次/试验上限、IC_IR 收敛、finalize）；"
                    "数据集/池/频率轮换是用户操作，只能随里程碑汇报上报，"
                    "不是 agent 的停笔理由",
                ],
            }
        # 拒绝分类（2026-08-26）：程序性（未评审——尾块反查失败/基础设施）
        # vs 实质性（门真判了）——铁律据此决定重提交时治愈还是烧名
        _rk = None
        if not accepted:
            _rk = ("procedural"
                   if any(m in str(reason) for m in _PROCEDURAL_REJECT_MARKS)
                   else "substantive")
        _pf = diagnosis.get("perf") if isinstance(diagnosis.get("perf"), dict) else {}
        _entry_cpu_s = _pf.get("cpu_s") if isinstance(_pf.get("cpu_s"), (int, float)) else None
        _entry_cpu_rel = None
        if _entry_cpu_s is not None and _entry_cpu_s > 0:
            try:
                from .factor.calib import ref_cpu_seconds
                _ref = ref_cpu_seconds(self.state_root)
                if _ref:
                    _entry_cpu_rel = round(float(_entry_cpu_s) / _ref, 2)
            except Exception:
                _entry_cpu_rel = None
        # 正交门（2026-09-06，设计见 state.py MINING_CONFIG 注释与
        # book_ranks.py）：显著性门回答"它自己是否真实"，本门回答"账本
        # 是否已知"。仅对将通过验收的候选执行（未过验收者不入册，无冗余
        # 可言）；基础设施失败 → 跳过不拒绝不烧名（book.backfill 重建）。
        ortho_report = None
        cand_u16 = None
        cand_dates = None
        if accepted and source_hash and MINING_CONFIG.get("orthogonality_gate", True):
            try:
                from .book_ranks import dev_rank_u16, load_book_cache, check_candidate
                _ortho_env = self._require_panel_env(params.get("envId", "primary"))
                ns = {"env": _ortho_env}
                exec(source, ns)
                F_cand = np.asarray(ns["factor"](_ortho_env), dtype=np.float64)
                cand_u16, cand_dates = dev_rank_u16(
                    _ortho_env, F_cand, _ortho_env.calibration.dev_end)
                cache = load_book_cache(self.state_root)
                if not cache or not cache.get("names"):
                    ortho_report = {"status": "skipped",
                                    "why": "book cache empty——先运行 book.backfill 建立在册秩基线"}
                else:
                    ortho_report = check_candidate(
                        cache, cand_u16,
                        reject_th=float(MINING_CONFIG.get("ortho_reject_th", 0.80)),
                        annotate_th=float(MINING_CONFIG.get("ortho_annotate_th", 0.60)))
                    # 正交门降级为提示（2026-09-10 用户决策）：冗余度不再否决
                    # 入册——深挖式重组（绑已知强腿过 deflation 墙）与正交硬拒
                    # 结构性冲突，前者恰是新域唯一现实入口。缺省 soft；置
                    # ortho_hard_gate=True 可恢复旧硬拒行为。
                    # 2026-09-15 两树收敛注记：此块 09-10 只落主树，worktree
                    # 昨夜以硬门运行并烧名一次——用户复裁恢复软门后自主树
                    # 移植归位。
                    if (ortho_report.get("action") == "reject"
                            and not MINING_CONFIG.get("ortho_hard_gate", False)):
                        ortho_report["action"] = "annotate"
                        ortho_report["hint"] = (
                            "冗余度超 reject_th——按 2026-09-10 决策降级为提示，"
                            "提交照常入册；test 区为最终仲裁")
                    if ortho_report.get("action") == "reject":
                        accepted = False
                        _rk = "substantive"
                        reason = (f"正交门拒收：与在册「{ortho_report['worst_name']}」"
                                  f"|ρ̄|={ortho_report['max_corr']:.2f} 同源"
                                  "（冗余非无效——账本变更后可依程序重议）")
                        next_moves = [
                            {"move": "structural_new_hypothesis",
                             "detail": (f"与在册 {ortho_report['worst_name']} 同源——"
                                        "构造正交新方向；确系同源变体时在 signal 中显式声明关系")},
                        ]
            except Exception as e:
                ortho_report = {"status": "skipped",
                                "why": ("ortho check infra failure（不拒绝不烧名）："
                                        f"{type(e).__name__}: {e}")[:220]}
        entry = {
            "name": name,
            # ts（2026-08-27 WS-A）：紧凑投影的时间字段——旧条目无此键，
            # 投影侧容忍 None（顺序即时间序）
            "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
            "signal": signal,
            "accepted": accepted,
            "reason": reason,
            "reject_kind": _rk,
            "ic_ir_train": diagnosis.get("ic_ir_train"),
            "verified": True if verified else False,
            "diagnosis": diagnosis,
            "noise_gate": noise_report,
            "day_perm": dayperm_report,
            "flatness": flatness_report,
            "admit_basis": admit_basis,
            "tail_track": tail_track,
            # 双轨完整标注（2026-08-25）：tracks = 两轨各自判定，
            # dual_pass = 双过（IC 强 + top-N 变现都过门——audit 实证
            # 仅 ~4/19，有区分度的信号质量维度）
            "tracks": {"ic": ic_verdict, "tail": tail_verdict},
            "dual_pass": dual_pass,
            "source": source,
            "source_hash": source_hash,
            # WS-C 灵感源标注（2026-09-01）：与引擎 trail 同一判定口径
            "origin": self._inspiration_origin(source_hash, current_lane()),
            # 通道⑤ 豁免标注(批次4):OOS 复活证据可审计
            **({"incubated_admitted": incubated_marker}
               if incubated_marker is not None and accepted else {}),
            "fingerprint": (diagnosis.get("_meta") or {}).get("fingerprint")
                           or self._env_full_fingerprint(params.get("envId", "primary")),
            "engine_version": __version__,
            "verdict": diagnosis.get("verdict"),
            # P4 Tier 3：效率元数据——慢因子的税是永久性的（入册后每次
            # 指纹采样/walk-forward 五折/噪声门 ≥10 倍/策略 apply 都重算它）。
            # cpu_rel = cpu_s / 本机参考负载（跨机可比）；无校准时为 null
            "cpu_s": _entry_cpu_s,
            "cpu_rel": _entry_cpu_rel,
        }
        if ortho_report is not None:
            entry["orthogonality"] = ortho_report
        # 终点写（并行 P0）：提交前的长计算（噪声门等）不持锁；落盘时刻
        # 锁内重读 registry——预检查用的是几分钟前的快照，并发会话可能
        # 已写入同名/同源码条目。锁内重跑硬不变量（同源码/同名非程序性
        # 拒绝在场 = 铁律拒绝），治愈逻辑不在此重复（罕见路径，直接拒）。
        with state_write_lock(self.state_root):
            registry = read_registry(self.state_root)
            if source_hash and any(
                    isinstance(e, dict) and e.get("source_hash") == source_hash
                    and not (e.get("accepted") is False and _is_procedural_reject(e))
                    for e in registry):
                raise BridgeError(
                    -32003,
                    "并发提交拦截：该源码已在另一会话登记（铁律：同一因子"
                    "不得重复登记）——用 factor_query_registry 查看后走 update。")
            if any(isinstance(e, dict) and e.get("name") == name
                   and not (e.get("accepted") is False and _is_procedural_reject(e))
                   for e in registry):
                raise BridgeError(
                    -32003,
                    f"并发提交拦截：名字「{name}」已被另一会话占用——"
                    "换名提交或用 factor_registry_update 修正已有条目。")
            registry.append(entry)
            write_registry(registry, self.state_root)
            # 正交门秩缓存追加（2026-09-06）：入册即入账本基线，此后后续
            # 提交的候选会与它做值相关检查。失败不阻断——book.backfill 可
            # 全量重建。
            try:
                if accepted and cand_u16 is not None:
                    from .book_ranks import append_to_cache as _ortho_append
                    if not _ortho_append(self.state_root, name, cand_dates,
                                         cand_u16,
                                         str(entry.get("fingerprint"))):
                        import sys as _sx
                        _sx.stderr.write(
                            "[orthogonality] 秩缓存追加跳过（缺失/日期/指纹"
                            "不符）——运行 book.backfill 重建基线\n")
            except Exception as _exc_o:
                import sys as _sx2
                _sx2.stderr.write(f"[orthogonality] 秩缓存追加失败（不阻断）: "
                                  f"{_exc_o!r}\n")
        # 永久审计账本（v4 2026-08-22）：ledger.json 记录每一次 submit——
        # name/结果/门参数/p，**不随任何 state.reset scope 清除**（只随
        # scope=registry 连带清空）。不做门的输入（层 2 已评审撤回：
        # 「没提交过的族免费」漏洞——batch 6 选 1 的跨族选择只有 trail 能
        # 完整计价），只做取证：reset 洗账后的 p 对账、跨会话审计。
        try:
            import time as _lt
            ledger_path = Path(self.state_root) / "ledger.json"
            with state_write_lock(self.state_root):
                ledger = []
                if ledger_path.exists():
                    try:
                        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
                        if not isinstance(ledger, list):
                            ledger = []
                    except Exception:
                        ledger = []
                dp_ledger = (diagnosis.get("deflated_train") or {})
                ledger.append({
                    "ts": _lt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "name": name, "accepted": bool(accepted),
                    "ic_ir_train": diagnosis.get("ic_ir_train"),
                    "p": dp_ledger.get("p"),
                    "bar_sigma": dp_ledger.get("bar_sigma"),
                    "pool_std": dp_ledger.get("pool_std"),
                    "n_trials": dp_ledger.get("n_trials"),
                    "day_perm_p_align": (dayperm_report or {}).get("p_align"),
                    "alignment_dependent": (dayperm_report or {}).get(
                        "alignment_dependent"),
                    "flatness_cliff": (flatness_report or {}).get("cliff"),
                    "admit_basis": admit_basis,
                    "dual_pass": dual_pass,
                    "tail_placebo_m": _placebo_gate_m,
                    "tail_placebo_z": _placebo_gate_z,
                    "engine_version": __version__,
                    "reason": str(reason)[:200],
                })
                tmp = ledger_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                os.replace(str(tmp), str(ledger_path))
        except Exception as e:
            try:
                import sys as _ls
                _ls.stderr.write(f"[ledger] 写入失败（审计账本不可用，submit 本身成功）: "
                                 f"{type(e).__name__}: {e}\n")
            except Exception:
                pass
        # 自主性停走指令：submit 响应必带（拒绝时刻 agent 最想停）
        try:
            loop = self._loop_directive()
        except Exception:
            loop = None
        result = {"accepted": accepted, "reason": reason, "entry": entry,
                  "receipt_verified": bool(verified),
                  "day_perm": dayperm_report,
                  "flatness": flatness_report,
                  "admit_basis": admit_basis,
                  "tail_track": tail_track,
                  "tracks": {"ic": ic_verdict, "tail": tail_verdict},
                  "dual_pass": dual_pass}
        if loop is not None:
            result["loop"] = loop
        if next_moves is not None:
            result["next_moves"] = next_moves
        return result

    def _book_backfill(self, params):
        """重建在册账本秩缓存（正交门基线，2026-09-06）。

        对 registry 全部 accepted 条目逐只重算 dev 区秩（每只秒级）并落
        book_ranks.npz。缓存过期（新入册未追加/指纹变更）时由提交路径
        提示运行本方法。→ {status, n_factors, n_dates, failed}"""
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        from .book_ranks import rebuild_book_cache
        registry = read_registry(self.state_root)
        entries = [e for e in registry
                   if e.get("accepted") and (e.get("source") or "").strip()]
        fp = self._env_full_fingerprint(self._resolve_env_id(env_id))
        report = rebuild_book_cache(self.state_root, env, entries,
                                    fingerprint=fp)
        if isinstance(report, dict) and report.get("status") == "ok":
            say = getattr(self, "_on_progress", None)
            if say:
                say({"label": "book.backfill",
                     "done": report.get("n_factors"),
                     "total": report.get("n_factors")})
        return report

    # ---- 推理日志（构造面 v7 2026-09-12：双层记忆的解释层，引擎只当机械仓库管理员）----
    def _journal_read(self, params):
        """渐进披露读：level 0=INDEX(L0)/1=live 全文(L1)/2=按 id 拉单条(L2)。"""
        from . import journal as _j
        try:
            lvl = int(params.get("level", 0))
        except (TypeError, ValueError):
            raise BridgeError(-32602, "level 必须是 0|1|2") from None
        try:
            return _j.read_journal(self.state_root, lvl,
                                   params.get("entry_id"))
        except _j.JournalError as e:
            raise BridgeError(-32602, str(e)) from None

    def _journal_append(self, params):
        """追加条目块到 live（首行约定校验：`## <id> <status> <标题>`；
        registered 传 source=因子源码——引擎算 hash 作 ref，评估后镜像
        按它关联；或显式写 `ref:<source_hash>` 行）。"""
        from . import journal as _j
        content = params.get("content")
        if not isinstance(content, str) or not content.strip():
            raise BridgeError(-32602,
                              "content 必填（markdown 条目块，首行 `## <id> "
                              "<status> <标题>`）")
        source = params.get("source")
        if source is not None and not isinstance(source, str):
            raise BridgeError(-32602, "source 须是因子源码字符串（引擎按它计算 ref）")
        try:
            return _j.append_entry(self.state_root, content, source)
        except _j.JournalError as e:
            raise BridgeError(-32602, str(e)) from None

    def _journal_update(self, params):
        """状态迁移/补 note（open→registered→adjudicated→resolved|stale，
        active→retired；同状态调用=补 note 合法）。"""
        from . import journal as _j
        eid = params.get("id")
        if not eid:
            raise BridgeError(-32602, "id 必填（live 区条目 ID，INDEX 里查）")
        try:
            return _j.update_entry(self.state_root, str(eid),
                                   str(params.get("status") or ""),
                                   params.get("note"))
        except _j.JournalError as e:
            raise BridgeError(-32602, str(e)) from None

    def _journal_distill(self, params):
        """弧末蒸馏：旧 live 快照入 raw/（防洗账）→ 终条目归档+墓志 →
        新 live 落盘 → INDEX 重建。遗漏的活条目自动保留并警示。"""
        from . import journal as _j
        new_live = params.get("new_live")
        if not isinstance(new_live, str):
            raise BridgeError(-32602,
                              "new_live 必填（蒸馏后的 live 全文——保留 preamble "
                              "与活条目，终条目会被引擎自动归档）")
        try:
            return _j.distill(self.state_root, new_live)
        except _j.JournalError as e:
            raise BridgeError(-32602, str(e)) from None

    def _standby_view(self, params):
        """候补线视图(批次0,2026-09-13):dev 优异未过线存量的派生只读视图。

        复活通道①③⑤的取材面;agent 无申诉权(复活由机械触发)。
        → {rows[], count_raw, count_family_dedup, meta}"""
        from .standby import standby_view
        try:
            return standby_view(
                self.state_root,
                min_abs_sr=float(params.get("min_abs_sr", 0.75)),
                top_n=int(params.get("top_n", 30)),
                family_dedup=bool(params.get("family_dedup", True)))
        except (TypeError, ValueError):
            raise BridgeError(-32602,
                              "min_abs_sr/top_n 须为数值;family_dedup 布尔") from None

    def _standby_combine(self, params):
        """通道③(批次3):规则化组合——候补池两两|ρ|<0.3 按分数取前 k 等权
        符号对齐;规则作为 dof=1 注册假设登记(journal+台账),组合走标准
        evaluate_composite(异步 job,凭 job_id 轮询)。agent 只触发不选成员。"""
        from .standby import combine as _combine
        rep = _combine(self.state_root,
                       k=int(params.get("k", 4) or 4),
                       ortho_th=float(params.get("ortho_th", 0.3) or 0.3),
                       min_abs_sr=float(params.get("min_abs_sr", 0.75) or 0.75))
        if not rep.get("ok"):
            return rep
        comp_src, comp_hash = rep["composite_source"], rep["composite_hash"]
        # 规则注册:journal registered(dof=1 机制层假设)+台账直接落盘
        # (composite 评估不走 _wrap_diagnosis 镜像,此处是层成员资格的写入点)
        jid = f"C-{comp_hash[:8]}"
        try:
            from . import journal as _j
            _j.append_entry(self.state_root,
                            f"## {jid} registered 规则化组合(通道③)\n"
                            f"预期:组合联合 alpha(成员各自近门,√K 增益过线)",
                            source=comp_src)
        except Exception as _e:
            try:
                sys.stderr.write(f"[standby.combine] journal 注册失败(继续): {_e}\n")
            except Exception:
                pass
        try:
            import ast as _ast
            _tree = _ast.parse(comp_src)
            _ops = sum(1 for _n in _ast.walk(_tree) if isinstance(_n, _ast.Call))
            with state_write_lock(self.state_root):
                with open(Path(self.state_root) / "registered_ledger.jsonl",
                          "a", encoding="utf-8") as _f:
                    _f.write(json.dumps({
                        "ts_registered": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "ts_eval": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "source_hash": comp_hash, "journal_id": jid,
                        "ops": _ops, "depth": 3,
                        "via": "standby.combine",
                        "members": [m["hash"][:12] for m in rep["members"]],
                    }, ensure_ascii=False) + "\n")
        except Exception as _e:
            try:
                sys.stderr.write(f"[standby.combine] 台账落盘失败(继续): {_e}\n")
            except Exception:
                pass
        # 成分源码(按成员 hash 从 sources 库取)
        from .standby import standby_view
        ing = {}
        for m in rep["members"]:
            f = Path(self.state_root) / "sources" / m["hash"][:2] / f"{m['hash']}.py"
            ing[m["hash"][:12]] = f.read_text(encoding="utf-8")
        rep.pop("composite_source", None)   # 全文走返回的评估参数,不双份
        out = self._factor_evaluate_composite({
            "envId": params.get("envId", "primary"),
            "source": comp_src, "ingredients": ing,
            "async": params.get("async", True)})
        if isinstance(out, dict):
            out["standby_combine"] = rep
        return out

    def _incubate_enqueue(self, params):
        """通道⑤ 入队:hashes(cohort 成员,agent 自由选择——dev 选择不污染
        新数据,统计免费;容量 ≤50),window_days 缺省 120。源码冻结。"""
        from . import incubate as _inc
        return _inc.enqueue(self.state_root,
                            params.get("hashes") or [],
                            window_days=int(params.get("window_days", 120) or 120),
                            start_date=params.get("start_date"))

    def _incubate_status(self, params):
        from . import incubate as _inc
        return _inc.status(self.state_root)

    def _incubate_judge(self, params):
        """通道⑤ 判决:需 env 已加载且数据含 start 后 window_days 个新交易
        日。幸存者落 survivors.jsonl;submit 凭 incubated_cohort 豁免 dev
        deflated p(其余门照旧)。窗口未满=not_ready,不消耗判决。"""
        from . import incubate as _inc
        cid = str(params.get("cohort_id") or "")
        if not cid:
            raise BridgeError(-32002, "cohort_id 必填(incubate.status 查)")
        env = self._require_panel_env(params.get("envId", "primary"))
        return _inc.judge(self.state_root, env, cid,
                          compile_fn=self._compile_factor)

    def _journal_stats(self, params):
        """遥测：live 条目计数/登记率(evaluates 中带 registered 预期的比例)/
        蒸馏次数/pull 计数。行为对照（journal 开/关）的数据源。"""
        from . import journal as _j
        d = _j._lane_dir(self.state_root, _j._current_lane())
        if not d.exists():
            return {"exists": False, "hint": "本线日志未开立"}
        counts = _j.rebuild_index(d)["counts"]
        st = _j._read_stats(d)
        return {"exists": True, "counts": counts, "stats": st,
                "registration_rate": (round(st["mirrored"] / st["evals"], 3)
                                      if st.get("evals") else None),
                "live_chars": (_j._live_path(d).stat().st_size
                               if _j._live_path(d).exists() else 0),
                "live_char_cap": _j.LIVE_CHAR_CAP}

    def _registry_update(self, params):
        """修正已入册条目的描述性字段（2026-08-18 换名重登事故的产品化通道）。

        只允许改 signal（描述）与追加 note——source/diagnosis/数字/判定是铁律域：
        换 source = 新因子（走 registry_submit，另起指纹）；改数字 = 编造。
        实测中 agent 想修正描述却只能换名重新 submit，被「同一因子不得重复
        登记」拦截——缺这条正规通道导致的行为漏洞。
        """
        import time as _t

        name = str(params.get("name") or "")
        if not name:
            raise BridgeError(-32602, "registry_update 需要 name（要更新的条目名）")
        # 写锁内读改写（并行 P0）：update 是短计算，整段持锁无饥饿风险
        with state_write_lock(self.state_root):
            registry = read_registry(self.state_root)
            entry = next((e for e in registry if e.get("name") == name), None)
            if entry is None:
                raise BridgeError(-32602, f"registry 中没有名为「{name}」的条目")
            forbidden = [k for k in ("source", "diagnosis", "ic_ir_train", "source_hash",
                                     "fingerprint", "verdict", "accepted", "verified")
                         if k in params]
            if forbidden:
                raise BridgeError(
                    -32003,
                    f"registry_update 不允许修改 {forbidden}——source/diagnosis/数字/判定是"
                    "铁律域：换 source = 新因子（走 registry_submit），改数字 = 编造。"
                    "只允许 signal（新描述）与 note（追加备注）。")
            changed = []
            if params.get("signal"):
                entry["signal"] = str(params["signal"])
                changed.append("signal")
            if params.get("note"):
                notes = entry.get("notes") or []
                notes.append({"ts": _t.strftime("%Y-%m-%d %H:%M:%S"),
                              "note": str(params["note"])[:1000]})
                entry["notes"] = notes
                changed.append("note(append)")
            if not changed:
                raise BridgeError(-32602, "没有可更新字段：传 signal（新描述）或 note（追加备注）")
            write_registry(registry, self.state_root)
        return {"ok": True, "name": name, "changed": changed}

    def _arxiv_search(self, params):
        """F1-F3 修复后的文献检索（2026-08-24 审计产品化）。

        - F2 类目默认：无 category 时注入 q-fin OR 链（杀物理噪声）
        - F3 分层采样：无 start 时拆 relevance 半 + submittedDate 半
          （新旧覆盖）；显式 start 走深部分页模式
        - F1 台账去重：papers.json 记录全部已返回论文；exhausted
          （已被证伪引用）直接排除，已见论文标注 seen_before 并让位
          给未见论文（去重发生在论文层——审计实测查询零重复但
          结果 26% 论文级重叠，查询层去重无效）
        """
        from . import arxiv
        query = str(params.get("query", "")).strip()
        max_results = max(1, min(int(params.get("max_results", 10) or 10), 25))
        category = params.get("category") or None
        # F2：显式 category → 单类目；未传 → q-fin 全类目 OR 链（审计中
        # 28/28 次裸查全库导致 31% 物理噪声——cross-section/momentum 等
        # 金融词与物理词碰撞）
        cat_filter = f"cat:{category}" if category else _QFIN_FILTER
        try:
            start = int(params.get("start", 0) or 0)
        except (TypeError, ValueError):
            start = 0
        if not query:
            return []
        ledger = self._read_papers()
        papers_seen = ledger.get("papers", {})
        exhausted = {pid for pid, e in papers_seen.items()
                     if isinstance(e, dict) and e.get("exhausted")}
        raw_lists = []
        if start > 0:
            raw_lists.append(arxiv.search(
                query, max_results + 5, cat_filter, "relevance", start))
        else:
            half = max(1, max_results // 2)
            raw_lists.append(arxiv.search(
                query, half + 5, cat_filter, "relevance", 0))
            raw_lists.append(arxiv.search(
                query, max_results - half + 5, cat_filter, "submittedDate", 0))
        # 双调用各自可能整失败（[{"error":...}]）——全部失败才直通错误
        merged, errors = [], []
        for lst in raw_lists:
            if isinstance(lst, list):
                for r in lst:
                    if isinstance(r, dict) and "error" in r:
                        errors.append(r)
                    elif isinstance(r, dict):
                        merged.append(r)
        if not merged and errors:
            return errors
        # 论文层去重（跨两次调用 + 跨历史调用）；台账键统一为归一化
        # 裸 id（搜索返回的是完整 URL，trail papers 溯源是裸 id——
        # 两路入口必须在同一键空间）
        by_id, order = {}, []
        for r in merged:
            pid = self._normalize_arxiv_id(str(r.get("arxiv_id") or ""))
            if not pid or pid in exhausted or pid in by_id:
                continue
            by_id[pid] = r
            order.append(pid)
        unseen = [pid for pid in order if pid not in papers_seen]
        seen = [pid for pid in order if pid in papers_seen]
        picked = unseen[:max_results] + seen[:max(0, max_results - len(unseen))]
        out = []
        for pid in picked:
            r = dict(by_id[pid])
            prev = papers_seen.get(pid)
            if prev is not None:
                r["seen_before"] = int(prev.get("times_returned", 1))
            out.append(r)
        # 只记录实际返回给 agent 的（抓到但未返回的不算已见）
        self._record_papers_search(query, out)
        ledger = self._read_papers()
        return {
            "query": query, "results": out,
            "fresh": len(unseen[:max_results]),
            "ledger": {"unique_papers": len(ledger.get("papers", {})),
                       "searches": int(ledger.get("searches", 0)),
                       "exhausted": len([e for e in ledger.get("papers", {}).values()
                                         if isinstance(e, dict) and e.get("exhausted")])},
        }

    def _papers_path(self):
        return Path(self.state_root) / "papers.json"

    def _read_papers(self) -> dict:
        """F1 论文台账：{papers: {id: {title, first_seen, times_returned,
        queries, cited_rounds, exhausted}}, searches: N}。损坏 → 空。"""
        try:
            d = json.loads(self._papers_path().read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("papers"), dict):
                d.setdefault("searches", 0)
                return d
        except Exception:
            pass
        return {"papers": {}, "searches": 0}

    def _write_papers(self, d: dict) -> None:
        p = self._papers_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))

    def _record_papers_search(self, query: str, results: list) -> None:
        d = self._read_papers()
        d["searches"] = int(d.get("searches", 0)) + 1
        for r in results:
            pid = self._normalize_arxiv_id(str(r.get("arxiv_id") or ""))
            if not pid:
                continue
            e = d["papers"].setdefault(pid, {
                "title": r.get("title", ""), "first_seen": d["searches"],
                "times_returned": 0, "queries": [], "cited_rounds": [],
                "exhausted": False})
            e["times_returned"] = int(e.get("times_returned", 0)) + 1
            if r.get("title"):
                e["title"] = r["title"]
            if query not in e["queries"] and len(e["queries"]) < 20:
                e["queries"].append(query)
        self._write_papers(d)

    @staticmethod
    def _normalize_arxiv_id(raw: str) -> str | None:
        """接受裸 id / abs/id / 完整 URL → 归一化 id；非法 → None。"""
        if not isinstance(raw, str):
            return None
        s = raw.strip().rstrip("/")
        m = _ARXIV_ID_RE.search(s)
        return m.group(2) if m else None

    # ---- 结果卡片导出（批次1b）：零计算组装，数据来自引擎 trail / registry / null 地形 ----
    def _report_export(self, params):
        import time as _t

        env_id = params.get("envId", "primary")
        source = str(params.get("source", ""))
        name = str(params.get("name") or "unnamed")
        source_hash = source_fingerprint(source) if source else None

        engine_trail = []
        p = Path(self.state_root) / "trail_engine.json"
        if p.exists():
            try:
                engine_trail = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                engine_trail = []
        entries = [e for e in engine_trail if e.get("source_hash") == source_hash] if source_hash \
            else engine_trail[-1:]
        if not entries:
            return {"ok": False, "error": "该因子源码没有评估记录——先 factor_evaluate 再导出"}
        latest = entries[-1]

        from .factor import random_gen
        landscape = random_gen.read_null_landscape(self.state_root)
        pool_stats = self._pool().stats()

        lines = [
            f"# Factor Report: {name}",
            "",
            f"- Generated: {_t.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- Engine: dsh-factor-mining v{latest.get('engine_version', __version__)}",
            f"- Data fingerprint: `{latest.get('fingerprint')}`",
            f"- Source hash: `{source_hash}`",
            "",
            "## Verdict",
            f"- **{latest.get('verdict', '?')}** (stage: {latest.get('stage')})",
        ]
        if latest.get("red_flags"):
            lines.append("- RED FLAGS:")
            lines.extend(f"  - ⚠ {f}" for f in latest["red_flags"])
        lines += [
            "",
            "## Key numbers",
            f"- IC_IR: {latest.get('ic_ir')}",
            f"- IC mean: {latest.get('ic_mean')}",
        ]
        if landscape:
            # v2：per-horizon 分位（主 horizon 口径的报告；非主 horizon 走查询接口）
            q_main = self._landscape_ic_quantiles(
                landscape, params.get("envId", "primary")) or {}
            p95 = q_main.get("p95")
            lines.append(f"- Null-landscape p95 (random baseline): {p95}"
                         + ("  → **above p95**" if isinstance(latest.get("ic_ir"), (int, float))
                            and isinstance(p95, (int, float)) and latest["ic_ir"] > p95 else ""))
            fp_status = self._landscape_fingerprint_status(
                landscape, params.get("envId", "primary"))
            if fp_status != "match":
                lines.append(f"- ⚠ Null-landscape fingerprint: {fp_status}"
                             "（地形已判无效，重跑 null-calibration 覆盖写）")
        if pool_stats:
            lines += ["", "## Memory pool",
                      f"- active: {pool_stats['active']}/{pool_stats['capacity']['active']}",
                      f"- falsified: {pool_stats['falsified']}/{pool_stats['capacity']['falsified']}"]
        if latest.get("suspects"):
            lines += ["", "## Duplicate suspects"]
            for k in ("duplicate_suspect", "method_suspect"):
                s = (latest["suspects"] or {}).get(k)
                if s:
                    lines.append(f"- {k}: {s.get('note')}")
        if source:
            lines += ["", "## Factor source", "```python", source, "```"]
        content = "\n".join(lines)
        out_dir = Path(self.state_root) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{_t.strftime('%Y%m%d-%H%M%S')}_{name.replace('/', '_')[:40]}.md"
        out_path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(out_path), "content": content}


# ---- 拒绝分类（2026-08-26 rank_persistence_w30 事故产品化） ----
# 程序性拒绝 = 因子从未被真正评审（尾块反查失败/基础设施失败/无法判定）；
# 实质性拒绝 = 门真的判了（deflation/G1-G3/噪声 artifact/平坦性悬崖/receipt
# 编造）。铁律只烧实质性——程序性拒绝的 entry 在重提交时删除放行
# （门全部引擎侧确定性重跑，重试无可翻盘的随机性，放行≠降标准）。
_PROCEDURAL_REJECT_MARKS = ("噪声硬门执行失败", "tail 块缺失")


def _is_procedural_reject(entry) -> bool:
    """entry 的拒绝是否程序性（未评审）。新条目读 reject_kind 字段；
    旧条目（无字段）按 reason 关键词回退——只认引擎写盘的已知模式，
    且限定 reason 前 120 字符防误匹配。"""
    if not isinstance(entry, dict):
        return False
    rk = entry.get("reject_kind")
    if rk == "procedural":
        return True
    if rk == "substantive":
        return False
    r = str(entry.get("reason", ""))[:120]
    return any(m in r for m in _PROCEDURAL_REJECT_MARKS)


def _passes_acceptance(result, z_threshold=3.0, beta_threshold=0.3, min_n=20):
    from .factor.evaluate import passes_acceptance
    return passes_acceptance(result, z_threshold, beta_threshold, min_n)


def _rpc_error(req_id, code, message, data=None):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": _error(code, message, data)},
                      ensure_ascii=False)


def _register_bridge_instance(state_root: str) -> None:
    """实例登记（并行 P0，取代启动独占锁）：同一 stateRoot 允许多个 bridge
    共存（层 2 放行决策）——状态一致性由变更级写锁保证（filelock）。
    本函数只维护报告性的 .bridge-instances.json（pid → 启动时间），
    顺带清掉已死实例；诊断时可知 root 上有几个活跃会话。"""
    p = Path(state_root) / ".bridge-instances.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_write_lock(state_root):
            instances = {}
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    instances = raw
            except Exception:
                instances = {}
            # 清死实例 + 登记 self
            instances = {k: v for k, v in instances.items()
                         if str(k) != str(os.getpid()) and pid_alive(int(k))}
            instances[str(os.getpid())] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(instances, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(str(tmp), str(p))
    except Exception:
        pass  # 登记失败不阻断启动（诊断性文件）


def main(argv=None):
    # stdio 双向 UTF-8（Windows 默认 GBK 会让中文错误信息到达 TS 端时变乱码）。
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="dsh-factor-mining JSON-RPC bridge")
    parser.add_argument("--probe", action="store_true", help="print version and exit")
    parser.add_argument("--state-root", dest="state_root", default=None)
    parser.add_argument("--data-config", dest="data_config", default=None)
    # 进程级默认 lane（2026-08-31 方案 A）：直连 CLI/ZCode 并行会话各起
    # 一个 bridge 进程时用；请求级 lane（dsh web 共享 bridge）优先于此
    parser.add_argument("--lane", dest="lane", default=None,
                        help="process-default parallel-lane id "
                             "(request-level lane takes precedence)")
    args = parser.parse_args(argv)

    if args.probe:
        print(json.dumps({"ok": True, "version": __version__, "schemaVersion": PROTOCOL_SCHEMA_VERSION}))
        return 0

    if args.lane:
        os.environ.setdefault("DSH_FACTOR_MINING_LANE", args.lane)

    bridge = Bridge(state_root=args.state_root, data_config_path=args.data_config)
    _register_bridge_instance(bridge.state_root)
    bridge._on_progress = lambda p: print(
        json.dumps({"jsonrpc": "2.0", "method": "progress", "params": p}, ensure_ascii=False),
        flush=True)
    print(json.dumps({"jsonrpc": "2.0", "method": "ready", "params": bridge._status({})}, ensure_ascii=False),
          flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print(_rpc_error(None, -32700, "Parse error"), flush=True)
            continue
        req_id = msg.get("id")
        if msg.get("method") == "cancel":
            # Cancellation is best-effort in the synchronous prototype.
            continue
        if msg.get("method") is None:
            continue
        try:
            result = bridge.dispatch(msg["method"], msg.get("params") or {})
            print(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": _json_safe(result)}, ensure_ascii=False,
                             default=_json_default), flush=True)
        except BridgeError as e:
            print(_rpc_error(req_id, e.code, e.message, e.data), flush=True)
        except DataError as e:
            # 用户数据/配置错误 → 域错误码（非内部错误），信息可直接呈现给用户
            print(_rpc_error(req_id, -32002, str(e)), flush=True)
        except Exception as e:
            print(_rpc_error(req_id, -32603, f"Internal error: {e}",
                             {"traceback": traceback.format_exc(limit=3)}), flush=True)
    return 0


def _json_default(obj):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def _json_safe(value):
    """Recursively replace non-finite floats with null so JSON.parse never sees NaN."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if hasattr(value, "item"):
        return _json_safe(value.item())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


if __name__ == "__main__":
    sys.exit(main())
