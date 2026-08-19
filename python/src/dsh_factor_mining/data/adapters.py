# coding=utf-8
"""Normalize user-supplied market data into FactorEnv matrices.

Supported source forms:
- long: one row per (symbol, date)
- wide: one row per date, columns encode (symbol, field)
- per_symbol: one file per symbol (glob)
- multiindex: parquet columns are a (symbol, field) MultiIndex
- minute_features: a precomputed feature long table

The original files are always opened read-only.  Normalization output is
returned in memory; an optional cache may be added by the caller.
"""
from __future__ import annotations

import glob as globlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

REQUIRED_OHLCV = ("open", "high", "low", "close", "volume")
OPTIONAL_FIELDS = ("amount", "listed")


@dataclass
class MappingSpec:
    symbol: str | None = None
    date: str | None = None
    open: str | None = None
    high: str | None = None
    low: str | None = None
    close: str | None = None
    volume: str | None = None
    amount: str | None = None
    listed: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    # wide layout only
    field_position: str = "last"  # "last" | "first" | "pattern"
    symbol_pattern: str | None = None
    field_pattern: str | None = None
    separator: str = "_"


@dataclass
class SourceSpec:
    type: str = "parquet"  # parquet | csv | glob
    path: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class EnvironmentSpec:
    id: str = "primary"
    label: str = ""
    kind: str = "panel"  # panel | minute_features
    source: SourceSpec = field(default_factory=SourceSpec)
    layout: str = "long"  # long | wide | per_symbol | multiindex
    mapping: MappingSpec = field(default_factory=MappingSpec)
    date_format: str | None = None
    frequency: str = "auto"  # auto | daily | minute
    constraints: dict[str, Any] = field(default_factory=lambda: {
        "minSymbols": 20,
        "minDates": 200,
        # 三态 true(strict)/false(off)/"report"（默认 report：个股停牌/退市缺口放行+摘要上报，
        # 与 _validate_matrices 的 .get(..., "report") 行为默认保持同一真相源）
        "requireFiniteOhlcv": "report",
        "allowZeroVolume": True,
    })
    # minute_features: which columns are features
    features: dict[str, str] = field(default_factory=dict)
    # 用户自由文本说明（原样保留，不参与任何逻辑）
    description: str = ""
    # 市场口径（profile 预设 + 字段覆盖；空 dict = 历史默认口径 cn_etf_daily）
    calibration: dict[str, Any] = field(default_factory=dict)


# 结构性市场口径预设：只定 frequency/execution/涨跌停 mask/年化换算；
# 研究性量（horizon/cost_bps/dev_end/sel_end）刻意不预设——避免预设偷偷改变
# 研究口径，全部走 config 显式覆盖（缺省 = 历史默认 H=20 / 10bps / 2021/2024）。
CALIBRATION_PROFILES: dict[str, dict[str, Any]] = {
    "cn_etf_daily":    {"frequency": "daily",  "execution": "t1",
                        "limit_up_down_mask": False, "annualization": 252},
    "cn_stock_daily":  {"frequency": "daily",  "execution": "t1",
                        "limit_up_down_mask": True,  "annualization": 252},
    "cn_etf_minute":   {"frequency": "minute", "execution": "t0",
                        "limit_up_down_mask": False, "bars_per_day": 48,
                        "annualization": 48 * 252},
    "cn_stock_minute": {"frequency": "minute", "execution": "t0",
                        "limit_up_down_mask": True,  "bars_per_day": 48,
                        "annualization": 48 * 252},
}

ALLOWED_CAL_KEYS = {"frequency", "horizon", "horizons", "cost_bps", "execution",
                    "dev_end", "sel_end", "annualization", "limit_up_down_mask",
                    "ic_sample_every", "top_n", "bars_per_day"}


def resolve_calibration(raw: dict[str, Any] | None) -> dict[str, Any]:
    """calibration 段 → 合并后的口径 dict（profile 打底，字段覆盖）。"""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise DataError("calibration 必须是 JSON 对象")
    profile = raw.get("profile")
    merged: dict[str, Any] = {}
    if profile is not None:
        if profile not in CALIBRATION_PROFILES:
            raise DataError(
                f"未知 calibration.profile: {profile}"
                f"（支持: {', '.join(CALIBRATION_PROFILES)}）")
        merged.update(CALIBRATION_PROFILES[profile])
        merged["profile"] = profile
    overrides = {k: v for k, v in raw.items() if k != "profile"}
    unknown = sorted(set(overrides) - ALLOWED_CAL_KEYS)
    if unknown:
        raise DataError(
            f"calibration 未知字段 {unknown}；可用字段: {sorted(ALLOWED_CAL_KEYS)}")
    merged.update(overrides)
    if merged.get("frequency") not in (None, "daily", "minute"):
        raise DataError("calibration.frequency 只支持 daily | minute")
    if merged.get("execution") not in (None, "t1", "t0"):
        raise DataError("calibration.execution 只支持 t1 | t0")
    _validate_calibration_domain(merged, raw)
    return merged


def _validate_calibration_domain(merged: dict[str, Any], raw: dict[str, Any]) -> None:
    """数值域校验（写配置时拒绝，绝不拖到 load/评估时才炸）。

    horizon<=0 会让 fwd 收益语义崩坏；cost_bps<0 等于负成本（垃圾因子净收益全变正）；
    ic_sample_every 处于 (0, horizon) 会产生重叠 IC 窗，绕过「IC_IR 不重叠」铁律。
    dev_end/sel_end 必须成对出现——只填一半会被 auto 静默覆盖，直接拒绝更诚实。
    """
    if "horizon" in merged and merged["horizon"] is not None:
        try:
            h = int(merged["horizon"])
        except (TypeError, ValueError):
            raise DataError(f"calibration.horizon 必须是正整数（收到 {merged['horizon']!r}）")
        if h <= 0:
            raise DataError(f"calibration.horizon 必须 > 0（收到 {h}）")
    if "horizons" in merged and merged["horizons"] is not None:
        # v2（2026-08-20）horizon 菜单：申报制评估的合法赌注集。
        # 约束：正整数、去重、1~6 个（菜单宽度 = 允许的假设维度，防爆炸）、
        # 必须包含主 horizon（主 horizon 是缺省申报）。
        raw_menu = merged["horizons"]
        if not isinstance(raw_menu, (list, tuple)):
            raise DataError(f"calibration.horizons 必须是正整数数组（收到 {raw_menu!r}）")
        try:
            menu = [int(x) for x in raw_menu]
        except (TypeError, ValueError):
            raise DataError(f"calibration.horizons 必须是正整数数组（收到 {raw_menu!r}）")
        if not menu or any(x <= 0 for x in menu):
            raise DataError(f"calibration.horizons 必须非空且全为正整数（收到 {menu}）")
        if len(set(menu)) != len(menu):
            raise DataError(f"calibration.horizons 不得重复（收到 {menu}）")
        if len(menu) > 6:
            raise DataError(f"calibration.horizons 菜单最多 6 个（收到 {len(menu)} 个：{menu}）"
                            "——菜单宽度 = 允许的假设维度，宽菜单 = 宽选择偏差")
        main_h = int(merged.get("horizon") or 20)
        if main_h not in menu:
            raise DataError(
                f"calibration.horizons 必须包含主 horizon {main_h}（收到 {menu}）"
                "——主 horizon 是缺省申报，不在菜单内 = 自相矛盾配置")
    if "cost_bps" in merged and merged["cost_bps"] is not None:
        try:
            c = float(merged["cost_bps"])
        except (TypeError, ValueError):
            raise DataError(f"calibration.cost_bps 必须是非负数（收到 {merged['cost_bps']!r}）")
        if c < 0:
            raise DataError(f"calibration.cost_bps 必须 >= 0（收到 {c}；负成本会让任何垃圾因子变盈利）")
    if "ic_sample_every" in merged and merged["ic_sample_every"] is not None:
        try:
            step = int(merged["ic_sample_every"])
        except (TypeError, ValueError):
            raise DataError(f"calibration.ic_sample_every 必须是整数（收到 {merged['ic_sample_every']!r}）")
        horizon = int(merged.get("horizon") or 20)
        if 0 < step < horizon:
            raise DataError(
                f"calibration.ic_sample_every={step} < horizon={horizon}：重叠 IC 窗会通胀 IC_IR，"
                f"绕过不重叠铁律。用 0（=horizon，不重叠）或 >= horizon。")
    dev_end, sel_end = merged.get("dev_end"), merged.get("sel_end")
    if (dev_end is None) != (sel_end is None):
        missing = "sel_end" if dev_end is not None else "dev_end"
        raise DataError(
            f"calibration.dev_end / sel_end 必须成对指定（当前缺 {missing}）。"
            f"只填一半会被 auto 60/20/20 覆写掉你填的那个——要么都填，要么都不填走 auto。")
    import pandas as _pd
    for key, val in (("dev_end", dev_end), ("sel_end", sel_end)):
        if val is not None:
            try:
                _pd.Timestamp(val)
            except Exception:
                raise DataError(f"calibration.{key} 不是有效日期: {val!r}")
    if dev_end is not None and sel_end is not None:
        if _pd.Timestamp(dev_end) >= _pd.Timestamp(sel_end):
            raise DataError(
                f"calibration.dev_end({dev_end}) 必须 < sel_end({sel_end})：dev→selection→test 依次向后")
    if "bars_per_day" in merged and merged["bars_per_day"] is not None:
        try:
            b = int(merged["bars_per_day"])
        except (TypeError, ValueError):
            raise DataError(f"calibration.bars_per_day 必须是正整数（收到 {merged['bars_per_day']!r}）")
        if b <= 0:
            raise DataError(f"calibration.bars_per_day 必须 > 0（收到 {b}）")
        # 分钟口径：用户没显式给 annualization 时按 bars_per_day×252 派生
        # （防忘设导致全部年化指标错几十倍；不写入配置文件，load 响应报告实际生效值）
        if "annualization" not in raw:
            merged["annualization"] = b * 252


@dataclass
class DataConfig:
    version: int = 1
    stateRoot: str = ""
    environments: dict[str, EnvironmentSpec] = field(default_factory=dict)
    # 顶层因子库段（透传 UserLibrary spec：{type?, path}；type 缺省按后缀推断）
    library: dict[str, Any] = field(default_factory=dict)
    # 预置已证伪库（开源用户带自己的历史轨迹）：只读 JSON，查询 explored 时合并（标 source=preset）
    explored_preset: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "stateRoot": self.stateRoot,
            "environments": {k: _dataclass_to_dict(v) for k, v in self.environments.items()},
            **({"library": self.library} if self.library else {}),
            **({"explored_preset": self.explored_preset} if self.explored_preset else {}),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DataConfig":
        if not isinstance(d, dict):
            raise DataError(f"数据配置必须是 JSON 对象（收到 {type(d).__name__}）")
        # v0 旧格式检测（data_root/file_index 时代）：明确报错引导重配，不做语义迁移。
        if "environments" not in d and ("data_root" in d or "file_index" in d):
            raise DataError(
                "数据配置为旧格式 v0（data_root/file_index），与本版本不兼容；"
                "请用 factor_data_probe + factor_config_write 重新生成 v1 配置")
        raw_envs = d.get("environments")
        if not isinstance(raw_envs, dict) or not raw_envs:
            raise DataError(_CONFIG_SCHEMA_HINT)
        known_keys = {"id", "label", "kind", "source", "layout", "mapping",
                      "dateFormat", "date_format", "frequency", "constraints",
                      "features", "description", "calibration"}
        problems: list[str] = []
        envs: dict[str, EnvironmentSpec] = {}
        for key, raw in raw_envs.items():
            if not isinstance(raw, dict):
                problems.append(f"environments['{key}'] 必须是 JSON 对象（收到 {type(raw).__name__}）")
                continue
            unknown = sorted(set(raw) - known_keys)
            if unknown:
                problems.append(
                    f"environments['{key}'] 含未知字段 {unknown}——数据文件路径放 "
                    "source.path，列映射放 mapping，说明文字放 description")
            src = raw.get("source") or {}
            if not isinstance(src, dict):
                problems.append(f"environments['{key}'].source 必须是 JSON 对象")
                src = {}
            if not str(src.get("path") or "").strip():
                problems.append(f"environments['{key}'].source.path 缺失（数据文件路径）")
            mapping = raw.get("mapping") or {}
            if not isinstance(mapping, dict):
                problems.append(f"environments['{key}'].mapping 必须是 JSON 对象")
                mapping = {}
            if raw.get("kind", "panel") == "panel" and raw.get("layout", "long") == "long":
                for must in ("symbol", "date"):
                    if not str(mapping.get(must) or "").strip():
                        problems.append(f"environments['{key}'].mapping.{must} 缺失（数据中的列名）")
            envs[key] = EnvironmentSpec(
                id=key,
                label=raw.get("label", key),
                kind=raw.get("kind", "panel"),
                source=SourceSpec(
                    type=src.get("type", "parquet"),
                    path=src.get("path", ""),
                    options=src.get("options") or {},
                ),
                layout=raw.get("layout", "long"),
                mapping=_mapping_from_dict(mapping),
                date_format=raw.get("dateFormat") or raw.get("date_format"),
                frequency=raw.get("frequency", "auto"),
                constraints=raw.get("constraints") or {},
                features=raw.get("features") or {},
                description=raw.get("description") or "",
                calibration=resolve_calibration(raw.get("calibration") or {}),
            )
        if problems:
            raise DataError("数据配置结构错误（已拒绝写入，不落盘）:\n- " + "\n- ".join(problems)
                            + "\n" + _CONFIG_SCHEMA_HINT)
        library = d.get("library") or {}
        if not isinstance(library, dict):
            raise DataError("library 必须是 JSON 对象（{type?, path}）")
        if library and not str(library.get("path") or "").strip():
            raise DataError("library.path 缺失（因子库文件路径）")
        preset = d.get("explored_preset") or ""
        if preset and not Path(preset).exists():
            raise DataError(f"explored_preset 文件不存在: {preset}")
        return cls(
            version=int(d.get("version", 1)),
            stateRoot=d.get("stateRoot") or d.get("state_root") or "",
            environments=envs,
            library=library,
            explored_preset=str(preset),
        )

    @classmethod
    def load(cls, path: str | os.PathLike) -> "DataConfig":
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        return cls.from_dict(json.loads(text))

    def save(self, path: str | os.PathLike) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _mapping_from_dict(d: dict[str, Any]) -> MappingSpec:
    return MappingSpec(
        symbol=d.get("symbol"),
        date=d.get("date"),
        open=d.get("open"),
        high=d.get("high"),
        low=d.get("low"),
        close=d.get("close"),
        volume=d.get("volume"),
        amount=d.get("amount"),
        listed=d.get("listed"),
        extra=d.get("extra") or {},
        field_position=d.get("fieldPosition") or d.get("field_position") or "last",
        symbol_pattern=d.get("symbolPattern") or d.get("symbol_pattern"),
        field_pattern=d.get("fieldPattern") or d.get("field_pattern"),
        separator=d.get("separator", "_"),
    )


def _dataclass_to_dict(obj) -> dict[str, Any]:
    if isinstance(obj, MappingSpec):
        return {
            "symbol": obj.symbol, "date": obj.date,
            "open": obj.open, "high": obj.high, "low": obj.low,
            "close": obj.close, "volume": obj.volume,
            "amount": obj.amount, "listed": obj.listed,
            "extra": obj.extra,
            "field_position": obj.field_position,
            "symbol_pattern": obj.symbol_pattern,
            "field_pattern": obj.field_pattern,
            "separator": obj.separator,
        }
    if isinstance(obj, SourceSpec):
        return {"type": obj.type, "path": obj.path, "options": obj.options}
    if isinstance(obj, EnvironmentSpec):
        d = {
            "id": obj.id, "label": obj.label, "kind": obj.kind,
            "source": _dataclass_to_dict(obj.source), "layout": obj.layout,
            "mapping": _dataclass_to_dict(obj.mapping),
            "dateFormat": obj.date_format, "frequency": obj.frequency,
            "constraints": obj.constraints,
        }
        if obj.features:
            d["features"] = obj.features
        if obj.description:
            d["description"] = obj.description
        if obj.calibration:
            d["calibration"] = obj.calibration
        return d
    raise TypeError(type(obj))


class DataError(ValueError):
    """A user-data configuration or format error. The message is machine-readable context."""


_CONFIG_SCHEMA_HINT = (
    "期望的最小完整结构（factor_config_write 的 config 参数）:\n"
    '{"version": 1, "environments": {\n'
    '  "etf": {"label": "ETF日线", "kind": "panel",\n'
    '          "source": {"type": "parquet", "path": "C:/绝对路径/data.parquet", "options": {}},\n'
    '          "layout": "long",\n'
    '          "mapping": {"symbol": "symbol", "date": "eob", "open": "open", "high": "high",\n'
    '                      "low": "low", "close": "close", "volume": "volume", "amount": "amount"},\n'
    '          "dateFormat": "%Y-%m-%d %H:%M:%S", "description": "自由说明",\n'
    '          "calibration": {"profile": "cn_etf_daily", "horizon": 20}},\n'
    ' "library": {"type": "python_module", "path": "C:/绝对路径/known_factors.py"}}\n'
    "环境 ID 用数据本名（如 etf/stock_smallcap），不要改成 primary。"
    "calibration.profile 可选 cn_etf_daily/cn_stock_daily/cn_etf_minute/cn_stock_minute"
    "（个股预设自动启用涨跌停一字板 mask；分钟预设 t0 执行），horizon/cost_bps 等全部可覆盖。"
    "三区分界 dev_end/sel_end 由用户选择（建议市场风格切换点）；不写则按数据 60/20/20"
    " 自动保底切分并在 load 报告中标注 auto——正式挖掘前应与用户确认后显式写入。"
)


def _read_source(spec: SourceSpec) -> pd.DataFrame:
    if spec.type == "glob":
        paths = sorted(globlib.glob(spec.path))
        if not paths:
            raise DataError(f"glob 未匹配任何文件: {spec.path}")
        frames = [_read_one(Path(p), spec.options, infer_type=p) for p in paths]
        return pd.concat(frames, ignore_index=True)
    return _read_one(Path(spec.path), spec.options, infer_type=spec.type)


def _read_one(path: Path, options: dict[str, Any], infer_type: str | None = None) -> pd.DataFrame:
    # 路径防御：空串会被 Path(".") 当成当前目录，pyarrow 把目录当数据集递归扫描，
    # 曾在 DSH 工作区撞上 node_modules 循环符号链接导致 WinError 1921。
    if str(path) in ("", "."):
        raise DataError("数据路径为空：请在 source.path 里给出数据文件的绝对路径")
    if not path.exists():
        raise DataError(f"数据文件不存在: {path}")
    if path.is_dir():
        raise DataError(f"路径是目录不是文件（目录递归扫描已禁用）: {path}")
    typ = infer_type or path.suffix.lower()
    if typ in ("auto", None, ""):
        typ = path.suffix.lower()
    if typ in (".parquet", "parquet"):
        return pd.read_parquet(path, **options)
    if typ in (".csv", "csv", ".txt"):
        return pd.read_csv(path, **options)
    raise DataError(f"不支持的源类型/后缀: {typ}（支持 parquet/csv/glob）")


def _resolve_column(df: pd.DataFrame, name: str | None, field_name: str, env_label: str) -> str | None:
    if name:
        if name not in df.columns:
            raise DataError(f"[{env_label}] 映射列 '{name}'（{field_name}）不存在；可用列: {list(df.columns)}")
        return name
    return None


def _coerce_date(df: pd.DataFrame, col: str | None, env_label: str) -> pd.DataFrame:
    if col is None:
        if df.index.name or not isinstance(df.index, pd.RangeIndex):
            dates = pd.to_datetime(df.index).tz_localize(None)
            return df.assign(__date__=dates.values)
        raise DataError(f"[{env_label}] 缺少日期列映射（date 为 null 且 index 不可解析）")
    dates = pd.to_datetime(df[col]).dt.tz_localize(None)
    return df.assign(__date__=dates.values)


def _load_long(spec: EnvironmentSpec) -> pd.DataFrame:
    df = _read_source(spec.source)
    m = spec.mapping
    sym = _resolve_column(df, m.symbol, "symbol", spec.id)
    if sym is None:
        raise DataError(f"[{spec.id}] 缺少 symbol 列映射")
    for f in REQUIRED_OHLCV:
        _resolve_column(df, getattr(m, f), f, spec.id)
    if m.date is None and df.index.name is None:
        raise DataError(f"[{spec.id}] long 布局缺少 date 列映射")
    df = _coerce_date(df, m.date, spec.id)
    df = df.copy()
    df["__symbol__"] = df[sym].astype(str)
    return df


def _field_column_for_wide(df: pd.DataFrame, field_name: str, spec: EnvironmentSpec) -> str:
    m = spec.mapping
    if isinstance(df.columns, pd.MultiIndex):
        if len(df.columns.names) != 2:
            raise DataError(f"[{spec.id}] MultiIndex 列需要 (symbol, field) 两级")
        level = 1 if m.field_position == "last" else 0
        hits = [c for c in df.columns if c[level] == field_name]
        if not hits:
            raise DataError(f"[{spec.id}] MultiIndex 列中找不到字段 {field_name}")
        return hits[0]
    # single-level pattern column
    hits = [c for c in df.columns if str(c).endswith(m.separator + field_name)]
    if not hits:
        raise DataError(f"[{spec.id}] wide 列中找不到字段 {field_name}（期望后缀 {m.separator}{field_name}）")
    return hits[0]


def _load_wide(spec: EnvironmentSpec) -> pd.DataFrame:
    df = _read_source(spec.source)
    if spec.mapping.date is not None:
        df = _coerce_date(df, spec.mapping.date, spec.id)
    elif not isinstance(df.index, pd.RangeIndex):
        df = _coerce_date(df, None, spec.id)
    else:
        raise DataError(f"[{spec.id}] wide 布局缺少 date 映射或日期 index")

    if isinstance(df.columns, pd.MultiIndex):
        # assign(__date__=...) turns the date into a ( '__date__', '' ) MultiIndex
        # column; extract it back into a plain Series before melting.
        date_key = ("__date__", "") if ("__date__", "") in df.columns else "__date__"
        if date_key not in df.columns:
            raise DataError(f"[{spec.id}] 日期列未正确附加")
        dates = pd.to_datetime(df[date_key]).tz_localize(None)
        df = df.drop(columns=[date_key])
        if df.columns.names[0] != "symbol" and spec.mapping.symbol_pattern is None:
            raise DataError(f"[{spec.id}] MultiIndex 第一级必须是 symbol（当前: {df.columns.names}）")
        field_level = 1 if spec.mapping.field_position == "last" else 0
        sym_level = 0 if field_level == 1 else 1
        frames = []
        for col in df.columns:
            if len(col) < 2:
                raise DataError(f"[{spec.id}] MultiIndex 列 {col!r} 缺少 field 级")
            frames.append(pd.DataFrame({
                "__date__": dates.values,
                "__symbol__": col[sym_level],
                "__field__": col[field_level],
                "__value__": df[col].values,
            }))
        long = pd.concat(frames, ignore_index=True)
        out = long.pivot_table(index="__date__", columns=["__symbol__", "__field__"], values="__value__")
        out.columns = pd.MultiIndex.from_tuples(out.columns, names=["symbol", "field"])
        out.index = pd.to_datetime(out.index)
        return out
    # single-level wide: melt selected OHLCV columns, then split symbol from field suffix
    selected = {}
    for f in REQUIRED_OHLCV:
        selected[f] = _field_column_for_wide(df, f, spec)
    if spec.mapping.amount:
        _resolve_column(df, spec.mapping.amount, "amount", spec.id)
    cols = ["__date__"] + list(selected.values())
    long = df[cols].melt(id_vars="__date__", var_name="__col__", value_name="__value__")
    long["__field__"] = long["__col__"].str.rsplit(spec.mapping.separator, n=1).str[-1]
    long["__symbol__"] = long["__col__"].str.rsplit(spec.mapping.separator, n=1).str[0]
    out = long.pivot_table(index="__date__", columns=["__symbol__", "__field__"], values="__value__")
    out.columns = pd.MultiIndex.from_tuples(out.columns, names=["symbol", "field"])
    return out


def _load_per_symbol(spec: EnvironmentSpec) -> pd.DataFrame:
    paths = sorted(globlib.glob(spec.source.path))
    if not paths:
        raise DataError(f"[{spec.id}] per_symbol glob 未匹配任何文件: {spec.source.path}")
    frames = []
    for p in paths:
        df = _read_one(Path(p), spec.source.options)
        df = _coerce_date(df, spec.mapping.date, spec.id)
        if spec.mapping.symbol_pattern:
            m = re.search(spec.mapping.symbol_pattern, str(p))
            if not m or not m.groups():
                raise DataError(f"[{spec.id}] 文件名 {p} 无法用 symbol_pattern 提取 symbol")
            symbol = m.group(1)
        else:
            symbol = Path(p).stem
        df = df.copy()
        df["__symbol__"] = symbol
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _load_minute_features(spec: EnvironmentSpec) -> pd.DataFrame:
    df = _read_source(spec.source)
    sym = _resolve_column(df, spec.mapping.symbol, "symbol", spec.id)
    if sym is None:
        raise DataError(f"[{spec.id}] minute_features 缺少 symbol 列映射")
    df = _coerce_date(df, spec.mapping.date, spec.id)
    missing = [k for k, col in spec.features.items() if not col or col not in df.columns]
    if missing:
        raise DataError(f"[{spec.id}] minute_features 缺少映射列: {missing}")
    return df


def _pivot_long(long: pd.DataFrame, env_label: str) -> dict[str, Any]:
    required_cols = {}
    for f in REQUIRED_OHLCV:
        if f not in long.columns:
            raise DataError(f"[{env_label}] long 表缺少字段 {f}")
        required_cols[f] = f

    panel = long.set_index(["__date__", "__symbol__"]).sort_index()
    panel.index.names = ["date", "symbol"]
    out = {"dates": None, "symbols": None}
    for f in REQUIRED_OHLCV:
        series = panel[required_cols[f]].astype(np.float64)
        mat = series.unstack("symbol")
        out[f] = mat
    if "amount" in long.columns:
        out["amount"] = panel["amount"].astype(np.float64).unstack("symbol")
    if "listed" in long.columns:
        out["listed"] = panel["listed"].astype(bool).unstack("symbol")
    out["dates"] = out["open"].index
    out["symbols"] = list(out["open"].columns)
    return out


def _pivot_wide(wide: pd.DataFrame, spec: EnvironmentSpec) -> dict[str, Any]:
    """wide is a (date, (symbol, field)) DataFrame."""
    dates = pd.to_datetime(wide.index).tz_localize(None)
    wide = wide.copy()
    wide.index = dates
    symbols = list(wide.columns.get_level_values(0).unique())
    out = {"dates": dates, "symbols": symbols}
    for f in REQUIRED_OHLCV:
        if f not in wide.columns.get_level_values(1):
            raise DataError(f"[{spec.id}] wide 表缺少字段 {f}")
        mat = wide.xs(f, axis=1, level=1)
        mat = mat.reindex(columns=symbols)
        out[f] = mat.astype(np.float64)
    level1 = set(wide.columns.get_level_values(1))
    if "amount" in level1:
        out["amount"] = wide.xs("amount", axis=1, level=1).reindex(columns=symbols).astype(np.float64)
    if "listed" in level1:
        out["listed"] = wide.xs("listed", axis=1, level=1).reindex(columns=symbols).astype(bool)
    return out


def validate_env_lightweight(spec: EnvironmentSpec) -> None:
    """轻量校验（config.validate 用）：文件可访问 + 映射列存在于数据列。
    不读数据体、不 pivot——全量校验发生在 factor_load_env。"""
    p = Path(spec.source.path)
    if str(p) in ("", "."):
        raise DataError("source.path 为空")
    if not p.exists():
        raise DataError(f"数据文件不存在: {p}")
    if p.is_dir():
        raise DataError(f"source.path 是目录（请给具体数据文件）: {p}")
    if p.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq
        names = {str(n) for n in pq.ParquetFile(str(p)).schema_arrow.names}
    elif p.suffix.lower() in (".csv", ".txt"):
        names = set(pd.read_csv(p, nrows=0).columns.astype(str))
    else:
        return  # 未知后缀留给 load 时报错
    m = spec.mapping
    for field in ("symbol", "date", *REQUIRED_OHLCV, "amount", "listed"):
        col = getattr(m, field)
        if col and col not in names:
            raise DataError(f"映射列 '{col}'（{field}）在数据列中不存在；可用列: {sorted(names)[:20]}")


def _validate_matrices(mats: dict[str, Any], spec: EnvironmentSpec) -> None:
    constraints = spec.constraints or {}
    c = mats["close"]
    if c.shape[1] < int(constraints.get("minSymbols", 20)):
        raise DataError(f"[{spec.id}] symbol 数 {c.shape[1]} < minSymbols {constraints.get('minSymbols')}")
    if c.shape[0] < int(constraints.get("minDates", 200)):
        raise DataError(f"[{spec.id}] 日期数 {c.shape[0]} < minDates {constraints.get('minDates')}")
    # 上市对齐 + 退市对齐（双向）：个股面板的合法缺口有三类——
    #   上市前（未上市）、末有效日之后（退市/长期停牌尾巴）、
    # 以及 A 股个股常态的**有效期内停牌洞**（与真数据缺失无法从数据区分）。
    # requireFiniteOhlcv 三态：
    #   "report"（默认）= 不阻断，缺口摘要进 dataQuality 返回；
    #   true/"strict"  = 有效期内有洞才报错（适合 ETF 等几乎不停牌的资产）；
    #   false/"off"    = 完全不查。
    mode = constraints.get("requireFiniteOhlcv", "report")
    strict = mode in (True, "strict", "true")
    if mode not in (False, None, "off", "false") and not strict:
        if mode != "report":
            raise DataError(
                f"requireFiniteOhlcv 只支持 true(strict)/false(off)/'report'，收到 {mode!r}")
    if mode not in (False, None, "off", "false"):
        valid = np.ones_like(mats["close"].values, dtype=bool)
        for f in REQUIRED_OHLCV:
            valid &= np.isfinite(mats[f].values)  # (T, N)
        has_data = valid.any(axis=0)              # 每 symbol 是否有任一有效日
        T = valid.shape[0]
        first_valid = valid.argmax(axis=0)        # 首个全字段有限日
        last_valid = T - 1 - valid[::-1].argmax(axis=0)  # 末个有限日（右对齐）
        t_axis = np.arange(T)[:, None]
        in_window = (t_axis >= first_valid[None, :]) & (t_axis <= last_valid[None, :])
        pre = int(((~valid) & (t_axis < first_valid[None, :])).sum())
        post = int(((~valid) & (t_axis > last_valid[None, :])).sum())
        holes = int(((~valid) & in_window).sum())  # 有效期内洞（停牌/缺数据）
        mats["dataQuality"] = {
            "mode": "strict" if strict else "report",
            "preListingHoles": pre,        # 合法：未上市
            "postLastValidHoles": post,    # 合法：退市/停牌尾巴（右对齐）
            "inWindowHoles": holes,        # 停牌或真缺失（个股常态，数据无法区分）
            "note": ("inWindowHoles 为有效期内缺口——A股个股多为停牌；"
                     "若疑似数据损坏可设 requireFiniteOhlcv=true 复查严格模式"),
        }
        if strict and holes > 0:
            raise DataError(
                f"[{spec.id}] 有效期内存在 {holes} 个缺口（停牌/数据缺失）；"
                f"另有上市前 {pre} 格与末有效日后 {post} 格为合法缺口。"
                f"个股停牌是常态——建议改用默认 report 模式或 requireFiniteOhlcv=false")
    if not constraints.get("allowZeroVolume", True):
        bad = int((mats["volume"].values == 0).sum())
        if bad:
            raise DataError(f"[{spec.id}] 存在 {bad} 个零成交量（allowZeroVolume=false）")


def normalize_environment(spec: EnvironmentSpec) -> dict[str, Any]:
    if spec.kind == "minute_features":
        df = _load_minute_features(spec)
        # Feature table is returned raw; alignment to a panel env happens in FactorRuntime.
        return {"kind": "minute_features", "frame": df}

    if spec.layout == "long":
        long = _load_long(spec)
        mats = _pivot_long(long, spec.id)
    elif spec.layout == "per_symbol":
        long = _load_per_symbol(spec)
        mats = _pivot_long(long, spec.id)
    elif spec.layout in ("wide", "multiindex"):
        wide = _load_wide(spec)
        mats = _pivot_wide(wide, spec)
    else:
        raise DataError(f"[{spec.id}] 未知 layout: {spec.layout}（支持 long/wide/per_symbol/multiindex）")

    _validate_matrices(mats, spec)
    return {"kind": "panel", **mats}


def build_factor_env(mats: dict[str, Any], calibration: dict[str, Any] | None = None):
    """Build FactorEnv from normalize_environment output.

    三区划分用户化：calibration 未显式给出 dev_end/sel_end 时，按数据时间范围
    自适应切分（train/selection/test = 60%/20%/20%）并回填实际分界——不使用
    任何隐式历史默认。建议 agent 配置时向用户展示数据范围并确认分界。
    """
    if mats.get("kind") != "panel":
        raise DataError("minute_features 不能直接构建 FactorEnv")
    from ..factor.env import Calibration, FactorEnv

    cal_raw = dict(calibration or {})
    cal_raw.pop("profile", None)  # profile 已在 resolve 阶段展开
    dates = pd.DatetimeIndex(mats["dates"]).tz_localize(None)
    symbols = list(mats["symbols"])
    amount = mats.get("amount")
    listed = mats.get("listed")

    explicit_regions = ("dev_end" in cal_raw and "sel_end" in cal_raw
                        and cal_raw.get("dev_end") and cal_raw.get("sel_end"))
    cal = Calibration(**{k: v for k, v in cal_raw.items()
                         if k in Calibration.__dataclass_fields__})
    if not explicit_regions:
        n = len(dates)
        if n < 30:
            raise DataError(f"日期数 {n} < 30，无法划分三区")
        cal.dev_end = str(dates[int(n * 0.6)].date())
        cal.sel_end = str(dates[int(n * 0.8)].date())
        cal.regions_auto = True

    if pd.Timestamp(cal.dev_end) <= dates[0]:
        raise DataError(
            f"dev_end={cal.dev_end} 早于数据起点 {str(dates[0].date())}——train 区为空，"
            "请检查三区分界与数据的匹配")
    if pd.Timestamp(cal.sel_end) <= pd.Timestamp(cal.dev_end):
        raise DataError(f"sel_end={cal.sel_end} 必须晚于 dev_end={cal.dev_end}")
    if pd.Timestamp(cal.sel_end) >= dates[-1]:
        raise DataError(
            f"sel_end={cal.sel_end} 不早于数据末端 {str(dates[-1].date())}——test 区为空，"
            "请检查三区分界与数据的匹配")

    return FactorEnv(
        mats["open"].values, mats["high"].values, mats["low"].values,
        mats["close"].values, mats["volume"].values,
        dates, symbols,
        listed=listed.values if listed is not None else None,
        amount=amount.values if amount is not None else None,
        calibration=cal,
    )


def _memory_estimate_gb(rows: int, symbol_count: int | None, date_count: int | None) -> dict:
    """规模防呆（批次1a）：pivot 峰值内存估算。

    长表 DataFrame 开销 ~3× 原始 + pivot 后 6~8 个 (T,N) float64 矩阵。
    粗估而非精确——目的只是分钟级大池在加载前给用户预期。"""
    if not rows or not symbol_count or not date_count:
        return {"estimate_gb": None}
    long_gb = rows * 9 * 8 * 3 / 1e9          # 长表：~9 列 × 8B × 3 开销系数
    panel_gb = date_count * symbol_count * 8 * 8 / 1e9  # 8 个 (T,N) 矩阵
    est = round(long_gb + panel_gb, 2)
    out = {"estimate_gb": est}
    if est > 4:
        out["warning"] = (f"预计加载峰值约 {est} GB——确认内存足够；"
                          "分钟级全池建议按年份/标的分片")
    return out


def probe_file(path: str, layout: str | None = None, date_format: str | None = None,
               limit: int = 2000) -> dict[str, Any]:
    """探测数据文件并产出映射建议报告。

    parquet：元数据取全表行数 + symbol/date 列全量扫描（真实 unique 数与日期范围），
    不再把整表读进内存——修复"只看前 2000 行导致聚簇存储的 parquet 误报标的数"。
    CSV 等回退 head 采样并明确标注 coverage="sampled"。
    """
    p = Path(path)
    if str(p) in ("", "."):
        return {"ok": False, "error": "路径为空", "path": path}
    if any(ch in str(p) for ch in "*?["):
        matches = sorted(globlib.glob(str(p)))
        if not matches:
            return {"ok": False, "error": f"glob 未匹配任何文件: {path}", "path": path}
        probe = probe_file(matches[0], layout=layout, date_format=date_format, limit=limit)
        probe["glob_files"] = len(matches)
        probe["path"] = path
        return probe
    if not p.exists():
        return {"ok": False, "error": f"文件不存在: {path}", "path": path}
    if p.is_dir():
        return {"ok": False, "error": f"路径是目录，请给出具体数据文件（目录递归扫描已禁用）: {path}", "path": path}

    if p.suffix.lower() == ".parquet":
        try:
            return _probe_parquet(p, layout, date_format)
        except Exception as e:  # keep probe non-fatal
            return {"ok": False, "error": f"parquet 探测失败: {e}", "path": path}

    try:
        df = _read_one(p, {})
    except Exception as e:  # keep probe non-fatal
        return {"ok": False, "error": str(e), "path": path}
    sample = df.head(limit)
    columns = list(df.columns)
    suggested, date_range, symbol_count = _suggest_mapping(sample, columns, date_format)
    return {
        "ok": True,
        "path": path,
        "layout": layout or ("wide" if isinstance(df.columns, pd.MultiIndex) else "long"),
        "columns": columns,
        "dtypes": {str(c): str(t) for c, t in sample.dtypes.items()},
        "coverage": "sampled",
        "sample_rows": int(len(sample)),
        "rows_total": int(len(df)),
        "date_range": date_range,
        "symbol_count": symbol_count,
        "note": "CSV 按前 sample_rows 行采样推断；标的数与日期范围可能低估",
        "suggested_mapping": {k: v for k, v in suggested.items() if v is not None},
        "missing_required": [f for f in REQUIRED_OHLCV if suggested[f] is None],
        "ambiguous": {
            "date_format": date_format is None,
            "symbol_parse": suggested["symbol"] is None,
        },
    }


def _suggest_mapping(frame: pd.DataFrame, columns: list, date_format: str | None):
    """从列名猜测 OHLCV/symbol/date 映射，并统计采样范围。"""
    lowered = {str(c).strip().lower(): c for c in columns}

    def guess(*names):
        for n in names:
            if n in lowered:
                return lowered[n]
        return None

    suggested = {
        "symbol": guess("symbol", "code", "ticker", "ts_code", "secid"),
        "date": guess("eob", "date", "trade_date", "datetime", "time"),
        "open": guess("open", "open_price", "o"),
        "high": guess("high", "high_price", "h"),
        "low": guess("low", "low_price", "l"),
        "close": guess("close", "close_price", "c", "price"),
        "volume": guess("volume", "vol", "v"),
        "amount": guess("amount", "turnover", "amt"),
    }
    date_range = None
    if suggested["date"]:
        try:
            d = pd.to_datetime(frame[suggested["date"]]).dt.tz_localize(None)
            date_range = [str(d.min()), str(d.max())]
        except Exception:
            pass
    symbol_count = None
    if suggested["symbol"]:
        symbol_count = int(frame[suggested["symbol"]].nunique())
    return suggested, date_range, symbol_count


def _probe_parquet(p: Path, layout: str | None, date_format: str | None) -> dict[str, Any]:
    """parquet 快速探测：元数据 O(1) 取行数，symbol/date 单列全量扫描。"""
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(str(p))
    names = [str(n) for n in pf.schema_arrow.names]
    rows_total = int(pf.metadata.num_rows)
    suggested, _, _ = _suggest_mapping(pd.DataFrame(columns=names), names, date_format)
    symbol_count = None
    date_range = None
    n_dates = None
    if suggested["symbol"]:
        col = pq.read_table(str(p), columns=[suggested["symbol"]]).column(0).to_pandas()
        symbol_count = int(col.nunique())
    if suggested["date"]:
        try:
            s = pd.to_datetime(
                pq.read_table(str(p), columns=[suggested["date"]]).column(0).to_pandas())
            if getattr(s.dt, "tz", None) is not None:
                s = s.dt.tz_localize(None)
            date_range = [str(s.min()), str(s.max())]
            n_dates = int(s.dt.normalize().nunique())
        except Exception:
            pass
    return {
        "ok": True,
        "path": str(p),
        "layout": layout or "long",
        "columns": names,
        "dtypes": {str(f.name): str(f.type) for f in pf.schema_arrow},
        "coverage": "full",
        "rows_total": rows_total,
        "sample_rows": rows_total,
        "symbol_count": symbol_count,
        "date_range": date_range,
        "memory_estimate": _memory_estimate_gb(rows_total, symbol_count, n_dates),
        "suggested_mapping": {k: v for k, v in suggested.items() if v is not None},
        "missing_required": [f for f in REQUIRED_OHLCV if suggested[f] is None],
        "ambiguous": {
            "date_format": date_format is None,
            "symbol_parse": suggested["symbol"] is None,
        },
    }
