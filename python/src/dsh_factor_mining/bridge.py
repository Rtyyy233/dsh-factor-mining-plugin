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
    _dsr_p_from_stats,
    evaluate,
    evaluate_batch,
    evaluate_composite,
    evaluate_selection,
    evaluate_test,
    evaluate_walk_forward,
)
from .library.contract import LibraryError, UserLibrary, query_entries
from .state import (
    append_explored,
    append_search_path,
    append_trail,
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

    def _landscape_pool_std(self, landscape, env_id: str) -> float | None:
        """null 地形 → pool_std 估计（含指纹硬门）。无效地形 → None。

        返回 None 时 evaluate 路径不注入 pool_std → N>1 的 deflated p 拒绝
        给出（D7 链路自然引导重校准），而不是拿错基线给不可信数字。"""
        if not isinstance(landscape, dict) or not landscape.get("ic_ir"):
            return None
        if self._landscape_fingerprint_status(landscape, env_id) != "match":
            return None
        q = landscape["ic_ir"]
        p10, p90 = q.get("p10"), q.get("p90")
        if p10 is None or p90 is None:
            return None
        return (p90 - p10) / 2.5631

    def _progress(self, label: str, done: int, total: int):
        if self._on_progress is not None:
            try:
                self._on_progress({"label": label, "done": done, "total": total})
            except Exception:
                pass

    def _make_receipt(self, result: dict[str, Any]) -> str:
        """H2 receipt：对诊断的关键数字做指纹缓存——registry_submit 校验用（防编造）。"""
        import hashlib as _h
        keys = ("ic_ir_train", "ic_mean_train", "ic_n_train", "ic_ir", "ic_mean", "ic_n", "verdict")
        payload = json.dumps({k: result.get(k) for k in keys if k in result},
                             sort_keys=True, ensure_ascii=False, default=str)
        rid = _h.sha256(payload.encode("utf-8")).hexdigest()[:12]
        self._receipts[rid] = {k: result.get(k) for k in keys if k in result}
        # 有界：只保留最近 4096 个 receipt（512 太小——50 轮×10 因子即触顶，
        # 被挤掉的 receipt 会让诚实提交降级 verified:false）
        if len(self._receipts) > 4096:
            self._receipts = dict(list(self._receipts.items())[-4096:])
        return rid

    def _verify_receipt(self, diagnosis: dict[str, Any]) -> bool | None:
        """内部三态：True（验证通过）/ False（声称有 receipt 但不匹配）/ None（无 receipt
        或缓存丢失，降级 unverified）。出口（submit 返回的 receipt_verified）一律
        bool 化（None→False）——下游 `is False` 严格检查不漏「无 receipt 手构」场景
        （2026-08-18 独立审计 F-A1-SEM 修正）。"""
        rid = diagnosis.get("_receipt")
        if not rid:
            return None
        rec = self._receipts.get(str(rid))
        if rec is None:
            return None  # bridge 重启后缓存丢失：同样降级，不冤枉
        return all(diagnosis.get(k) == v for k, v in rec.items() if v is not None)

    def _append_engine_trail(self, env_id: str, source_hash: str, stage: str,
                             result: dict[str, Any], suspects: dict[str, Any] | None):
        """引擎层强制 trail（evaluate 自动记录硬事实，按 source_hash+stage 去重更新）。
        agent 的叙事层 trail（record_trail）与此分文件存储，trail_summary 合并。"""
        self._append_engine_trail_batch(env_id, [(source_hash, stage, result, suspects)])

    def _append_engine_trail_batch(self, env_id: str, items: list[tuple]):
        """批量写 trail_engine（2026-08-19 A1）：evaluate_batch 的 M 个成员一次落盘。

        单条版逐成员调用 = 每成员整文件重写（O(M²) IO）；批量版读一次、
        过滤+追加 M 条、原子写一次。items: [(source_hash, stage, result, suspects)]。
        sketch 处理（A2）：result 里的 ic_series_train 摘出存 entry（trail 级
        N_eff 聚类用），不进 agent 可见 schema 的其余部分由调用方 pop。"""
        try:
            p = Path(self.state_root) / "trail_engine.json"
            entries = []
            if p.exists():
                entries = json.loads(p.read_text(encoding="utf-8"))
            fp = self._env_full_fingerprint(env_id)
            for source_hash, stage, result, suspects in items:
                sketch = None
                if isinstance(result, dict):
                    s = result.get("ic_series_train")
                    if isinstance(s, (list, tuple)) and len(s) > 0:
                        # 截尾 256 点（均匀采样）：防御性上限，正常 train IC 序列
                        # 远小于此；round 4 位足够聚类相关计算
                        if len(s) > 256:
                            idx = np.linspace(0, len(s) - 1, 256).astype(int)
                            s = [s[i] for i in idx]
                        sketch = [round(float(x), 4) for x in s]
                entry = {
                    "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
                    "envId": env_id, "source_hash": source_hash, "stage": stage,
                    "fingerprint": fp,
                    "engine_version": __version__,
                    "ic_ir": result.get("ic_ir_train", result.get("ic_ir")) if isinstance(result, dict) else None,
                    "ic_mean": result.get("ic_mean_train", result.get("ic_mean")) if isinstance(result, dict) else None,
                    "verdict": result.get("verdict") if isinstance(result, dict) else None,
                    "red_flags": result.get("red_flags", []) if isinstance(result, dict) else [],
                    "suspects": suspects,
                    "ic_series_sketch": sketch,
                }
                entries = [e for e in entries
                           if not (e.get("source_hash") == source_hash and e.get("stage") == stage)]
                entries.append(entry)
            # 原子写（tmp + os.replace）：中途崩溃不留半截 JSON
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(str(tmp), str(p))
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
        self._append_engine_trail(env_id, source_hash, stage, result, suspects)
        # A2：sketch 已由 _append_engine_trail 摘出存 trail；agent 可见
        # schema 保持不变（ic_series_train 不外泄，registry 不膨胀）
        result.pop("ic_series_train", None)
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
            return fn(params or {})
        except DataError as e:
            # 用户数据/配置错误统一映射到域错误码（调用方无需感知异常类型）
            raise BridgeError(-32002, str(e)) from None

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
        return {
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
            env_os = dict(os.environ)
            if "site-packages" not in src_dir:
                env_os["PYTHONPATH"] = src_dir + os.pathsep + env_os.get("PYTHONPATH", "")

            timeout_s = self.worker_timeout_ms / 1000.0
            cmd = [sys.executable, "-m", "dsh_factor_mining.worker",
                   "--request", str(req_path), "--result", str(out_path)]
            try:
                # cwd 隔离：worker 是纯计算进程，绝不继承 DSH 工作区 cwd
                # （曾因工作区 node_modules 循环符号链接 + MAX_PATH 触发 WinError 1921）。
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=timeout_s, env=env_os, cwd=str(run_root))
            except subprocess.TimeoutExpired:
                # 杀进程树：Windows 用 taskkill /T（跨平台兼容：POSIX 下 subprocess.run
                # 超时已自动 kill 主进程，worker 无孙进程，无需额外动作）
                if sys.platform == "win32":
                    try:
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                       capture_output=True)
                    except Exception:
                        pass
                raise BridgeError(-32005, f"worker 超时（>{timeout_s:.0f}s），已终止")

            if not out_path.exists():
                tail = (proc.stderr or "")[-500:]
                # 污染特征快速诊断：stdlib backport 遮蔽（2026-08-18 实测事故，
                # agent 曾为此耗掉整轮对话——在这里给出可执行的修法）
                hint = ""
                if "pathlib" in tail or "from collections import" in tail:
                    hint = ("｜疑似 site-packages 的 stdlib backport（如 pathlib 1.0.1）遮蔽内置库："
                            "pip uninstall pathlib 后重启会话")
                raise BridgeError(
                    -32005,
                    f"worker 无结果输出(rc={proc.returncode}): {tail}{hint}")
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
            F = fn(env)
            _ps = params.get("pool_std")
            _ps = float(_ps) if isinstance(_ps, (int, float)) else None
            if stage == "development":
                return evaluate(F, env, n_trials=int(params.get("n_trials", 1)),
                                pool_std=_ps)
            if stage == "selection":
                return evaluate_selection(F, env)
            if stage == "test":
                try:
                    return evaluate_test(F, env, state_root=self.state_root)
                except RuntimeError as e:
                    raise BridgeError(-32003, str(e))
            raise BridgeError(-32602, f"未知 stage: {stage}")
        if method == "factor.evaluate_composite":
            parts = {}
            for name, src in (params.get("ingredients") or {}).items():
                parts[name] = self._compile_factor(src)(env)
            return evaluate_composite(fn(env), parts, env)
        if method == "factor.evaluate_batch":
            F_dict = {}
            for name, src in (params.get("sources") or {}).items():
                F_dict[name] = self._compile_factor(src)(env)
            return evaluate_batch(F_dict, env)
        if method == "factor.walk_forward":
            return evaluate_walk_forward(
                fn(env), env, n_folds=int(params.get("n_folds", 5)),
                t0_date=params.get("t0_date"), t1_date=params.get("t1_date"))
        if method == "factor.audit":
            return audit_mod.audit(fn, env)
        raise BridgeError(-32602, f"未知 factor 方法: {method}")

    def _enforce_causality(self, source: str, env, env_id: str | None = None) -> dict[str, Any]:
        """H3 前置强制：evaluate 前该 source 必须有 causal verdict。FUTURE_LEAK 直接拒绝评估。

        缓存 key = 源码hash : 环境三元组指纹（数据文件+口径+引擎版本）——
        同一源码换环境/换数据文件后不得误命中旧结论：扰动法依赖 env 尺寸与数据，
        跨环境的前视结论不可移植。纪律变机制，不靠 agent 自觉。"""
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
        # 低效模式扫描（效率防御层1）：warning 不阻断，附在结果里
        ineff = scan_inefficiency(source)
        result = self._run_factor("factor.check_causality", source, params, env)
        if isinstance(result, dict):
            result["causal"] = result.get("verdict") == "causal"
            if not ineff.get("ok", True):
                result["inefficiency_warning"] = ineff
            self._causality_cache[self._causality_cache_key(source, env_id)] = result
        return result

    @staticmethod
    def _n_eff_from_entries(entries: list[dict], source_hash: str | None) -> float:
        """trail 级 N_eff（2026-08-19 A2）：簇聚类 + 簇内线性修正。

        口径：探索过程中**所有计算过的因子**（trail_engine 全体——评估成功
        即写，verdict=fail 也算；选择偏差发生在评估时刻，不是注册时刻）。

        算法：
        - 成员 = unique source_hash（dev/batch 双写同 hash 只计一次）+ 本因子
          （未评估过则按单例追加）；每个成员带 ic_series_sketch（train IC 序列）
        - 两两 |pearson ρ| >= 0.7 连边（与库内 curate_strong_subset 阈值一致，
          不引 scipy）→ union-find 聚簇：同族参数变体归一簇
        - N_eff = Σ_簇 [1 + (m_c-1)·(1-ρ̄_c)]：纯簇数对簇内宽度失明（50 个
          高相关变体只算 1），纯计数对相关性失明（50 个变体算 50）；线性
          修正各取所长——ρ̄_c→1 时簇退化为 1，ρ̄_c→0 时簇退化为 m_c
        - 无 sketch 旧条目 / 长度不齐 / 零方差：按单例计（保守——N_eff 只高不低）

        退化兼容：全体无 sketch（旧版 trail）→ 纯计数，等于旧口径
        unique hash 数，行为不变。

        生产审核修正（2026-08-19）：
        - 畸形条目（None/非 dict/坏类型字段）直接跳过不崩（审核 R1）
        - 相关矩阵按 sketch 长度分组向量化（np.corrcoef 整组一次）——
          M=400 时逐对 Python 循环 >3s 且每次 evaluate 都要付；向量化后
          毫秒级（审核 R3）
        """
        by_hash: dict[str, list | None] = {}
        for e in entries:
            if not isinstance(e, dict):
                continue  # 生产审核 R1：畸形条目（null/字符串等）跳过
            h = e.get("source_hash")
            if not h or h in by_hash:
                continue
            s = e.get("ic_series_sketch")
            by_hash[h] = s if isinstance(s, list) and len(s) >= 5 else None
        if source_hash and source_hash not in by_hash:
            by_hash[source_hash] = None  # 本因子未评估过：单例计（不重复计已评估的）

        hashes = list(by_hash)
        sketches = [by_hash[h] for h in hashes]
        M = len(hashes)
        if M <= 1:
            return 1.0

        parent = list(range(M))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        # 按长度分组向量化相关（R3）：同长度组内 np.corrcoef 一次算完；
        # 跨长度 / 无 sketch 无法算相关 → 不连边（各归各的簇）
        corr: dict[tuple[int, int], float] = {}
        groups: dict[int, list[int]] = {}
        for i, s in enumerate(sketches):
            if s is not None:
                groups.setdefault(len(s), []).append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            mat = np.asarray([sketches[i] for i in idxs], dtype=np.float64)
            if mat.ndim != 2:
                continue
            with np.errstate(invalid="ignore", divide="ignore"):
                cm = np.corrcoef(mat)
            cm = np.atleast_2d(cm)
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    c = cm[a, b]
                    if np.isfinite(c):
                        corr[(idxs[a], idxs[b])] = abs(float(c))
        for (i, j), c in corr.items():
            if c >= 0.7:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri

        clusters: dict[int, list[int]] = {}
        for i in range(M):
            clusters.setdefault(find(i), []).append(i)

        n_eff = 0.0
        for members in clusters.values():
            m = len(members)
            if m == 1:
                n_eff += 1.0
                continue
            rhos = [corr[(min(a, b), max(a, b))]
                    for x, a in enumerate(members) for b in members[x + 1:]
                    if (min(a, b), max(a, b)) in corr]
            rho_bar = float(np.mean(rhos)) if rhos else 0.0
            n_eff += 1.0 + (m - 1) * (1.0 - rho_bar)
        return float(n_eff)

    def _resolve_pool_std(self, env_id: str, trail_pool_std: float | None) -> float | None:
        """pool_std 保守合并（2026-08-19 生产审核 ADV-11）。

        攻击面：评估 10 个同族变体（IC_IR 几乎相同）→ trail IC_IR std≈0 →
        sr0≈0（多重检验惩罚全灭），同时簇聚类又把 N_eff 压小——双重中和门控。
        修复：trail 实测 std 与指纹门后的 null 地形估计取 **max**——池尺度
        估计不得低于随机算子空间的宽度（宁可保守）。trail 覆盖面广于
        随机地形时（正常探索），max 自然取 trail——「trail 优先」语义保留。
        """
        from .factor import random_gen
        land_std = self._landscape_pool_std(
            random_gen.read_null_landscape(self.state_root), env_id)
        cands = [s for s in (trail_pool_std, land_std) if s is not None and s > 0]
        return max(cands) if cands else None

    def _trial_stats(self, source_hash: str | None) -> tuple[float, float | None]:
        """多重检验的引擎侧硬统计（2026-08-18 生产审计修正；2026-08-19 A2 N_eff 口径）。

        旧实现用 mining_state.round+1 计数——但 agent 走 factor_record_trail
        （append_trail）从不调 record_round，round 恒 0 → n_trials 恒 1，
        多重检验校正从未生效。真实计数在 trail_engine.json（evaluate/batch
        自动记录的硬事实）。

        返回 (n_eff, pool_std)：
        - n_eff = 簇聚类口径的有效假设数（见 _n_eff_from_entries；A1 落地后
          batch 成员与单因子同等入 trail，走 batch 通道不再漏计）
        - pool_std = trail 实测 IC_IR std（≥10 样本；调用方经 _resolve_pool_std
          与 null 地形保守合并）。trail 损坏 → 按空 trail 处理（N_eff 保守
          放行）但 **stderr 可见**——静默 N 重置是不可审计的事故温床（审核 ADV-12）。
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
                        f"[trail_engine] 读取失败（按空 trail 处理，N_eff 将低估）: "
                        f"{type(e).__name__}: {e}\n")
                except Exception:
                    pass
        n_eff = self._n_eff_from_entries(entries, source_hash)
        irs = [e.get("ic_ir") for e in entries
               if isinstance(e, dict) and isinstance(e.get("ic_ir"), (int, float))]
        pool_std = None
        if len(irs) >= 10:
            import numpy as _np
            pool_std = float(_np.std(irs, ddof=1))
        return n_eff, pool_std

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
                    if json.loads(lock_path.read_text(encoding="utf-8")).get("consumed", False):
                        raise BridgeError(
                            -32003,
                            "test 已被消费（test_lock.json）。test 是最终消耗品，禁止反复评估调参。"
                            "锁不在任何 reset scope——只能手动删除（意味着声明放弃本 stateRoot 的 test 纪律）。")
                except BridgeError:
                    raise
                except Exception:
                    pass  # 锁文件损坏：放行到 evaluate_test 内部路径统一处理
        env = self._require_panel_env(env_id)
        source = params.get("source", "")
        stage = params.get("stage", "development")
        self._enforce_causality(source, env, env_id)
        # n_trials/pool_std（DSR 多重检验折减）：引擎侧从 trail_engine 硬统计，
        # 不依赖 agent 自觉传 batch 或维护任何计数器（2026-08-18 修正）
        source_hash = source_fingerprint(source)
        if stage == "development":
            trials, pool_std = self._trial_stats(source_hash)
            # pool_std 保守合并（ADV-11）：trail 实测与指纹门后 null 地形取 max
            pool_std = self._resolve_pool_std(env_id, pool_std)
        else:
            trials, pool_std = 1, None
        params = {**params, "n_trials": trials, "pool_std": pool_std,
                  "source_hash": source_hash}
        result = self._run_factor("factor.evaluate", source, params, env)
        return self._wrap_diagnosis(env_id, source, stage, result)

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
        params = {**params, "n_trials": max(len(sources), 1)}
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
                # A1（2026-08-19 堵 batch 绕过）：批次成员与单因子 evaluate 同等
                # 写入 trail_engine——N_eff 计数不因走 batch 通道而漏计。此前
                # batch 成员不写 trail → N 恒不涨 → deflated p 按 N=1 给出，
                # 拿 batch 诊断直接 submit 即绕过 D7 门控（实验证实）。
                src = str(sources.get(name, ""))
                if src:
                    trail_items.append((source_fingerprint(src), "batch", diag, None))
            if trail_items:
                self._append_engine_trail_batch(env_id, trail_items)
            # A2：sketch 已存 trail；agent 可见 schema 保持不变（不留 ic_series_train）
            for name, diag in result["factors"].items():
                if isinstance(diag, dict):
                    diag.pop("ic_series_train", None)
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
            try:
                fp_env = self._resolve_env_id(env_id)
            except Exception:
                fp_env = env_id
            return random_gen.run_null_calibration(
                env, self.state_root, n=n, seed=seed, opset=opset,
                env_fingerprint=self._env_full_fingerprint(fp_env),
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
        results = []
        for i in range(n):
            tree = random_gen.generate_tree(rng, opset)
            try:
                F = random_gen._eval_tree(tree, env)
                diag = random_gen.light_ic_scan(F, env)
            except Exception as e:
                diag = {"ic_mean": None, "ic_ir": None, "n": 0, "error": str(e)[:120]}
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
        dist = [abs(r["light_ic"].get("ic_ir") or 0.0) for r in results]
        return {
            "mode": "explore", "n": n, "seed": seed, "seed_note": seed_note, "top_k": top_k,
            "top": top,
            "abs_ic_ir_distribution": {
                "median": float(np.median(dist)) if dist else None,
                "p95": float(np.percentile(dist, 95)) if dist else None,
                "max": float(np.max(dist)) if dist else None,
            },
            "null_hint": "top 因子的 |IC_IR| 若未超 null p95，大概率是噪声（见 factor.null_landscape）",
        }

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
            result = append_explored(entry, self.state_root)
            # 正式证伪的因子入 falsified 池（防重复挖坟；有指纹缓存则带数值指纹）
            src = str(entry.get("source") or entry.get("factor_source") or "")
            if src:
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
            return append_trail(entry, self.state_root)
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
        verdicts = {}
        red_flagged = []
        for e in engine_trail:
            v = e.get("verdict") or "unknown"
            verdicts[v] = verdicts.get(v, 0) + 1
            if e.get("red_flags"):
                red_flagged.append({"source_hash": e.get("source_hash"),
                                    "stage": e.get("stage"),
                                    "red_flags": e["red_flags"][:3]})
        return {
            "generated_at": _t.strftime("%Y-%m-%dT%H:%M:%S"),
            "mining": {k: mining.get(k) for k in ("round", "global_fail_streak", "finalized")},
            "termination": check_termination(mining),
            "evaluations": {"total": len(engine_trail),
                            "unique_sources": len({e.get("source_hash") for e in engine_trail}),
                            "verdict_counts": verdicts},
            "agent_rounds": len(agent_trail),
            "explored_count": len(explored),
            "red_flagged": red_flagged[-10:],
            "pool": self._pool().stats(),
            "last_engine_trail": engine_trail[-5:],
            "note": ("引擎层 trail 为 evaluate 自动记录（硬事实，不可瞒报）；"
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
        # pool/ 是有界双池记忆（active/falsified + .npy 指纹），同属挖掘记忆
        "mining": ["trail.json", "trail_engine.json", "explored_paths.json",
                   "search_paths.json", "mining_state.json", "pool"],
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
        return {"registry": read_registry(self.state_root)}

    def _registry_submit(self, params):
        """提交候选到 registry。纯数据操作：收 factor_evaluate 的完整诊断对象，不重新执行。

        去耦合（DESIGN §8）：不 require env、不编译 factor、不评估——评估在 factor_evaluate
        已做，这里只校验结构 + 落盘用户 stateRoot/registry.json。
        批次1a 强化：
        - receipt 校验（H2）：diagnosis 带 _receipt 则逐位核对关键数字——防低级模型编造
          数字入册；无 receipt / 缓存丢失 → verified:false 降级（不冤枉、可追溯）
        - 铁律代码化：同一 source_hash 不得以不同名字重复登记
        - 版本溯源：entry 自动附 fingerprint / engine_version / source_hash
        """
        name = params.get("name") or params.get("signal") or "unnamed"
        signal = params.get("signal", "")
        diagnosis = params.get("diagnosis") or params.get("result")
        if not isinstance(diagnosis, dict) or "ic_ir_train" not in diagnosis:
            raise BridgeError(-32602, "registry_submit 需要 diagnosis（factor_evaluate 的完整诊断对象，含 ic_ir_train）")
        verified = self._verify_receipt(diagnosis)
        source = str(params.get("source", ""))
        source_hash = source_fingerprint(source) if source else None
        existing = read_registry(self.state_root)
        if source_hash:
            for e in existing:
                if e.get("source_hash") == source_hash and e.get("name") != name:
                    raise BridgeError(
                        -32003,
                        f"该因子源码已以名字「{e.get('name')}」登记过（铁律：同一因子不得重复登记为新发现）")
        # 2026-08-18 生产审计补充（同名重复入册事故）：同一名字只允许一条 entry——
        # 实测 agent 想修正描述却反复 submit（同 hash 同名 / 改源码同名各一次），
        # registry 被同一因子灌 3 条。两种情况都拒绝并引导 update / 换名。
        dup = next((e for e in existing if e.get("name") == name), None)
        if dup is not None:
            if source_hash and dup.get("source_hash") == source_hash:
                raise BridgeError(
                    -32003,
                    f"名字「{name}」已登记过同一因子（铁律：不得重复入册）。"
                    "修正描述/追加备注用 factor_registry_update(name, signal/note)——"
                    "不要重复 submit。")
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
        if dp.get("sr_hat") is not None:
            src_hash_dp = source_hash or (diagnosis.get("_meta") or {}).get("source_hash")
            n_eff, trail_std = self._trial_stats(
                str(src_hash_dp) if src_hash_dp else None)
            pool_std = self._resolve_pool_std(
                params.get("envId", "primary"), trail_std)
            p_new = _dsr_p_from_stats(dp.get("sr_hat"), dp.get("skew"), dp.get("kurt"),
                                      dp.get("n_obs"), n_eff, pool_std)
            diagnosis["deflated_train"] = {**dp, "p": p_new,
                                           "n_trials": float(n_eff), "n_eff": float(n_eff),
                                           "pool_std": pool_std,
                                           "recomputed_at_submit": True}
        accepted, reason = _passes_acceptance(diagnosis)
        if diagnosis.get("red_flags"):
            accepted = False
            reason = f"red_flags 未清：{diagnosis['red_flags'][:2]}"
        entry = {
            "name": name,
            "signal": signal,
            "accepted": accepted,
            "reason": reason,
            "ic_ir_train": diagnosis.get("ic_ir_train"),
            "verified": True if verified else False,
            "diagnosis": diagnosis,
            "source": source,
            "source_hash": source_hash,
            "fingerprint": (diagnosis.get("_meta") or {}).get("fingerprint")
                           or self._env_full_fingerprint(params.get("envId", "primary")),
            "engine_version": __version__,
            "verdict": diagnosis.get("verdict"),
        }
        registry = existing
        registry.append(entry)
        write_registry(registry, self.state_root)
        return {"accepted": accepted, "reason": reason, "entry": entry,
                "receipt_verified": bool(verified)}

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
        from . import arxiv
        return arxiv.search(params.get("query", ""), int(params.get("max_results", 10)),
                            params.get("category"))

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
            p95 = (landscape.get("ic_ir") or {}).get("p95")
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


def _passes_acceptance(result, z_threshold=3.0, beta_threshold=0.3, min_n=20):
    from .factor.evaluate import passes_acceptance
    return passes_acceptance(result, z_threshold, beta_threshold, min_n)


def _rpc_error(req_id, code, message, data=None):
    return json.dumps({"jsonrpc": "2.0", "id": req_id, "error": _error(code, message, data)},
                      ensure_ascii=False)


def _acquire_state_lock(state_root: str) -> None:
    """多会话防护：同一 stateRoot 只允许一个 bridge 进程（test 双消费/文件竞争防线）。
    锁 = PID 文件；PID 已死（stale）则接管。进程级（main 里调用），单测直调 Bridge 不受影响。

    原子性：先尝试 O_EXCL 独占创建——两个进程同时到达时只有一个能创建成功，
    消除「检查-存活-写入」三步之间的 TOCTOU 竞态；只在接管 stale 锁时才走删除+重建。
    """
    import re as _re
    import subprocess as _sp

    lock = Path(state_root) / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"pid": os.getpid(),
                          "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S")})
    # 快路径：独占创建（不存在时原子成功）
    try:
        with open(lock, "x", encoding="utf-8") as f:
            f.write(payload)
        return
    except FileExistsError:
        pass
    # 已有锁：检查持锁者是否存活
    try:
        old = json.loads(lock.read_text(encoding="utf-8"))
        pid = int(old.get("pid", 0))
        alive = False
        if pid > 0 and pid != os.getpid():
            if sys.platform == "win32":
                r = _sp.run(["tasklist", "/FI", f"PID eq {pid}"],
                            capture_output=True, text=True, timeout=10)
                # 词边界匹配：裸子串会把 PID 5 误配到内存列/标题里的任何含 5 数字
                alive = _re.search(rf"(?<!\d){pid}(?!\d)", (r.stdout or "")) is not None
            else:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
        if alive:
            raise SystemExit(
                f"stateRoot '{state_root}' 正被另一会话使用（PID={pid}，启动于 "
                f"{old.get('ts','?')}）。关闭那个会话或等待其退出后再启动；"
                f"确认其已崩溃则手动删除 {lock}")
    except (json.JSONDecodeError, ValueError, OSError):
        pass  # 锁损坏 → 接管
    # stale / 损坏 → 删除后重试独占创建一次（仍失败说明有人抢先，让位退出）
    try:
        lock.unlink()
    except OSError:
        pass
    try:
        with open(lock, "x", encoding="utf-8") as f:
            f.write(payload)
    except FileExistsError:
        raise SystemExit(
            f"stateRoot '{state_root}' 的锁在接管瞬间被另一进程抢先获取，本次启动让位退出")


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
    _acquire_state_lock(bridge.state_root)
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
