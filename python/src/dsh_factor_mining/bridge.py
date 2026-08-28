# coding=utf-8
"""NDJSON JSON-RPC 2.0 stdio bridge for dsh-factor-mining.

The bridge owns data loading, the persistent environment cache, the user
factor library, and user state.  It never reads or writes files outside:
- user-specified data files (read only)
- user state root (writes)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
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
from .factor.evaluate import (
    _blp_sigma,
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
    factor_source_stats,
    store_factor_source,
    append_search_path,
    append_trail,
    arc_rounds_bump,
    check_termination,
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
    "compose": ("做中阶合成：从 trail 中挑已验证但未入册的单因子，构造"
                "正交组合（目标截面相关 < 0.7），用 factor_evaluate_composite 验证。"),
    "query": ("正交性审计：计算最近入册因子与既有入册因子的截面相关性，"
              "确认是否带来增量信息；发现高相关就记录 explored 并构造去重组合。"),
}


def _pick_strategy(*, stop_kind: str | None = None, streak: int,
                   pending_rejected, frozen: bool, plateau: bool,
                   pass_unadmitted: int, accepted_n: int, agent_rounds: int,
                   n_trials: int, lit_search_count: int = 0,
                   family_marginal: dict | None = None) -> dict | None:
    """下一轮方向类型选择（纯函数，供注入器与 loop 指令共用）。

    停点分流（2026-08-25 arc 化 + 2026-08-26 v8 族收敛）：
    - stop_kind ∈ {finalize, fail_streak} → None（真终态，注入器静默——
      交还用户；convergence 已退役：全局/族内枯竭都只做换向信号）
    - stop_kind == direction_budget（arc/簇上限/族内收敛）→ 强制
      rotate/literature（族边际 <-0.10 用 literature），跳过 R3-R8——
      预算耗尽/族收敛的方向不允许 continue/refine 原地续推；换向断链
      自动重置预算，注入器照常推进
    - stop_kind == None（running）→ 常规优先级

    常规优先级（v6 2026-08-25：家族升级改边际收益驱动，删 streak 绝对阈值；
    2026-08-27 双轨化：R1/R2 与族收敛同口径——**两线都枯竭**才触发，单线
    枯竭只出 escalation（IC 平但尾部有苗头的方向不被强制换向））：
    R1 IC 与尾部线边际都严重枯竭（<-0.10）→ literature
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

    t = None
    if stop_kind == "direction_budget":
        # 方向预算耗尽：族边际决定换向强度；无论边际如何都不允许原地续推
        if _both_exhausted(-0.10):
            t = "literature"
            why = (f"方向段预算耗尽且 IC/尾部线边际均严重枯竭"
                   f"（IC {fam_m:+.3f}"
                   + (f"，尾部 {tail_m:+.3f}" if tail_ok else "，尾部线无数据")
                   + "）——内生假设源枯竭，必须文献注入后换族")
        else:
            t = "rotate"
            why = "方向段预算耗尽（轮次/簇试验上限）——必须换向，断链后预算自动重置"
    elif _both_exhausted(-0.10):
        t = "literature"
        why = (f"IC 与尾部线边际均严重枯竭（IC {fam_m:+.3f}"
               + (f"，尾部 {tail_m:+.3f}" if tail_ok else "，尾部线无数据")
               + "）——内生假设源枯竭，必须文献注入")
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
        key = f"query:{accepted_n}"
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
    if stop_kind == "direction_budget":
        directive = (directive
                     + "（本方向段轮次/簇试验预算已耗尽：新因子必须与当前族"
                       "不同源——构造指纹或 IC 谱不同链；断链后预算自动重置，"
                       "同族参数微调不会重置。）")
    return {"type": t, "why": why, "key": key,
            "directive": directive}


class Bridge:
    def __init__(self, state_root: str | None = None, data_config_path: str | None = None,
                 library_spec: dict[str, Any] | None = None, execution_mode: str = "worker",
                 worker_timeout_ms: int = 300_000):
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
        self._pool_instance = None                                 # MemoryPool 惰性初始化（需要 state_root）
        self._on_progress = None                          # main() 注册：进度 notification 回调
        # 执行安全（DESIGN §10）：默认 worker 子进程隔离；in_process 仅受信调试
        self.execution_mode = execution_mode if execution_mode in ("worker", "in_process") else "worker"
        self.worker_timeout_ms = int(worker_timeout_ms or 300_000)
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
            new_entries = []
            # 断链检测（2026-08-25 arc 化）：新试验不接当前尾部链 = 机械
            # 换向事件 → 方向段轮次（arc_rounds）归零。簇试验计数
            # （cluster_trials=streak）读时计算，断链自然断——两个计数
            # 器在同一事件上重置，判定谓词同为 _entries_linked。
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
                entry = {
                    "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                    "envId": env_id, "source_hash": source_hash, "stage": stage,
                    "horizon": horizon,
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

                entries = [e for e in entries if not _same_trial(e)]
                if entries and isinstance(entries[-1], dict) \
                        and not _entries_linked(entries[-1], entry):
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
                # trail（下次断链或 reset 补——arc 少归一次零只会更保守）
                try:
                    arc_rounds_bump(self.state_root, reset=True)
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
            "arxiv.search": self._arxiv_search,
        }
        fn = table.get(method)
        if fn is None:
            raise BridgeError(-32601, f"Method not found: {method}")
        return fn

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
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

    def _run_worker(self, method: str, source: str, params: dict[str, Any], env) -> Any:
        """在独立 worker 子进程执行用户 factor 代码（隔离 + 超时）。

        序列化 env → npz → 子进程 run_request → 结构化结果回传。
        超时杀进程（Windows 用 taskkill /T 杀进程树）；结果文件解析失败 = INFRASTRUCTURE 错误。
        """
        run_root = Path(self.state_root) / "worker_runs"
        run_root.mkdir(parents=True, exist_ok=True)
        wdir = Path(tempfile.mkdtemp(dir=str(run_root)))
        job = None
        try:
            from .worker import write_env_npz
            npz = wdir / "env.npz"
            write_env_npz(str(npz), env)

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

            # P1/D3：墙钟上限按声明份额伸缩（jobs=DSH_FACTOR_JOBS，默认满核≈base）
            timeout_s = effective_wall_timeout(base_s=self.worker_timeout_ms / 1000.0)
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
                # 错误（W1 2026-08-26）——存活/零写入/根因修法/纪律
                cpu_note = (f"（实测 CPU {verdict['cpu_s']:.1f}s / 墙钟 {timeout_s:.0f}s"
                            f"——{verdict['note']}）" if verdict["ratio"] is not None else "")
                raise BridgeError(
                    -32005,
                    f"worker 超时（>{timeout_s:.0f}s）——{method} 的 worker 子进程已终止"
                    "并清理；bridge 本体未受影响，无需等待恢复，可立即重试。本次调用"
                    "零写入（registry/trail 均未动，不烧名）。最可能根因：factor(env) "
                    f"单次计算超过 {timeout_s:.0f}s，典型是 per-symbol Python 循环；"
                    "修法 = 向量化（df.groupby(\"symbol\") 的 shift/rolling，或 unstack "
                    "到宽表做矩阵运算）。这是基础设施事件，不是对因子/研究方向的判定"
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
            return result["result"]
        finally:
            # P2：释放 job 句柄（job 内已无进程，无副作用；父崩溃时 OS 按
            # KILL_ON_JOB_CLOSE 兜底杀残余）
            detach_worker_limits(job)
            import shutil
            shutil.rmtree(str(wdir), ignore_errors=True)

    def _run_factor(self, method: str, source: str, params: dict[str, Any], env) -> Any:
        """统一执行入口：worker 模式 → 子进程；in_process 模式 → 当前进程（调试用）。

        2026-08-18 修复：evaluate_batch 是多 source 方法（无单个 source 参数），
        入口的条件编译跳过——此前无条件编译空串导致 batch 在两种模式下
        全部失败（agent 三次重试全 ERR，从未有人成功调用过 batch）。
        """
        if self.execution_mode == "worker":
            return self._run_worker(method, source, params, env)
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
                stat = spread_ir_statistic
                # net 基（2026-08-28 WS-T2）：与 worker.run_request 同口径
                if params.get("net_cost") is not None:
                    import functools as _ft
                    stat = _ft.partial(spread_ir_statistic,
                                       cost=float(params["net_cost"]))
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
        3. 包络（σ 单位）：E[max|X|] 在 CRN 下天然单调（superset 逐点
           max ≥ subset——定理）；包络 max(当前值, bar_sigma_at_write 章,
           blp(n_eff_at_write) 旧章换算) 防 rebuild 重估噪声与跨版本回退。

        返回 stats dict：n_trials（试验计数 M，遥测）/ n_eff（谱 M_eff，
        遥测）/ bar_sigma（E[max|X|] σ 单位，**门参数**，含包络）/
        nu（ν 残差方差占比遥测，sampler 缺席时 None）。

        退化兼容：全体无 sketch → R=I → bar = M 个独立试验的双侧选运
        （≠ v2 的计数 M——不同的量：独立 M 个的 E[max|Z|]）；M=1 →
        E|Z|≈0.798（冷启动选运底价：连符号都是选出来的）；旧条目无
        horizon 字段 → 按 main_horizon 归位。
        """
        # 1) 试验集合：(hash, horizon) 去重 + 包络收集（σ 单位）
        trials: dict[tuple, list | None] = {}
        env_sigma = 0.0
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
            v = e.get("bar_sigma_at_write")
            if isinstance(v, (int, float)) and np.isfinite(v):
                env_sigma = max(env_sigma, float(v))
            # v2 旧章（谱 N_eff，无量纲）：B-LP 换算 σ 后参与包络。
            # 单侧 < 同 N 的双侧——诚实 bar 自然支配旧章，无跨版本灌水。
            v2 = e.get("n_eff_at_write")
            if isinstance(v2, (int, float)) and np.isfinite(v2):
                env_sigma = max(env_sigma, _blp_sigma(float(v2)))
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
                    "bar_sigma": float(env_sigma), "nu": None}

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
        bar = max(smp.bar_sigma(), env_sigma)
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

    def _trial_stats(self, source_hash: str | None = None,
                     horizon: int | None = None,
                     env_id: str | None = None) -> tuple[dict, float | None]:
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
        stats = self._n_eff_from_entries(entries, source_hash, horizon,
                                         cross_h_prior=prior, main_horizon=main_h,
                                         sampler=self._luck)
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
        计数口径：round = arc_rounds 当前方向段轮次；rounds_total =
        trail.json 终身叙事轮次（信息性）；n_trials = trail_engine 唯一
        (source_hash, horizon) 终身试验数（只用于 deflation 计价不做停点）；
        cluster_trials = family_streak 同族链长。"""
        agent_trail = read_json_list("trail", self.state_root)
        engine_trail = self._read_engine_trail()
        agent_rounds = len(agent_trail)
        mining = read_mining_state(self.state_root)
        arc = int(mining.get("arc_rounds", 0) or 0)
        n_trials = len({(e.get("source_hash"), e.get("horizon"))
                        for e in engine_trail if isinstance(e, dict)
                        and e.get("source_hash")})
        # 簇 = 尾部同族链（_entries_linked 双信号）；streak 同时是
        # 「当前簇试验数」，簇试验上限停点的计数基础
        streak = self._family_streak(engine_trail)
        effective = {**mining, "n_trials": n_trials,
                     "cluster_trials": streak}
        term = check_termination(effective)
        # v8（2026-08-26 规划书）：全局收敛退役，族内收敛顶上——纯
        # must_rotate 停点（用户钉死）：族收敛永远只是换向信号，不设
        # 静默终态；convergence stop_kind 消失，静默终态只剩
        # finalize/fail_streak（全局枯竭不再自动判定）
        fam_conv = self._family_convergence(engine_trail, mining)
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
        # 停笔宣言不接受回显，降级为 pending_rejected 并点名拒收
        pending = None
        pending_rejected = None
        if agent_trail and isinstance(agent_trail[-1], dict):
            nh = agent_trail[-1].get("next_hypothesis")
            if isinstance(nh, str) and nh.strip():
                hit = _surrender_match(nh)
                if hit:
                    pending_rejected = {"text": nh.strip()[:400], "reason": hit}
                else:
                    pending = nh.strip()[:400]
        # 家族饱和：边际收益驱动（2026-08-25 用户决策：不看链长绝对
        # 阈值，只看族内边际）——仍在改善的族不催（哪怕 100 个试验），
        # 已平台的族催（哪怕只有 5 个）
        fam_marg = self._family_marginal(engine_trail)
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
        else:
            obligation = ((stop_reason or "停点触发")
                          + "。此停点换方向无法解除，注入器已静默——数据集轮换"
                            "是用户操作，向用户上报当前进展即可，不要停下来空等")
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
        pass_n = sum(1 for e in engine_trail if isinstance(e, dict)
                     and e.get("verdict") == "pass")
        try:
            accepted_n = sum(1 for e in read_registry(self.state_root)
                             if isinstance(e, dict) and e.get("accepted"))
        except Exception:
            accepted_n = 0
        lit_search_count = int(self._read_papers().get("searches", 0) or 0)
        strategy = _pick_strategy(
            stop_kind=stop_kind, streak=streak,
            pending_rejected=pending_rejected, frozen=frozen,
            plateau=plateau, pass_unadmitted=max(pass_n - accepted_n, 0),
            accepted_n=accepted_n, agent_rounds=agent_rounds,
            n_trials=n_trials, lit_search_count=lit_search_count,
            family_marginal=fam_marg)
        loop = {
            "state": state,
            "stop_kind": stop_kind,
            "round": arc,
            "max_rounds": int(effective.get("max_rounds",
                                            MINING_CONFIG["max_rounds"])),
            "rounds_total": agent_rounds,
            "n_trials": n_trials,
            "cluster_trials": streak,
            "max_cluster_trials": int(effective.get(
                "max_cluster_trials", MINING_CONFIG["max_cluster_trials"])),
            "family_streak": streak,
            "stop_reason": stop_reason,
            "family_convergence": fam_conv[2],
            "tail": self._tail_telemetry(engine_trail),
            "pending_hypothesis": pending,
            "pending_rejected": pending_rejected,
            "obligation": obligation,
            "escalation": escalation,
            "strategy": strategy,
        }
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
                         "失败的调用零写入、可立即重试"),
            }
        return loop

    def _factor_evaluate(self, params):
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
            result = self._run_factor("factor.evaluate", source, params, env)
            return self._wrap_diagnosis(env_id, source, stage, result)

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
                    profile[str(h)] = self._evaluate_dev(
                        env_id, env, source, source_hash, {**params, "horizon": int(h)})
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
        result = self._run_factor("factor.evaluate", source, params, env)
        return self._wrap_diagnosis(env_id, source, "development", result)

    def _factor_evaluate_composite(self, params):
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        source = params.get("source", "")
        self._enforce_causality(source, env, env_id)
        if params.get("ingredients") is not None:
            params = {**params, "ingredients": _as_dict(params["ingredients"], "ingredients")}
        result = self._run_factor("factor.evaluate_composite", source, params, env)
        return self._wrap_diagnosis(env_id, source, "composite", result)

    def _factor_evaluate_batch(self, params):
        env_id = params.get("envId", "primary")
        env = self._require_panel_env(env_id)
        sources = params.get("sources") or {}
        if params.get("sources") is not None:
            sources = _as_dict(params["sources"], "sources")
            params = {**params, "sources": sources}
        # 批次内每个因子也要过因果（缓存命中时零成本）
        for name, src in sources.items():
            self._enforce_causality(str(src), env, env_id)
        # v2 申报制：整批同一声明 horizon（须 ∈ 菜单；scan 不适用于 batch——
        # 菜单×批次 = 维度爆炸，scan 走 factor.evaluate 单因子路径）
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
        result = self._run_factor("factor.evaluate_batch", params.get("source", ""), params, env)
        # 批次结果逐因子附指纹/对表（轻量：只附 _meta 不重复对表）
        if isinstance(result, dict) and isinstance(result.get("factors"), dict):
            fp = self._env_full_fingerprint(env_id)
            trail_items = []
            for name, diag in result["factors"].items():
                if not isinstance(diag, dict) or diag.get("error"):
                    continue
                diag["_meta"] = {"fingerprint": fp, "engine_version": __version__,
                                 "receipt": self._make_receipt(diag)}
                diag["_receipt"] = diag["_meta"]["receipt"]
                # v5 血缘：批成员也带构造指纹
                diag["_construction_fp"] = _construction_fingerprint(str(src))
                # A1（2026-08-19 堵 batch 绕过）：批次成员与单因子 evaluate 同等
                # 写入 trail_engine——N_eff 计数不因走 batch 通道而漏计。此前
                # batch 成员不写 trail → N 恒不涨 → deflated p 按 N=1 给出，
                # 拿 batch 诊断直接 submit 即绕过 D7 门控（实验证实）。
                # v2：entry 带 horizon（试验单元 (hash, horizon)）。
                src = str(sources.get(name, ""))
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
        env = self._require_panel_env(params.get("envId", "primary"))
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
        # 按 |ic_ir| 排序取 top_k（无 ic_ir 的沉底）
        results.sort(key=lambda r: -(abs(r["light_ic"].get("ic_ir") or 0.0)))
        top = []
        for r in results[:top_k]:
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
        top_tail = []
        for r in tail_ranked[:top_k]:
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
        overlap_idx = sorted({r["index"] for r in results[:top_k]}
                             & {r["index"] for r in top_tail})
        dist = [abs(r["light_ic"].get("ic_ir") or 0.0) for r in results]
        sp_dist = [_sp(r) for r in results if _sp(r) is not None]
        out = {
            "mode": "explore", "n": n, "seed": seed, "seed_note": seed_note, "top_k": top_k,
            "top": top,
            "top_tail": top_tail,
            "top_net": top_net,
            "overlap_indexes": overlap_idx,
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
            # 穷尽宣告拒收（2026-08-24）：停笔宣言混进 next_hypothesis 会被
            # 引擎回显背书成状态（session.jsonl 实测 12/12 假穷尽）——写入口
            # 直接拒绝，让停点定义权留在引擎机械判据手里
            nh = str(entry.get("next_hypothesis", "") or "")
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
            # 方向段轮次计数（2026-08-25 arc 化）：一条叙事 trail = 当前
            # 方向段一轮。上限按 arc 计不按终身计——机械换向（家族链断）
            # 自动归零。计数失败不阻断叙事写入（少计只会更保守）。
            try:
                arc_rounds_bump(self.state_root)
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
        explored = read_json_list("explored", self.state_root)
        mining = read_mining_state(self.state_root)
        # 与 _loop_directive 同口径注入读时计算的计数（2026-08-25 修正：
        # 此前直接传 mining，cluster_trials 从不在盘上 → termination 恒显
        # 示「簇试验 0/200」，与 loop 指令口径不一致）
        streak = self._family_streak(engine_trail)
        n_trials = len({(e.get("source_hash"), e.get("horizon"))
                        for e in engine_trail if isinstance(e, dict)
                        and e.get("source_hash")})
        effective = {**mining, "n_trials": n_trials, "cluster_trials": streak}
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
            "last_engine_trail": [_trail_entry_compact(e) for e in engine_trail[-5:]],
            "evaluations": {"total": len(engine_trail),
                            "unique_sources": len({e.get("source_hash") for e in engine_trail}),
                            "verdict_counts": verdicts},
            # 源码外挂库（2026-08-29）：source_hash 即键，按
            # sources/<hash[:2]>/<hash>.py 取全文
            "source_store": factor_source_stats(self.state_root),
            "agent_rounds": len(agent_trail),
            "explored_count": len(explored),
            "red_flagged": red_flagged[-10:],
            "pool": self._pool().stats(),
            "note": ("引擎层 trail 为 evaluate 自动记录（硬事实，不可瞒报）；"
                     "last_engine_trail 为紧凑投影（每条 ≈150B，防呈现层截断），"
                     "完整条目人读 stateRoot/trail_engine.json；"
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
                   "papers.json", "sources"],
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
        accepted, reason = _passes_acceptance(diagnosis)
        if diagnosis.get("red_flags"):
            accepted = False
            reason = f"red_flags 未清：{diagnosis['red_flags'][:2]}"
        # 噪声硬门（2026-08-24 用户设计决策）：因子在随机噪声世界上的
        # 直接表现。真 alpha 按构造不可预测噪声；噪声上仍显著 = 因子
        # 公式在拟合评价 artifact（管道偏差/重叠窗口构造），无条件拒收。
        # 2026-08-25 pw15_compD_5050 事故修正语义：基础设施失败（超时/
        # worker 崩溃）= **事务中止（raise，不落盘）**——旧版写成
        # accepted=false 条目导致铁律烧名，重试被「不得重复入册」挡死。
        # 只有实质性判定（artifact 确认）才落拒绝条目。
        noise_report = None
        try:
            noise_report = self._factor_noise_test({
                "envId": params.get("envId", "primary"),
                "source": source,
                "m": int(read_mining_state(self.state_root).get(
                    "noise_gate_m", 50) or 50)})
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
            dayperm_report = self._factor_day_perm_test({
                "envId": params.get("envId", "primary"),
                "source": source,
                "m": int(read_mining_state(self.state_root).get(
                    "day_perm_m", 200) or 200)})
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
        fp_decl = params.get("flatness_params")
        if fp_decl is not None:
            self._flatness_decl_check(source, fp_decl)
            try:
                flatness_report = self._run_factor(
                    "factor.flatness_test", str(source),
                    {"flatness": fp_decl},
                    self._require_panel_env(params.get("envId", "primary")))
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
        admit_basis = str(params.get("admit_basis") or "ic")
        if admit_basis not in ("ic", "tail"):
            raise BridgeError(-32602,
                              f"admit_basis 必须是 ic | tail（收到 {admit_basis!r}）")
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
        hor_tb = diagnosis.get("horizon")
        if not isinstance(hor_tb, (int, float)) or int(hor_tb) <= 0:
            hor_tb = None
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
        import hashlib as _hl
        _fp_ns = self._env_full_fingerprint(
            params.get("envId", "primary")) or "nofp"
        _seed_ns = int(_hl.sha256(
            str(_fp_ns).encode("utf-8")).hexdigest()[:8], 16)
        # 换手率定价（2026-08-28 规划书 WS-T2 v1 双报）：判定基开关——
        # false（默认）= G2/G3 判毛口径，net 三件套只陪跑；true = 三门
        # 全 net 口径（G1 本就 net）。可被 mining_state.tail_net_basis 覆盖
        _net_basis = bool(read_mining_state(self.state_root).get(
            "tail_net_basis", MINING_CONFIG["tail_net_basis"]))
        _tail_env = self._require_panel_env(params.get("envId", "primary"))
        try:
            spread_noise = self._run_factor(
                "factor.noise_test", str(source),
                {"m": int(read_mining_state(self.state_root).get(
                    "tail_noise_m", 12) or 12),
                 "base_seed": _seed_ns, "statistic": "spread",
                 "net_cost": (float(_tail_env.calibration.cost)
                              if _net_basis else None)},
                _tail_env)
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
        _tpm = int(read_mining_state(self.state_root).get(
            "tail_placebo_m", 120) or 0)
        _placebo_gate_z: float | None = None
        _placebo_gate_m: int | None = None
        if _tpm > 0 and source and source_hash:
            from .factor.tail import K_FRAC as _KF
            _tp_budget = float(read_mining_state(self.state_root).get(
                "tail_placebo_budget_secs", 180) or 180)
            _tp_draws = max(60, _tpm)
            _tp_h = int(hor_tb) if hor_tb is not None else None
            _cached = self._placebo_cache_read(
                params.get("envId", "primary"), source_hash, _tp_h,
                _KF, _seed_ns, _tp_draws)
            if _cached is not None:
                _placebo_gate_z = _cached.get("z")
                _placebo_gate_m = _cached.get("draws")
            else:
                try:
                    _rep = self._run_factor(
                        "factor.tail_placebo", str(source),
                        {"draws": _tp_draws, "seed": _seed_ns,
                         "horizon": _tp_h, "budget_secs": _tp_budget},
                        self._require_panel_env(
                            params.get("envId", "primary")))
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
    args = parser.parse_args(argv)

    if args.probe:
        print(json.dumps({"ok": True, "version": __version__, "schemaVersion": PROTOCOL_SCHEMA_VERSION}))
        return 0

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
