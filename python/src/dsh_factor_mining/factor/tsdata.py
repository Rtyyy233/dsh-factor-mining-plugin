# coding=utf-8
"""期货主力连续日线管道（factor-mining-futures 线，2026-11-07 任务书 §三）。

管道：akshare 独立子进程拉取（硬超时，挂死坑在案）→ 主力判定（OI argmax
机械规则）→ G-roll 增量复权（point-in-time）→ G-limit 数据口径（limit_pct
参数留在 TsEnv/评估层消费）→ 落盘 + 数据指纹 + 机械审计 fail-closed。

G-roll 增量复权（期货第一前视源，任务书 §三.1）：
- 换月日 t（旧 A→新 B）只用**当日** A、B 两合约收盘价：r_t = close_A(t)/close_B(t)。
- 因子逐日累积：cum(t) = prod(r_s : 换月日 s ≤ t)；主力价序列 =
  dominant 当日原始价 × cum(t)。等价叙述：换月时把"新合约价格"折算进
  旧合约的价格水平，序列在换月日的日收益 = 旧合约自身日收益（跳空被抹平，
  段内日收益与原始逐位一致——乘性常数在比值中消去，evaluate_ts.limit_hit_bars
  不会因换月误触发）。
- 该构造只用 date ≤ t 的信息：随机截断日 τ 用仅 date ≤ τ 的数据重建，
  与全量重建在 date ≤ τ 的行上逐位相同（roll_audit 机械复核）。
  全样本（以末段口径）一次性回溯复权 = 前视，审计必抓（fail-closed：
  审计不过 → 不落盘、不写指纹）。

oi/volume 取 dominant 合约**原始值**（不跨合约调整——仓量口径与价格口径
分离是刻意的：复权因子只作用于价格 level，持仓量/成交量的跨合约拼接
没有连续语义）。

库层纪律：本模块无 print——一切异常走 TsDataError，一切统计走返回值。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from queue import Empty as _QueueEmpty
from queue import Queue as _Queue

import numpy as np
import pandas as pd

__all__ = [
    "TsDataError",
    "contract_universe",
    "fetch_one_contract_daily",
    "build_main_series",
    "roll_audit",
    "fetch_and_build",
]


class TsDataError(RuntimeError):
    """期货数据管道错误（拉取失败/格式缺陷/主力断链/复权审计不过）。"""


class _FetchTimeout(TsDataError):
    """拉取超时（独立子进程已被终止）。子类存在的目的：超时=系统性挂死
    信号（akshare 主进程 import 挂死坑在案），fetch_and_build 对它立即
    fail-closed，而单合约的其他失败可跳过记录。"""


#: 同日缓存目录（fetch_one_contract_daily 每合约每天只拉一次）
DEFAULT_CACHE_DIR = Path.home() / ".factor-mining-futures" / "cache"

#: 品种 → 主力月份码表（v1：rb 显式；其余走 _FALLBACK_MONTHS 通用回退）。
#: 上期所 rb 螺纹钢主力月 01/05/10；回退生成 1/5/9/10 全部月码，
#: 多余合约在主力判定中天然出局（OI argmax），宁可多拉不漏。
INSTRUMENT_MONTHS: dict[str, tuple[int, ...]] = {
    "rb": (1, 5, 10),
}
_FALLBACK_MONTHS: tuple[int, ...] = (1, 5, 9, 10)

_PRICE_FIELDS = ("open", "high", "low", "close")

# 子进程拉取源码（symbol 经 argv 传入，绝不进源码字符串）。
# akshare 在主进程 import 会挂死（坑在案）——独立子进程 + 硬超时。
_FETCH_SRC = (
    "import akshare as ak, json, sys\n"
    "df = ak.futures_zh_daily_sina(symbol=sys.argv[1])\n"
    "print(df.to_json(orient='records', force_ascii=False))\n"
)


# ---------------------------------------------------------------------------
# 1.1 单合约拉取（独立子进程 + 硬超时 + 同日缓存）
# ---------------------------------------------------------------------------

def fetch_one_contract_daily(symbol: str, timeout_s: float = 60.0,
                             cache_dir=None) -> list[dict]:
    """拉单合约日线（akshare 新浪接口，独立子进程，硬超时杀进程）。

    返回清洗后的行 dict 列表：{date: "YYYY-MM-DD", open, high, low, close,
    volume, oi}（akshare 的 hold 列=持仓量 → oi；缺 hold/oi → TsDataError）。
    同日缓存：cache_dir 下 <SYMBOL>_<YYYYMMDD>.json，当日已存在直接读。
    """
    symbol = str(symbol).strip()
    cache_root = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    cache_file = cache_root / f"{symbol}_{datetime.date.today():%Y%m%d}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except (OSError, ValueError):
            pass    # 缓存损坏 → 丢弃重拉

    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _FETCH_SRC, symbol],
                capture_output=True, timeout=float(timeout_s), cwd=tmp)
        except subprocess.TimeoutExpired:
            # subprocess.run 超时即 kill 子进程——"独立进程已终止"是事实陈述
            raise _FetchTimeout(
                f"akshare 拉取超时（独立进程已终止）: {symbol}")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise TsDataError(
            f"akshare 拉取失败（{symbol}，退出码 {proc.returncode}）: {err[-300:]}")
    text = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if not text:
        raise TsDataError(f"akshare 拉取返回空输出: {symbol}")
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise TsDataError(f"akshare 输出无法解析为 JSON（{symbol}）: {e}") from e
    rows = _clean_rows(raw, symbol)
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(rows, ensure_ascii=False),
                              encoding="utf-8")
    except OSError:
        pass    # 缓存写失败不阻断拉取结果（best-effort）
    return rows


def _clean_rows(rows, symbol: str) -> list[dict]:
    """akshare 原始行 → 统一 {date,open,high,low,close,volume,oi}。

    对已清洗行（已含 oi、无 hold）幂等。缺 hold/oi 或数值不可转 → TsDataError。
    """
    if not isinstance(rows, list):
        raise TsDataError(
            f"{symbol} 拉取结果不是行列表（收到 {type(rows).__name__}）")
    out: list[dict] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise TsDataError(f"{symbol} 第 {i} 行不是 dict: {r!r}")
        d = r.get("date")
        if d is None or (isinstance(d, float) and math.isnan(d)):
            raise TsDataError(f"{symbol} 第 {i} 行缺 date")
        try:
            ts = (pd.Timestamp(d) if isinstance(d, str)
                  else pd.Timestamp(int(d), unit="ms"))  # to_json 默认 epoch ms
        except (ValueError, TypeError, OverflowError) as e:
            raise TsDataError(f"{symbol} 第 {i} 行 date 无法解析: {d!r}") from e
        row: dict = {"date": str(ts.date())}
        for k in (*_PRICE_FIELDS, "volume"):
            v = r.get(k)
            if v is None:
                raise TsDataError(f"{symbol} 第 {i} 行缺 {k} 列")
            try:
                row[k] = float(v)
            except (TypeError, ValueError) as e:
                raise TsDataError(
                    f"{symbol} 第 {i} 行 {k} 无法转 float: {v!r}") from e
        v = r.get("oi", r.get("hold"))    # akshare: hold=持仓量 → oi
        if v is None:
            raise TsDataError(
                f"{symbol} 第 {i} 行缺 hold 列（持仓量→oi 的唯一来源）")
        try:
            row["oi"] = float(v)
        except (TypeError, ValueError) as e:
            raise TsDataError(f"{symbol} 第 {i} 行 hold/oi 无法转 float: {v!r}") from e
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 1.2 合约宇宙
# ---------------------------------------------------------------------------

def contract_universe(instrument: str, start_year: int, end_year: int) -> list[str]:
    """品种 → 合约代码清单（大写品种前缀 + 4 位 YYMM，如 rb → RB2401）。

    v1：rb 显式月码 (1,5,10)；未知品种走通用回退 (1,5,9,10) 全生成——
    多余合约由 OI 主力判定天然淘汰，宁多勿漏。
    """
    inst = str(instrument).strip().lower()
    if not inst:
        raise TsDataError("instrument 不能为空")
    start_year, end_year = int(start_year), int(end_year)
    if end_year < start_year:
        raise TsDataError(f"end_year {end_year} < start_year {start_year}")
    months = INSTRUMENT_MONTHS.get(inst, _FALLBACK_MONTHS)
    syms = [f"{inst.upper()}{y % 100:02d}{m:02d}"
            for y in range(start_year, end_year + 1) for m in months]
    return sorted(syms)


# ---------------------------------------------------------------------------
# 1.3 主力连续序列构建（纯 pandas/numpy，无 IO）
# ---------------------------------------------------------------------------

def _normalize_contract_frame(df, oi_col: str, sym: str) -> pd.DataFrame:
    """单合约帧规范化：date→datetime64、数值列→float64、排序、去重。"""
    required = ("date", *_PRICE_FIELDS, "volume", oi_col)
    have = list(getattr(df, "columns", []))
    missing = [c for c in required if c not in have]
    if missing:
        raise TsDataError(
            f"合约 {sym} 缺少列 {missing}"
            f"（需要 date/open/high/low/close/volume/{oi_col}）")
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"], errors="coerce"),
        **{k: pd.to_numeric(df[k], errors="coerce").astype(np.float64)
           for k in (*_PRICE_FIELDS, "volume")},
        "oi": pd.to_numeric(df[oi_col], errors="coerce").astype(np.float64),
    })
    out = out.dropna(subset=["date"]).sort_values("date", kind="stable")
    return out.drop_duplicates(subset="date", keep="last").reset_index(drop=True)


def _last_finite_in_col(col: np.ndarray, upto_inclusive: int):
    """col[..upto] 内最后一个有限值（换月日旧合约当日无行时的回退取价）。"""
    i = int(upto_inclusive)
    while i >= 0:
        v = col[i]
        if np.isfinite(v):
            return float(v)
        i -= 1
    return None


def build_main_series(frames: dict[str, pd.DataFrame], oi_col: str = "oi") -> pd.DataFrame:
    """主力连续日线构建 + G-roll 增量复权（point-in-time，纯计算无 IO）。

    输入：{合约代码: 日线 DataFrame(date/open/high/low/close/volume/oi)}，
    对齐到并集日历。

    主力判定（机械规则，任务书 §三.3）：每日 dominant = argmax oi
    （当日无任何有限 oi → 回退 argmax volume；再缺 → 沿用前一日；
    首日即无依据 → TsDataError）。次主力 = 同口径第二大。

    换月日 = dominant 改变的日子。复权见模块 docstring（cum(t) =
    prod(close_旧/close_新 : 换月日 s ≤ t)，价 = 原始价 × cum；
    close2 = 次主力收盘 × 同一 cum，与主连 level 可比）。

    输出列：date, open, high, low, close, volume, oi, close2, dominant, roll。
    """
    if not isinstance(frames, dict) or not frames:
        raise TsDataError("frames 必须是非空 dict[合约 -> 日线 DataFrame]")
    norm: dict[str, pd.DataFrame] = {}
    for sym in sorted(frames):
        df = frames[sym]
        if df is None or len(df) == 0:
            continue
        norm[sym] = _normalize_contract_frame(df, oi_col, sym)
    if not norm:
        raise TsDataError("全部合约数据为空，无法构建主力序列")
    symbols = list(norm)
    K = len(symbols)
    all_dates: set = set()
    for sym in symbols:
        all_dates.update(pd.DatetimeIndex(norm[sym]["date"]))
    calendar = pd.DatetimeIndex(sorted(all_dates))
    T = len(calendar)
    if T == 0:
        raise TsDataError("并集日历为空")

    fields = (*_PRICE_FIELDS, "volume", "oi")
    mats = {k: np.full((T, K), np.nan, dtype=np.float64) for k in fields}
    for j, sym in enumerate(symbols):
        d = norm[sym]
        pos = calendar.get_indexer(pd.DatetimeIndex(d["date"]))
        for k in fields:
            mats[k][pos, j] = d[k].to_numpy(dtype=np.float64)

    # 主力/次主力逐日判定（因果：只用当日截面 oi/volume）
    dom = np.full(T, -1, dtype=np.int64)
    second = np.full(T, -1, dtype=np.int64)
    prev = -1
    for t in range(T):
        basis = None
        oi_row = np.where(np.isfinite(mats["oi"][t]), mats["oi"][t], -np.inf)
        if (oi_row > -np.inf).any():
            basis = oi_row
        else:
            vol_row = np.where(np.isfinite(mats["volume"][t]),
                               mats["volume"][t], -np.inf)
            if (vol_row > -np.inf).any():
                basis = vol_row    # oi 缺失日回退 volume
        if basis is not None:
            order = np.argsort(-basis, kind="stable")
            dom[t] = int(order[0])
            if K > 1 and basis[order[1]] > -np.inf:
                second[t] = int(order[1])
        elif prev >= 0:
            dom[t] = prev          # 再缺 → 沿用前一日（非换月）
        else:
            raise TsDataError(
                f"首日 {calendar[t].date()} 无 oi/volume 可判定主力（且无前日可沿用）")
        prev = int(dom[t])

    # G-roll 增量复权：换月日只用当日新旧两合约收盘（当日无行回退最近有限收盘）
    roll = np.zeros(T, dtype=bool)
    factor = np.ones(T, dtype=np.float64)
    cum = 1.0
    close_m = mats["close"]
    for t in range(1, T):
        if dom[t] != dom[t - 1]:
            roll[t] = True
            a, b = int(dom[t - 1]), int(dom[t])
            ca = _last_finite_in_col(close_m[:, a], t)
            cb = _last_finite_in_col(close_m[:, b], t)
            if ca is None or cb is None or not (ca > 0.0 and cb > 0.0):
                raise TsDataError(
                    f"换月日 {calendar[t].date()}（{symbols[a]}→{symbols[b]}）"
                    "无法取两合约有效收盘，G-roll 复权无法进行")
            cum *= ca / cb
        factor[t] = cum

    rows = np.arange(T)
    dom_idx = dom
    close2_raw = close_m[rows, np.where(second >= 0, second, 0)]
    close2_raw = np.where(second >= 0, close2_raw, np.nan)
    return pd.DataFrame({
        "date": calendar,
        "open": mats["open"][rows, dom_idx] * factor,
        "high": mats["high"][rows, dom_idx] * factor,
        "low": mats["low"][rows, dom_idx] * factor,
        "close": close_m[rows, dom_idx] * factor,
        # oi/volume：dominant 当日原始值（不跨合约调整，见模块 docstring）
        "volume": mats["volume"][rows, dom_idx],
        "oi": mats["oi"][rows, dom_idx],
        "close2": close2_raw * factor,
        "dominant": [symbols[j] for j in dom_idx],
        "roll": roll,
    })


# ---------------------------------------------------------------------------
# 1.4 机械审计：随机截断重建逐位一致
# ---------------------------------------------------------------------------

def _float_cols_bitwise_equal(a, b) -> bool:
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        return False
    both_nan = np.isnan(a) & np.isnan(b)
    return bool(np.all((a == b) | both_nan))


def roll_audit(frames: dict[str, pd.DataFrame], n_checks: int = 6,
               seed: int = 42) -> dict:
    """G-roll point-in-time 机械审计（任务书 §三.1，fail-closed 的依据）。

    随机 n_checks 个截断日 τ（日历 [10%, 90%] 内），用**仅 date ≤ τ 的数据**
    重建 build_main_series，断言与全量重建在 date ≤ τ 的行上逐位相同
    （价格 np 逐位含 NaN 对齐；dominant/roll 相同）。任何一位不同 → ok=False
    （调用方 fetch_and_build 据此拒绝落盘）。

    返回：{"ok": bool, "checks": [{cut, n_rows, bitwise_equal}],
           "n_roll_days": int}。重建异常按该检查失败计（不吞）。
    """
    norm = {s: _normalize_contract_frame(d, "oi", s)
            for s, d in frames.items() if d is not None and len(d)}
    full = build_main_series(norm)
    dates = pd.DatetimeIndex(full["date"])
    T = len(dates)
    lo = max(1, int(T * 0.10))
    hi = max(lo + 1, int(T * 0.90))
    n = max(1, min(int(n_checks), hi - lo))
    rng = np.random.default_rng(seed)
    cut_idx = np.sort(rng.choice(np.arange(lo, hi), size=n, replace=False))
    checks = []
    for ci in cut_idx:
        tau = dates[int(ci)]
        mask = dates <= tau
        try:
            part = build_main_series(
                {s: d[d["date"] <= tau] for s, d in norm.items()})
            sub_full = full.loc[mask]
            price_eq = all(
                _float_cols_bitwise_equal(sub_full[c].to_numpy(dtype=np.float64),
                                          part[c].to_numpy(dtype=np.float64))
                for c in (*_PRICE_FIELDS, "close2"))
            dom_eq = list(sub_full["dominant"]) == list(part["dominant"])
            roll_eq = np.array_equal(sub_full["roll"].to_numpy(dtype=bool),
                                     part["roll"].to_numpy(dtype=bool))
            eq = bool(price_eq and dom_eq and roll_eq)
        except Exception:  # noqa: BLE001 — 重建失败=审计失败
            eq = False
        checks.append({"cut": str(pd.Timestamp(tau).date()),
                       "n_rows": int(mask.sum()), "bitwise_equal": eq})
    return {"ok": all(c["bitwise_equal"] for c in checks), "checks": checks,
            "n_roll_days": int(full["roll"].sum())}


# ---------------------------------------------------------------------------
# 1.5 编排：拉取 → 构建 → 审计 → 落盘 + 指纹（fail-closed）
# ---------------------------------------------------------------------------

def _call_with_timeout(fn, symbol: str, timeout_s: float):
    """对注入实现同样强制硬超时（守护线程 + 队列；超时守护线程自杀不阻塞）。"""
    q: _Queue = _Queue()

    def _run():
        try:
            q.put(("ok", fn(symbol)))
        except BaseException as e:  # noqa: BLE001 — 原样回传给调用方
            q.put(("err", e))

    threading.Thread(target=_run, daemon=True,
                     name=f"tsdata-fetch-{symbol}").start()
    try:
        kind, val = q.get(timeout=max(0.05, float(timeout_s)))
    except _QueueEmpty:
        raise _FetchTimeout(f"akshare 拉取超时（独立进程已终止）: {symbol}")
    if kind == "err":
        raise val
    return val


def fetch_and_build(instrument: str = "rb", start: str = "2014-01-01",
                    end: str | None = None,
                    out_dir=Path.home() / ".factor-mining-futures" / "data",
                    timeout_s: float = 60.0, fetch_one=None) -> dict:
    """全管道编排：合约宇宙 → 逐合约拉取 → [start,end] 过滤 → 主力构建
    → G-roll 审计 → 落盘 + 指纹。

    - fetch_one 可注入替换（缺省 fetch_one_contract_daily；注入实现同样受
      timeout_s 硬超时约束）。
    - 单合约失败：记录跳过（后续主力判定天然绕开它）；全部失败或主力断链
      → TsDataError。超时=系统性挂死信号 → 立即 TsDataError。
    - fail-closed：审计不过 → TsDataError，不落盘、不写指纹。
    - 落盘：<out_dir>/<instrument>_main_daily.parquet；同目录
      tsdata_fingerprint.json = {"sha256"(parquet 字节 sha256 前 16),
      "instrument", "built_at", "source", "rows", "n_roll_days", "audit",
      "columns"}。
    """
    instrument = str(instrument).strip().lower()
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end else pd.Timestamp(datetime.date.today())
    if end_ts < start_ts:
        raise TsDataError(f"end {end_ts.date()} 早于 start {start_ts.date()}")
    universe = contract_universe(instrument, int(start_ts.year), int(end_ts.year))

    if fetch_one is not None:
        fetcher = fetch_one
    else:
        def fetcher(sym: str) -> list[dict]:
            return fetch_one_contract_daily(sym, timeout_s=timeout_s)

    failures: dict[str, str] = {}
    frames: dict[str, pd.DataFrame] = {}
    for sym in universe:
        try:
            rows = _call_with_timeout(fetcher, sym, timeout_s)
            cleaned = _clean_rows(rows, sym)
        except _FetchTimeout:
            raise    # 系统性挂死：立即失败，不逐合约烧超时预算
        except TsDataError as e:
            failures[sym] = str(e)
            continue
        except Exception as e:  # noqa: BLE001 — 单合约失败隔离
            failures[sym] = f"{type(e).__name__}: {e}"
            continue
        if not cleaned:
            continue    # 无数据（未上市/无行情）：非失败
        df = pd.DataFrame(cleaned)
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)]
        if len(df):
            frames[sym] = df
    if not frames:
        raise TsDataError(
            f"{instrument}: 全部合约拉取失败或无数据"
            f"（universe={len(universe)}，failures={failures}）")
    try:
        main = build_main_series(frames)
    except TsDataError as e:
        raise TsDataError(
            f"主力序列构建断链（skipped={sorted(failures)}）: {e}") from e

    audit = roll_audit(frames)
    if not audit.get("ok"):
        raise TsDataError(
            "G-roll 增量复权审计未通过（fail-closed：不落盘、不写指纹）: "
            + json.dumps(audit["checks"], ensure_ascii=False))

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pq_path = out / f"{instrument}_main_daily.parquet"
    main.to_parquet(pq_path, index=False)
    digest = hashlib.sha256(pq_path.read_bytes()).hexdigest()[:16]
    fingerprint = {
        "sha256": digest,
        "instrument": instrument,
        "built_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "akshare-sina",
        "rows": int(len(main)),
        "n_roll_days": int(audit["n_roll_days"]),
        "audit": audit,
        "columns": [str(c) for c in main.columns],
    }
    fp_path = out / "tsdata_fingerprint.json"
    fp_path.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return {
        "path": str(pq_path),
        "rows": int(len(main)),
        "n_roll_days": int(audit["n_roll_days"]),
        "audit": audit,
        "fingerprint_path": str(fp_path),
        "sha256": digest,
        "skipped_contracts": failures,
        "contracts_used": sorted(frames),
    }
