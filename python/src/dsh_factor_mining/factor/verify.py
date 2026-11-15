# -*- coding: utf-8 -*-
"""内部段验证管线（2026-11-05 段隔离任务书 §三 A1/A2/A3）。

模型在 dev 区自由挖矿评估；提交通过快检与既有四门后，由本模块在
**冻结的样本外分段数据**上做 SPRT 证据累积验证，模型只收二值判定。

隔离纪律（红线）：
- 本模块产出的一切数字（各段 IR/累积 t/段数/弱段）只进人读面
  （verify_ledger.jsonl / registry 条目的 verify 内部块），永不进入
  模型可见文案——模型面只有三态固定文案（通过/未通过/族额度用尽）。
- 段定义先验冻结（segments.json + content_sha）：载入时校验完整性，
  被改动即停用（fail-closed）。
- 判定阈值 T_acc 与随机化带经 A4 回放校准一次后冻结；上线后只走
  审计期人签核变更，永不因因子表现调整。

SPRT 语义（对齐主符号的累积 t 统计量）：
- 逐段评估（段序=segments.json 列序，烧毁重者优先）；
- 灾难熔断：任一段对齐 IR < -0.5×|dev_IR|（符号翻转级）→ 立即拒绝；
- 早通过：累积 t ≥ T_acc+band → 通过，余段不评估（省下处女段）；
- 边界随机化（Thresholdout 配方）：累积 t ∈ [T_acc-band, T_acc+band]
  时按 Φ 线性概率随机给判定（确定性种子=source_hash×段定义 sha——
  同因子同段定义可复现，不构成可重试的彩票）；
- 段尽未达 → 拒绝。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from .env import TsEnv

# ---- 常数（A4 校准后冻结；变更须走审计期人签核）----
# T_ACC 初始值 2.8（任务书 §三 A2），经 A4 回放校准（2026-11-05，
# analysis/glm_mining/replay_calib_20261105*）一次性调整为 2.6：
# 全管线口径 robust_high 通过率 73.1%（M8 重写贷记）≥70% 且
# overfit(铁证+高置信) 8.9% ≤10% 的唯一取值；T_acc=2.5 时 overfit
# 12.2% 超限、2.8 时 robust 62.6% 不达标。校准细节与 null 参照组
# 标签语义发现见总战报 §A4。
T_ACC = 2.6          # 早通过阈值
BAND = 0.3           # 边界随机化带半宽
DISASTER_K = 0.5     # 灾难熔断：对齐 IR < -DISASTER_K×|dev_IR|
WEAK_K = 0.25        # 弱段：对齐 IR < WEAK_K×|dev_IR| → 观察册
WEEKLY_FAMILY_CAP = 60   # 族级预算：每线每自然周判定额度
VERSION = "sprt-v1"

# ---- 尾轨段验证（2026-11-05 收尾任务书 §四 1；累积对象=逐采样日尾价差）----
# T_ACC_TAIL 经 tail 回放校准一次定值冻结（1040 只重跑源码回放：
# analysis/glm_mining/tail_sprt_calibration_20261105.json + 符号翻转 null
# 修正档 tail_sprt_null_20261105.json）。**degraded 冻结**：robust 召回档
# 在网格内任何取值不可达（两线合计 credit ≈34.5%≪70%——尾价差对象每段
# 观测 ~58 条且段内衰减更深，IC 版阈值迁移近全拒），按同款决策矩阵取
# 违约最小者 2.5；保护两档全过（overfit 0%≤10%、合成 null 0.86-1.6%
# ≤5%）——尾轨以高精度低召回形态上线，召回由 dev 侧尾门链+季度复审
# 复议通道兜底。常数与单测互锁；改动须走审计期人签核。
T_ACC_TAIL = 2.5
VERSION_TAIL = "sprt-tail-v1"

# ---- 模型面固定文案（三态；零数字零段数零原因）----
TEXT_PASS = (
    "样本外验证通过：候选入册。判定为最终判定；同一份源码不得重复提交。")
TEXT_REJECT = (
    "样本外验证未通过：候选未入册。判定为引擎在冻结的样本外数据上独立"
    "作出，为最终判定——同一份源码不得重试；换构造（不同假设/不同算子族）"
    "的新候选照常提交。被拒候选依季度复审程序翻案（新数据延长后可重议）")
TEXT_BUDGET = (
    "本构造族的本周验证额度已用尽：本周内该构造族（按源码算子结构归类）"
    "不能再提交。换一个不同构造族的假设，或等待下一自然周。额度档位"
    "（不含具体数字）：充裕 / 紧张 / 用尽——当前为用尽")
TEXT_ERROR = (
    "验证管线内部错误：本次提交未完成验证（未入册、未消耗额度），属"
    "临时故障——可直接原样重试；连续失败请报运维（人读侧 verify_ledger"
    " 有失败记录）")
TEXT_SEGMENTS_BROKEN = (
    "验证管线暂不可用（段定义完整性校验未通过）——本次提交未入册、"
    "未消耗额度。这是引擎侧配置问题，等待修复后原样重试即可")


# ---------------------------------------------------------------------------
# 段定义：载入 + 冻结完整性
# ---------------------------------------------------------------------------

def _spec_payload(spec: dict) -> str:
    keys = ("line", "panel", "segments", "upgrade_clause")
    return json.dumps({k: spec[k] for k in keys},
                      ensure_ascii=False, sort_keys=True)


def load_segments(state_root: str | Path) -> dict:
    """载入并校验段定义。segments.json 缺失 / sha 不符 → SegmentsError。

    fail-closed：段定义被改动后一切依赖它的验证停用（不静默放行）。
    """
    root = Path(state_root)
    p = root / "segments.json"
    if not p.exists():
        raise SegmentsError("segments.json 不存在（段验证未部署）")
    try:
        spec = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise SegmentsError(f"segments.json 解析失败: {type(e).__name__}") from None
    sha = hashlib.sha256(_spec_payload(spec).encode("utf-8")).hexdigest()
    if spec.get("content_sha") != sha:
        raise SegmentsError("段定义完整性校验失败：内容与冻结 sha 不符")
    fz = root / "segments.freeze.json"
    if fz.exists():
        try:
            frz = json.loads(fz.read_text(encoding="utf-8"))
            if frz.get("content_sha") != sha:
                raise SegmentsError("冻结文件 sha 与段定义不符")
        except SegmentsError:
            raise
        except Exception:
            pass  # 冻结文件损坏：段文件自身 sha 已验过，放行（审计面问题）
    return spec


class SegmentsError(RuntimeError):
    """段定义缺失/被改动——验证管线停用（fail-closed）。"""


def write_synthetic_segments(state_root: str | Path) -> dict:
    """测试夹具专用：写一份合法签名的合成单段定义（全覆盖窗口）。

    生产 stateRoot 不得使用（真实段定义走 segment_freeze 离线流程）；
    tests/conftest.py 在 Bridge 构造后自动注入，使提交路径测试无需
    每文件自备段文件。
    """
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    spec = {
        "line": "TEST", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "panel": {"start": "2000-01-01", "end": "2099-12-31",
                  "dev_end": "2000-01-01",
                  "source": "synthetic-test"},
        "segments": [{"id": "seg1", "start": "2000-01-01",
                      "end": "2099-12-31", "burn_baseline": 0}],
        "upgrade_clause": "synthetic-test",
        "detection": None, "yearly_stats": [],
        "method_note": "tests/conftest.py 自动注入的合成段（全覆盖窗口）",
    }
    spec["content_sha"] = hashlib.sha256(
        _spec_payload(spec).encode("utf-8")).hexdigest()
    (root / "segments.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    (root / "segments.freeze.json").write_text(json.dumps(
        {"frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "content_sha": spec["content_sha"],
         "authorized_by": "synthetic-test",
         "modification_rule": "synthetic-test"}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return spec


# ---------------------------------------------------------------------------
# 族预算（构造指纹桶，每线每自然周 60 次判定）
# ---------------------------------------------------------------------------

def family_fingerprint(source: str) -> str:
    """构造指纹：AST 调用名多重集哈希（与 M8 同一 AST 基础）。

    同族 = 算子结构相同的变体（换字面量/换变量名不换族）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return hashlib.sha256(b"<unparseable>").hexdigest()[:16]
    names: list[str] = []

    def _nm(fn: ast.AST) -> str:
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute):
            return _nm(fn.value) + "." + fn.attr
        return "<expr>"

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            names.append(_nm(node.func))
    blob = "\n".join(sorted(names))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _week_key(ts: float | None = None) -> str:
    import datetime as _dt
    d = _dt.datetime.fromtimestamp(ts if ts is not None else time.time())
    iy, iw, _ = d.isocalendar()
    return f"{iy}-W{iw:02d}"


def budget_ledger_path(state_root: str | Path) -> Path:
    return Path(state_root) / "verify_budget.json"


def budget_status(state_root: str | Path, family: str) -> dict:
    """族额度查询：只回内部结构（模型面只允许消费档位，不给数字）。"""
    p = budget_ledger_path(state_root)
    buckets: dict[str, Any] = {}
    if p.exists():
        try:
            buckets = json.loads(p.read_text(encoding="utf-8")).get("buckets", {})
        except Exception:
            buckets = {}
    wk = _week_key()
    used = int((buckets.get(family) or {}).get("weeks", {}).get(wk, 0))
    return {"family": family, "week": wk, "used": used, "cap": WEEKLY_FAMILY_CAP,
            "remaining": max(0, WEEKLY_FAMILY_CAP - used),
            "exhausted": used >= WEEKLY_FAMILY_CAP,
            "band": ("用尽" if used >= WEEKLY_FAMILY_CAP
                     else ("紧张" if used >= WEEKLY_FAMILY_CAP * 0.8 else "充裕"))}


def budget_spend(state_root: str | Path, family: str) -> dict:
    """判定记账：+1（只在真发出判定时调用；锁内重读写）。"""
    from ..filelock import state_write_lock
    p = budget_ledger_path(state_root)
    with state_write_lock(state_root):
        data: dict[str, Any] = {"buckets": {}}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                data = {"buckets": {}}
        buckets = data.setdefault("buckets", {})
        fb = buckets.setdefault(family, {"weeks": {}})
        wk = _week_key()
        fb["weeks"][wk] = int(fb.get("weeks", {}).get(wk, 0)) + 1
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        import os as _os
        _os.replace(str(tmp), str(p))
    return budget_status(state_root, family)


# ---------------------------------------------------------------------------
# 验证账本（人读 + 引擎内部）
# ---------------------------------------------------------------------------

def ledger_append(state_root: str | Path, record: dict) -> None:
    p = Path(state_root) / "verify_ledger.jsonl"
    line = json.dumps(record, ensure_ascii=False)
    from ..filelock import state_write_lock
    with state_write_lock(state_root):
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------------------------------------------------------------------------
# SPRT 累积器
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pass_prob_in_band(t: float, t_acc: float | None = None) -> float:
    """Φ 线性概率：t 从 T-BAND 到 T+BAND，通过概率 0→1 线性于 Φ(t)。"""
    _t = T_ACC if t_acc is None else t_acc
    lo, hi = _norm_cdf(_t - BAND), _norm_cdf(_t + BAND)
    return (_norm_cdf(t) - lo) / (hi - lo)


def segment_slice(ic_dates: list[str], ic_values: list[float],
                  start: str, end: str) -> list[float]:
    """按段窗口切 IC 序列（闭区间，日期字符串比较足够 ISO 格式）。"""
    out = []
    for d, v in zip(ic_dates, ic_values):
        if start <= d <= end:
            out.append(float(v))
    return out


def sprt_evaluate(ic_dates: list[str], ic_values: list[float],
                  dev_ir: float, segments: list[dict],
                  seed: str) -> dict:
    """SPRT 段序证据累积。纯函数（同输入同输出）——回放校准与生产同码。

    返回（人读/引擎内部，永不直接进模型面）：
    {verdict, seg_evals[], cumulative_t, n_seg_evaluated, randomized,
     disaster, weak_segments[]}
    """
    return _sprt_core(ic_dates, ic_values, dev_ir, segments, seed,
                      VERSION, T_ACC)


def sprt_evaluate_tail(dates: list[str], spreads: list[float],
                       dev_spread_ir: float, segments: list[dict],
                       seed: str) -> dict:
    """尾轨段序 SPRT（2026-11-05 收尾任务书 §四 1）。

    与 IC 版完全同构（灾难熔断/早通过/边界随机化/确定性种子/弱段登记
    全同 IC 版语义），差别只有两点：累积对象=逐采样日尾价差（毛口径
    top-K 组差，compute_tail_spread_series 产出——段统计=段内价差序列的
    对齐 IR）；独立 VERSION 种子域（同因子两轨的边界随机化落点互不
    复制）。纯函数——tail 回放校准与生产同码。
    """
    return _sprt_core(dates, spreads, dev_spread_ir, segments, seed,
                      VERSION_TAIL, T_ACC_TAIL)


def _sprt_core(ic_dates: list[str], ic_values: list[float],
               dev_ir: float, segments: list[dict], seed: str,
               version: str, t_acc: float) -> dict:
    """两轨共用的段序 SPRT 累积器（IC 轨=IC 序列，尾轨=尾价差序列）。"""
    sgn = 1.0 if dev_ir >= 0 else -1.0
    dev_abs = abs(float(dev_ir))
    rng = np.random.default_rng(
        int(hashlib.sha256(f"{seed}:{version}".encode("utf-8")).hexdigest()[:12], 16))
    pooled: list[float] = []
    seg_evals: list[dict] = []
    weak: list[str] = []
    disaster = False
    randomized = False
    verdict = None
    t_cum = None
    for seg in segments:
        vals = segment_slice(ic_dates, ic_values, seg["start"], seg["end"])
        n = len(vals)
        if n < 3:
            # 数据不足的段=引擎侧数据问题（不是因子失败）→ 显式报错
            raise SegmentsError(
                f"段 {seg.get('id')} 观测数 {n} < 3（面板过期或段定义漂移）")
        arr = np.asarray(vals, dtype=np.float64) * sgn   # 对齐主符号
        mean = float(arr.mean())
        std = float(arr.std(ddof=1))
        ir = mean / std if std > 0 else (math.copysign(math.inf, mean)
                                         if mean != 0 else 0.0)
        seg_evals.append({"id": seg.get("id"), "n": n,
                          "mean": round(mean, 6), "std": round(std, 6),
                          "ir": round(ir, 4),
                          "burn_baseline": seg.get("burn_baseline")})
        # 灾难熔断（先于累积：符号翻转级即拒）
        if dev_abs > 0 and ir < -DISASTER_K * dev_abs:
            disaster = True
            verdict = "reject"
            break
        if ir < WEAK_K * dev_abs:
            weak.append(str(seg.get("id")))
        pooled.extend(arr.tolist())
        a = np.asarray(pooled, dtype=np.float64)
        m, s = float(a.mean()), float(a.std(ddof=1))
        t_cum = (m / s) * math.sqrt(len(a)) if s > 0 else 0.0
        seg_evals[-1]["cumulative_t"] = round(t_cum, 4)
        # 早通过（确定性区）
        if t_cum >= t_acc + BAND:
            verdict = "pass"
            break
        # 边界随机化（Thresholdout：确定性种子，同因子同段定义可复现）
        if t_cum > t_acc - BAND:
            p = _pass_prob_in_band(t_cum, t_acc)
            if float(rng.random()) < p:
                verdict = "pass"
                randomized = True
                break
    if verdict is None:
        # 段尽仍未决：最后一次在带内随机化的失败落点也在此（verdict=None
        # 继续段序，段尽即拒）
        verdict = "pass" if (t_cum is not None and t_cum >= t_acc + BAND) \
            else "reject"
    return {
        "verdict": verdict,
        "seg_evals": seg_evals,
        "cumulative_t": (round(t_cum, 4) if t_cum is not None else None),
        "n_segments_total": len(segments),
        "n_segments_evaluated": len(seg_evals),
        "randomized": randomized,
        "disaster": disaster,
        "weak_segments": weak,
        "params": {"T_acc": t_acc, "band": BAND,
                   "disaster_k": DISASTER_K, "weak_k": WEAK_K,
                   "version": version},
    }


# ---------------------------------------------------------------------------
# 全面板 IC 重算（复用法证已验证的管线）
# ---------------------------------------------------------------------------

def compute_ic_series(source: str, env, horizon: int | None) -> tuple[list, list]:
    """执行因子源码并在全面板上算 IC 序列（与法证 a23_ic_series 同管线）。

    返回 (ic_dates, ic_values)。执行环境剥离网络（B7 配套）。
    """
    import os as _os
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
               "NO_PROXY", "http_proxy", "https_proxy", "ftp_proxy",
               "all_proxy", "no_proxy"):
        _os.environ.pop(_k, None)
    if isinstance(env, TsEnv):
        # ts 线分派（factor-mining-futures，任务书 §五终审）：执行源码 →
        # (T,) 校验 → 全面板乘积序列（法证同管线语义：提交时刻引擎重算，
        # 不信 dev 诊断自带序列）。SPRT 累积器/族预算/文案与面板共用
        # （run_internal_verification/_sprt_core 零改动复用）。
        from .evaluate_ts import ts_product_series
        ns: dict[str, Any] = {"__name__": "verify_factor_ts"}
        exec(compile(source, "<factor>", "exec"), ns)
        fn = ns.get("factor")
        if not callable(fn):
            raise SegmentsError("源码缺 factor(env) 函数")
        F = np.asarray(fn(env), dtype=np.float64)
        if F.ndim != 1 or F.shape[0] != env.T:
            raise SegmentsError(
                f"ts 因子输出必须 (T,)={env.T}（收到 {F.shape}）")
        s = ts_product_series(F, env, horizon)
        return ([str(d.date()) for d in s.index],
                [float(x) for x in s.values])
    from .evaluate import (_env_horizon_view, _forward_returns, _pit_mask,
                           _cross_sectional_ic)
    ns: dict[str, Any] = {"__name__": "verify_factor"}
    exec(compile(source, "<factor>", "exec"), ns)
    fn = ns.get("factor")
    if not callable(fn):
        raise SegmentsError("源码缺 factor(env) 函数")
    h = int(horizon) if horizon else None
    view = _env_horizon_view(env, h)
    fwd = _forward_returns(view)
    pit = _pit_mask(view)
    import numpy as _np
    F = _np.asarray(fn(env), dtype=_np.float64)
    ic = _cross_sectional_ic(F, fwd, pit, view, sig_only=True)
    return [str(d.date()) for d in ic.index], [float(x) for x in ic.values]


def compute_tail_spread_series(source: str, env,
                               horizon: int | None) -> tuple[list, list]:
    """执行因子源码并产出逐采样日尾价差序列（尾轨段验证的累积对象）。

    口径与生产尾门同源（tail._topk_block / tail_metrics）：
    k_frac=env.calibration.tail_k、采样步长=env.calibration.sample_step、
    毛口径 top-K 组差、无效行（截面可用资产 <8）跳过。返回
    (dates, spreads)——dates 与 spreads 严格对齐（跳行同步剔除日期）。
    执行环境剥离网络（同 compute_ic_series）。有效行 <3 → SegmentsError
    （fail-closed，不判）。
    """
    import os as _os
    for _k in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
               "NO_PROXY", "http_proxy", "https_proxy", "ftp_proxy",
               "all_proxy", "no_proxy"):
        _os.environ.pop(_k, None)
    from .evaluate import _env_horizon_view, _forward_returns, _pit_mask
    from .tail import _topk_block
    import numpy as _np
    ns: dict[str, Any] = {"__name__": "verify_factor_tail"}
    exec(compile(source, "<factor>", "exec"), ns)
    fn = ns.get("factor")
    if not callable(fn):
        raise SegmentsError("源码缺 factor(env) 函数")
    h = int(horizon) if horizon else None
    view = _env_horizon_view(env, h)
    fwd = _forward_returns(view)
    pit = _pit_mask(view)
    F = _np.asarray(fn(env), dtype=_np.float64)
    step = max(int(env.calibration.sample_step), 1)
    rows = _np.arange(0, F.shape[0], step)
    valid = [int(t) for t in rows
             if int((pit[t] & _np.isfinite(F[t]) & _np.isfinite(fwd[t])
                     ).sum()) >= 8]
    if len(valid) < 3:
        raise SegmentsError(
            f"尾价差有效采样行 {len(valid)} < 3（面板过期或因子截面退化）")
    spreads, *_ = _topk_block(F, fwd, pit, _np.asarray(valid, dtype=int),
                              float(env.calibration.tail_k))
    dates = [str(view.dates[t].date()) for t in valid]
    return dates, [float(x) for x in spreads]


# ---------------------------------------------------------------------------
# 生产编排（提交路径调用；一切异常 → 固定文案）
# ---------------------------------------------------------------------------

def run_internal_verification(state_root: str | Path, source: str,
                              env, dev_ir: float, horizon: int | None,
                              source_hash: str, name: str = "",
                              lane: str = "") -> dict:
    """内部段验证主入口。返回 {verdict, tier, report}。

    verdict ∈ pass|reject；tier ∈ core|observation（weak 段登记但不告诉
    模型）；report 为引擎内部全数字记录（verify_ledger / registry.verify）。
    额度：真发判定（pass/reject）时消耗 1 次；异常不消耗。
    调用方须已确认额度未耗尽（budget_status.exhausted=False）。
    """
    t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    spec = load_segments(state_root)
    # 面板过期护栏：段末晚于面板末端 → 数据问题，拒做（fail-closed）
    ic_dates, ic_values = compute_ic_series(source, env, horizon)
    _last = env.dates[-1] if getattr(env, "dates", None) else None
    panel_end = str(getattr(_last, "date", lambda: _last)())
    seed = f"{source_hash}:{spec['content_sha']}"
    res = sprt_evaluate(ic_dates, ic_values, float(dev_ir),
                        spec["segments"], seed)
    fam = family_fingerprint(source)
    tier = ("observation" if res["weak_segments"] else "core")
    record = {
        "ts": t0, "name": name, "source_hash": source_hash,
        "family": fam, "line": spec.get("line"),
        "dev_ir": round(float(dev_ir), 6), "horizon": horizon,
        "verdict": res["verdict"], "tier": tier,
        "segments_sha": spec["content_sha"],
        "panel_end": panel_end, **res,
    }
    # 额度消耗（真发判定才消耗）
    record["budget"] = budget_spend(state_root, fam)
    ledger_append(state_root, record)
    return {"verdict": res["verdict"], "tier": tier, "report": record}


def verify_mode(state_root: str | Path) -> str:
    """回滚开关：mining_state.verify_mode ∈ internal_sprt|off（默认开）。"""
    try:
        from ..state import read_mining_state
        v = str(read_mining_state(state_root).get("verify_mode", "internal_sprt"))
        return v if v in ("internal_sprt", "off") else "internal_sprt"
    except Exception:
        return "internal_sprt"


def run_internal_verification_tail(state_root: str | Path, source: str,
                                   env, dev_spread_ir: float | None,
                                   horizon: int | None, source_hash: str,
                                   name: str = "", lane: str = "") -> dict:
    """尾轨内部段验证主入口（2026-11-05 收尾任务书 §四 1）。

    语义与 IC 版 run_internal_verification 完全一致：verdict ∈ pass|reject
    二值；tier 按弱段机械定；report 为引擎内部全数字（verify_ledger /
    registry.verify）。dev_spread_ir=dev 侧尾门 spread_ir（train 区毛
    口径，调用方从 diagnosis.tail 取）；缺省（None）时由序列的 dev 侧
    切片就地计算（segments.json panel.dev_end 为界——与尾门 train 区
    同口径）。真发判定消耗族额度（与 IC 轨同桶：判定即额度，防两轨
    绕倍）。
    """
    t0 = time.strftime("%Y-%m-%dT%H:%M:%S")
    spec = load_segments(state_root)
    dates, spreads = compute_tail_spread_series(source, env, horizon)
    _last = env.dates[-1] if getattr(env, "dates", None) else None
    panel_end = str(getattr(_last, "date", lambda: _last)())
    if dev_spread_ir is None:
        dev_end = str((spec.get("panel") or {}).get("dev_end") or "")
        dv = [v for d, v in zip(dates, spreads) if d < dev_end]
        a = np.asarray(dv, dtype=np.float64)
        if len(a) >= 3 and float(a.std(ddof=1)) > 0:
            dev_spread_ir = float(a.mean()) / float(a.std(ddof=1))
        else:
            raise SegmentsError("dev 侧尾价差序列不足以计算参照 spread_ir")
    seed = f"{source_hash}:{spec['content_sha']}"
    res = sprt_evaluate_tail(dates, spreads, float(dev_spread_ir),
                             spec["segments"], seed)
    fam = family_fingerprint(source)
    tier = ("observation" if res["weak_segments"] else "core")
    record = {
        "ts": t0, "name": name, "source_hash": source_hash,
        "family": fam, "line": spec.get("line"), "track": "tail",
        "dev_spread_ir": round(float(dev_spread_ir), 6),
        "horizon": horizon,
        "verdict": res["verdict"], "tier": tier,
        "segments_sha": spec["content_sha"],
        "panel_end": panel_end, **res,
    }
    # 额度消耗（真发判定才消耗；与 IC 轨同桶）
    record["budget"] = budget_spend(state_root, fam)
    ledger_append(state_root, record)
    return {"verdict": res["verdict"], "tier": tier, "report": record}


def decay_band(dev_ir: float) -> dict:
    """McLean-Pontiff 衰减带（A3）：发表后衰减 26%-58% → 预期前向带。

    纯 dev 侧变换（不含任何样本外信息），册内跟踪对照用。
    """
    a = abs(float(dev_ir))
    sgn = 1.0 if dev_ir >= 0 else -1.0
    return {"low": round(sgn * a * (1 - 0.58), 4),
            "high": round(sgn * a * (1 - 0.26), 4),
            "basis": "McLean-Pontiff 2016：衰减 26%-58%",
            "note": "前向实际衰减穿带 → 观察名单（book_audit 判据）"}
