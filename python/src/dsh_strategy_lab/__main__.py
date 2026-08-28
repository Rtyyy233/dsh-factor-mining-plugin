# coding=utf-8
"""CLI 入口（python -m dsh_strategy_lab；形态 S6 拍板：CLI 优先，非 DSH、
非 MCP）。

唯一评估路径：性能数字只从这里出（锁 5——每次评估自动记账，计数不
依赖自觉）。子命令：

- ``build-env``：面板 spec（复用因子层 data.adapters）→ npz（禁运配套：
  面板加载器私有，后续 evaluate/submit 只走 npz）。
- ``evaluate``：development / walk_forward / test 评估（审计链全量 +
  轻 placebo + 增量报告）；test 成功返回后主进程消费一次性锁。
- ``submit``：事务化准入——worker 权威测量（m≥60 placebo + G1′-G4′ +
  robustness）→ **全过才写 registry**；拒收也记账（procedural/
  substantive reject_kind + 理由，供后续策略避坑）+ 计试验；超时 =
  零写入不烧指纹。
- ``factor-select``：因子 select 比较的记录（2026-08-28 用户决策：
  策略层要有 select 的记录）——对因子 registry 全部 accepted 条目在
  select 窗跑一轮 selection 评估（账目照常进因子 trail）+ 可选
  衰减/稳健性体检，快照原子落策略层 state root；--choose 把特征
  挑选决策本身留痕。
- ``trail`` / ``status``：账本与状态查看。

响应为紧凑投影（防截断——尾线 WS-A 教训前置）；--out-file 可存全量。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import __version__
from .contract import StrategyError, compile_strategy
from .fingerprint import strategy_fingerprint
from .simulator import CostModel
from .state import (
    append_registry_entry,
    atomic_write_json,
    check_factor_refs,
    check_test_lock,
    consume_test_lock,
    read_registry,
    resolve_state_root,
)
from .trail import append_trail, n_trials, strategy_ledger

WORKER_TIMEOUT_S = 300.0


class CliError(RuntimeError):
    pass


# ---------------------------------------------------------------- env 准备

def _spec_from_dict(d: dict):
    from dsh_factor_mining.data.adapters import (
        EnvironmentSpec, MappingSpec, SourceSpec)

    src = SourceSpec(**{k: v for k, v in (d.get("source") or {}).items()
                        if k in SourceSpec.__dataclass_fields__})
    mp = MappingSpec(**{k: v for k, v in (d.get("mapping") or {}).items()
                        if k in MappingSpec.__dataclass_fields__})
    known = EnvironmentSpec.__dataclass_fields__
    others = {k: v for k, v in d.items()
              if k in known and k not in ("source", "mapping")}
    return EnvironmentSpec(source=src, mapping=mp, **others)


def build_env_npz(env_spec_path: str, out_path: str) -> dict:
    """面板 spec → normalize → FactorEnv → npz（env 只由 harness 构造）。"""
    from dsh_factor_mining.data.adapters import (
        build_factor_env, normalize_environment)
    from dsh_factor_mining.worker import write_env_npz

    spec = _spec_from_dict(json.loads(Path(env_spec_path).read_text(
        encoding="utf-8")))
    mats = normalize_environment(spec)
    env = build_factor_env(mats, spec.calibration)
    write_env_npz(out_path, env)
    return {"out": str(out_path), "T": env.T, "N": env.N,
            "first": str(env.dates[0].date()), "last": str(env.dates[-1].date())}


def _resolve_env_npz(args) -> str:
    if getattr(args, "env_npz", None):
        return args.env_npz
    if not getattr(args, "env_spec", None):
        raise CliError("需要 --env-npz 或 --env-spec（面板 → 先跑 build-env）")
    root = resolve_state_root(getattr(args, "state_root", None))
    spec_raw = Path(args.env_spec).read_text(encoding="utf-8")
    h = hashlib.sha256(spec_raw.encode("utf-8")).hexdigest()[:16]
    out = root / "cache" / f"env-{h}.npz"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        build_env_npz(args.env_spec, str(out))
    return str(out)


# ---------------------------------------------------------------- worker 调度

def _kill_process_tree(pid: int) -> None:
    """Windows 杀整树（孙进程残留会继续烧 CPU，加剧并行会话竞争）；
    POSIX 退化为杀直接子进程。"""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:
            pass
    else:
        try:
            import signal
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def _run_worker(method: str, npz_path: str, source: str, params: dict,
                timeout_s: float = WORKER_TIMEOUT_S) -> dict:
    """spawn worker 子进程（锁 2）；超时 = 二维归因（P1/D1）+ 零写入。

    超时判据：CPU 密集 → 四要素错误（修实现不换假设）；CPU 饿死 →
    infra_failure（机器超订，重试+降并行，不是策略的错）。
    run_dir 加 pid+uuid 后缀（并行 P0）：秒级时间戳在并发 CLI 下同秒碰撞，
    两个会话共用 request.json/result.json 互相覆盖。"""
    from dsh_factor_mining.procinfo import (
        attach_worker_limits,
        cpu_seconds,
        detach_worker_limits,
        omp_quiet_env,
        posix_limit_preexec,
        starvation_verdict,
        worker_mem_bytes,
    )

    root = resolve_state_root(params.get("state_root"))
    run_dir = root / "runs" / (
        time.strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    run_dir.mkdir(parents=True, exist_ok=True)
    req_path = run_dir / "request.json"
    res_path = run_dir / "result.json"
    req_path.write_text(json.dumps(
        {"npzPath": str(Path(npz_path).resolve()), "method": method,
         "source": source, "params": params},
        ensure_ascii=False), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "dsh_strategy_lab.worker",
         "--request", str(req_path), "--result", str(res_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=omp_quiet_env(),
        preexec_fn=posix_limit_preexec(cpu_s=timeout_s,
                                       mem_bytes=worker_mem_bytes()))
    job = attach_worker_limits(proc, cpu_s=timeout_s,
                               mem_bytes=worker_mem_bytes())
    try:
        proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # 杀之前读 CPU 秒（进程退出后句柄失效）→ 二维归因
        cpu_s = cpu_seconds(proc.pid)
        verdict = starvation_verdict(cpu_s, timeout_s)
        _kill_process_tree(proc.pid)
        try:
            proc.communicate(timeout=15)
        except Exception:
            pass
        detach_worker_limits(job)
        if verdict["class"] == "starved":
            raise CliError(
                f"worker 超时（墙钟 {timeout_s:.0f}s，实际仅获得 "
                f"{verdict['cpu_s']:.1f}s CPU，占比 {verdict['ratio']:.0%}）——"
                "机器超订（并行会话/其他进程挤占 CPU）所致，不是策略实现慢。"
                "本次调用零写入（registry/trail/test 锁均未动，不烧指纹）。"
                "修法：直接重试同一策略即可（未计入账本）；若反复出现，"
                "降低并行（多会话/多 CLI 加总 ≤ 核数）。这是基础设施事件"
                "——不修实现、不换假设，也不作为对策略的判定。")
        cpu_note = (f"（实测 CPU {verdict['cpu_s']:.1f}s / 墙钟 {timeout_s:.0f}s"
                    f"——{verdict['note']}）" if verdict["ratio"] is not None else "")
        raise CliError(
            f"worker 超时（>{timeout_s:.0f}s）——{method} 的 worker 子进程"
            "已终止并清理；CLI 本体未受影响，无需等待恢复，可立即重试。"
            "本次调用零写入（registry/trail/test 锁均未动，不烧指纹）。"
            f"最可能根因：fit/apply 单次计算超过 {timeout_s:.0f}s，典型是 "
            "per-bar Python 循环或逐资产重算；修法 = 向量化（矩阵运算/"
            "unstack 宽表/预计算截面）。这是基础设施事件，不是对策略或"
            f"研究方向的判定——修实现，不换假设。{cpu_note}")
    detach_worker_limits(job)
    if not res_path.exists():
        tail = (err or "")[-400:]
        raise CliError(
            f"worker 无输出（exit={proc.returncode}）——{method} 未产生"
            "结果文件，本次调用零写入。stderr 尾部："
            f"{tail or '(空)'}——先本地复现编译错误再重试")
    out = json.loads(res_path.read_text(encoding="utf-8"))
    if not out.get("ok"):
        err = out.get("error") or {}
        raise CliError(
            f"worker 执行失败（{err.get('type')}: {err.get('message')}）"
            "——契约/运行错误，本次调用零写入；按错误信息修策略源后重试")
    return out["result"]


# ---------------------------------------------------------------- 因子引用

def _resolve_factor_refs(factor_refs_path: str | None, params: dict,
                         state_root, factor_state_root) -> dict:
    """refs 解析：JSON [{source_hash, horizon}] → 交叉核对 + 抽出源码
    （被动基线用）。返回 {ok, detail, sources}。"""
    if not factor_refs_path:
        return {"ok": False, "detail": {"ok": False, "note": "未提供 --factor-refs"},
                "sources": {}}
    refs = json.loads(Path(factor_refs_path).read_text(encoding="utf-8"))
    check = check_factor_refs(refs, factor_state_root)
    sources = {}
    if check["ok"]:
        freg = {}
        try:
            from dsh_factor_mining.state import read_registry as _rr
            freg = {e.get("source_hash"): e
                    for e in _rr(factor_state_root) if isinstance(e, dict)}
        except Exception:    # noqa: BLE001
            freg = {}
        for r in refs:
            e = freg.get(r.get("source_hash")) or {}
            if e.get("source"):
                sources[r["source_hash"]] = e["source"]
    return {"ok": bool(check["ok"]), "detail": check, "sources": sources,
            "refs": refs}


# ---------------------------------------------------------------- 紧凑投影

def _compact_metrics(m: dict) -> dict:
    if not isinstance(m, dict):
        return m
    keys = ("sharpe", "ann_return", "ann_vol", "max_drawdown", "n_trades",
            "n_trade_days", "avg_turnover_per_trade_day", "total_return",
            "total_fees")
    return {k: m.get(k) for k in keys if k in m}


def _compact(result: dict) -> dict:
    """紧凑投影（防截断）：丢 equity/daily_returns/base_path/trades 明细，
    保指标/门判定/账本键。--out-file 存全量。"""
    out = {}
    for k, v in result.items():
        if k == "sim":
            out["sim_metrics"] = _compact_metrics(v.get("metrics"))
            out["n_trades"] = len(v.get("trades") or [])
        elif k == "audit":
            out["audit"] = {kk: (vv if not isinstance(vv, dict) or kk != "base_path"
                                 else "<path-dropped>")
                            for kk, vv in v.items() if kk != "base_path"}
        elif k == "increment":
            out["increment"] = ({
                "strategy": _compact_metrics(v.get("strategy")),
                "baseline": _compact_metrics(v.get("baseline")),
                "delta_sharpe": v.get("delta_sharpe"),
                "delta_ann_return": v.get("delta_ann_return"),
                "block_bootstrap": v.get("block_bootstrap"),
            } if isinstance(v, dict) else v)
        elif k == "gate_verdicts":
            out["gate_verdicts"] = {g: {"pass": ok, "reason": msg}
                                    for g, (ok, msg) in v.items()}
        elif k == "robustness":
            out["robustness"] = {kk: vv for kk, vv in v.items()
                                 if kk != "points"} | {
                "n_points_reported": len(v.get("points") or [])}
        else:
            out[k] = v
    return out


def _emit(payload: dict, out_file: str | None) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))
    if out_file:
        Path(out_file).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")


# ---------------------------------------------------------------- 子命令

def _common_params(args) -> dict:
    params = {"state_root": getattr(args, "state_root", None),
              "seed": getattr(args, "seed", 42) or 42}
    if getattr(args, "cost_file", None):
        cm = json.loads(Path(args.cost_file).read_text(encoding="utf-8"))
        known = CostModel.__dataclass_fields__
        params["cost_model"] = {k: v for k, v in cm.items() if k in known}
    return params


def cmd_evaluate(args) -> int:
    source = Path(args.source_file).read_text(encoding="utf-8")
    compile_strategy(source)            # 早期契约校验（编译错误零写入）
    npz = _resolve_env_npz(args)
    params = _common_params(args)
    params.update({"stage": args.stage})
    if args.wf_config:
        params["wf_config"] = json.loads(
            Path(args.wf_config).read_text(encoding="utf-8"))
    refs = _resolve_factor_refs(args.factor_refs, params,
                                params.get("state_root"),
                                args.factor_state_root)
    if refs["ok"]:
        params["factor_sources"] = refs["sources"]
        params["factor_refs"] = refs["refs"]
    wf_cfg = params.get("wf_config") or {}
    cm = CostModel(**(params.get("cost_model") or {}))
    fp = strategy_fingerprint(
        refs.get("refs") or [], source, _params_hash_input(args),
        wf_cfg, cm.version)
    result = _run_worker("strategy.evaluate", npz, source, params,
                         timeout_s=float(args.timeout or WORKER_TIMEOUT_S))
    # 自动记账（development/walk_forward 计试验；test 记录但不计）
    sim = result.get("sim") or {}
    trail_entry = {"stage": args.stage, "fingerprint": fp,
                   "metrics": _compact_metrics(sim.get("metrics")),
                   "cost_model_version": result.get("cost_model_version")}
    try:
        from .gates import trade_minhash
        trail_entry["trade_mh"] = trade_minhash(sim.get("trades") or [])
    except Exception:    # noqa: BLE001 — trade_mh 缺失按独立试验计（保守）
        pass
    append_trail(trail_entry, root=params.get("state_root"))
    if args.stage == "test":
        consume_test_lock(fp["key"], root=params.get("state_root"))
    _emit({"ok": True, "stage": args.stage, "fingerprint": fp["key"],
           "n_trials": n_trials(params.get("state_root")),
           "result": _compact(result)}, args.out_file)
    return 0


def _params_hash_input(args) -> dict:
    if getattr(args, "params_file", None):
        return json.loads(Path(args.params_file).read_text(encoding="utf-8"))
    return {}


def cmd_submit(args) -> int:
    state_root = args.state_root
    source = Path(args.source_file).read_text(encoding="utf-8")
    compile_strategy(source)
    refs = _resolve_factor_refs(args.factor_refs, {}, state_root,
                                args.factor_state_root)
    params = _common_params(args)
    wf_cfg = json.loads(Path(args.wf_config).read_text(
        encoding="utf-8")) if args.wf_config else {}
    cm = CostModel(**(params.get("cost_model") or {}))
    fp = strategy_fingerprint(refs.get("refs") or [], source,
                              _params_hash_input(args), wf_cfg, cm.version)
    name = args.name or f"strategy-{fp['key'][:8]}"

    def _registry_write(accepted: bool, reason: str, reject_kind, extra: dict):
        entry = {"name": name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "accepted": accepted, "reason": reason,
                 "reject_kind": reject_kind, "fingerprint": fp,
                 **extra}
        # 锁内读-追加-写（P5 压测补丁：锁外读有丢更新窗口）
        append_registry_entry(entry, root=state_root)
        return entry

    if not refs["ok"]:
        _registry_write(False, (refs["detail"].get("note")
                                or "factor_refs 校验未过"), "procedural",
                        {"factor_refs_check": refs["detail"]})
        _emit({"ok": False, "stage": "submit", "name": name,
               "fingerprint": fp["key"],
               "reason": refs["detail"].get("note"),
               "reject_kind": "procedural",
               "note": "refs 无效 = 程序性拒收（无评估发生，不计试验）"},
              args.out_file)
        return 1
    params["factor_sources"] = refs["sources"]
    params["factor_refs"] = refs["refs"]
    params["wf_config"] = wf_cfg
    # N_eff 计价需要账本（含历史 trade_mh）
    params["ledger"] = strategy_ledger(state_root)
    npz = _resolve_env_npz(args)
    result = _run_worker("strategy.submit", npz, source, params,
                         timeout_s=float(args.timeout or WORKER_TIMEOUT_S))
    verdicts = result.get("gate_verdicts") or {}
    accepted = bool(result.get("accepted"))
    reason = next((msg for ok, msg in verdicts.values() if not ok),
                  "全门通过") if verdicts else result.get(
        "audit", {}).get("note", "G0 未过")
    reject_kind = None if accepted else (
        "procedural" if not verdicts.get("g0", (True,))[0] else "substantive")
    entry = _registry_write(accepted, reason, reject_kind, {
        "gates": {g: {"pass": ok, "reason": msg}
                  for g, (ok, msg) in verdicts.items()},
        "n_eff": result.get("n_eff"), "n_total": result.get("n_total"),
        "metrics": _compact_metrics((result.get("sim") or {}).get("metrics")),
        "robustness": result.get("robustness"),
    })
    # submit 必经 wf 语义：按 walk_forward 计一次试验（拒收也计——
    # 可选停时防御；超时/编译错误在上面已被零写入拦下）
    sim = result.get("sim") or {}
    append_trail({"stage": "walk_forward", "fingerprint": fp,
                  "submit": {"accepted": accepted, "name": name},
                  "metrics": _compact_metrics(sim.get("metrics")),
                  "trade_mh": result.get("trade_mh"),
                  "n_eff_at_write": result.get("n_eff")},
                 root=state_root)
    _emit({"ok": accepted, "stage": "submit", "name": name,
           "fingerprint": fp["key"], "accepted": accepted,
           "reject_kind": reject_kind, "reason": reason,
           "n_eff": result.get("n_eff"),
           "n_trials": n_trials(state_root),
           "result": _compact(result)}, args.out_file)
    return 0 if accepted else 1


def cmd_trail(args) -> int:
    entries = read_json_list_for_trail(args.state_root)
    last = entries[-args.last:] if args.last else entries

    def _proj(e):
        out = {k: e.get(k) for k in
               ("ts", "last_ts", "stage", "submit", "metrics", "n_eff_at_write")}
        if isinstance(e.get("fingerprint"), dict):
            out["fingerprint"] = e["fingerprint"].get("key")
        return out

    _emit({"ok": True, "n_total": len(entries),
           "n_counted": n_trials(args.state_root),
           "entries": [_proj(e) for e in last]}, args.out_file)
    return 0


def read_json_list_for_trail(state_root):
    from .state import path_for, read_json_list as _rjl

    return _rjl(path_for("trail", state_root))


def cmd_status(args) -> int:
    reg = read_registry(args.state_root)
    lock = check_test_lock(args.state_root)
    _emit({"ok": True, "version": __version__,
           "state_root": str(resolve_state_root(args.state_root).resolve()),
           "n_trials": n_trials(args.state_root),
           "registry": {"total": len(reg),
                        "accepted": sum(1 for e in reg
                                        if isinstance(e, dict)
                                        and e.get("accepted")),
                        "rejected": sum(1 for e in reg
                                        if isinstance(e, dict)
                                        and not e.get("accepted"))},
           "test_lock": lock}, args.out_file)
    return 0


def cmd_factor_select(args) -> int:
    """因子 select 记录（用户决策 2026-08-28：策略开发层要有 select 的记录）。

    一次调用 = 一轮 select 比较：对因子 registry 全部 accepted 条目在
    因子层 stateRoot 上跑 stage=selection 评估（账目照常进因子 trail，
    同键去重更新），快照原子写入策略层 state root 的 factor_select.json
    ——特征挑选的证据与决策（--choose）从此留痕，不再是一次性脚本输出。

    纪律不变：一轮比完就定；select 数字难看不许回头改因子再比（改了
    = 新因子回 dev 重新排队）。--choose-only 只补记决策、不重跑比较。"""
    import time as _time

    sroot = resolve_state_root(args.state_root)
    snap_path = sroot / "factor_select.json"

    if args.choose_only:
        if not snap_path.exists():
            raise CliError("--choose-only 需要已有 factor_select 快照——"
                           "先不带该参数跑一轮比较")
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        snap["chosen"] = list(args.choose or [])
        snap["chosen_ts"] = _time.strftime("%Y-%m-%dT%H:%M:%S")
        atomic_write_json(snap_path, snap)
        _emit({"ok": True, "chosen": snap["chosen"],
               "ts": snap["chosen_ts"]}, args.out_file)
        return 0

    if not args.factor_state_root:
        raise CliError("需要 --factor-state-root（因子层状态根，只读+selection 评估）")
    from dsh_factor_mining.bridge import Bridge
    from dsh_factor_mining.state import read_registry as read_factor_registry

    fb = Bridge(state_root=str(args.factor_state_root),
                execution_mode="in_process")
    env_id = args.env_id
    if env_id is None:
        cfg = json.loads((Path(args.factor_state_root)
                          / "data-config.json").read_text(encoding="utf-8"))
        envs = list((cfg.get("environments") or {}).keys())
        if not envs:
            raise CliError("因子层 data-config 无环境——先在因子层配置并 data.load")
        env_id = envs[0]
    fb.dispatch("data.load", {"envId": env_id})

    cands = [e for e in read_factor_registry(args.factor_state_root)
             if isinstance(e, dict) and e.get("accepted")
             and isinstance(e.get("source"), str) and "def factor" in e["source"]]
    if not cands:
        raise CliError("因子 registry 无 accepted 条目——无从比较")

    from .feature_diligence import battery
    from dsh_factor_mining.worker import _compile

    entries = []
    for i, e in enumerate(cands):
        name = str(e.get("name"))
        try:
            r = fb.dispatch("factor.evaluate", {"envId": env_id,
                                                "stage": "selection",
                                                "source": e["source"]})
            topn = r.get("topn") or {}
            bb = topn.get("block_bootstrap") or {}
            tracks = e.get("tracks") if isinstance(e.get("tracks"), dict) else {}
            ic_ok = bool((tracks.get("ic") or {}).get("accepted"))
            tail_ok = bool((tracks.get("tail") or {}).get("accepted"))
            item = {
                "name": name,
                "track": "dual" if (ic_ok and tail_ok) else ("tail" if tail_ok else "ic"),
                "hash12": str(e.get("source_hash"))[:12],
                "train_ic_ir": e.get("ic_ir_train"),
                "sel_ic_ir": r.get("ic_ir"),
                "sel_net_annual": topn.get("net_annual"),
                "sel_gross_annual": topn.get("gross_annual"),
                "sel_turn_avg": topn.get("turn_avg"),
                "sel_boot_z": bb.get("z"),
            }
            if args.battery:
                F = _compile(e["source"])(fb.envs[env_id])
                item["battery"] = battery(F, fb.envs[env_id])
            entries.append(item)
            print(f"[{i + 1}/{len(cands)}] {name}: "
                  f"sel_ic_ir={r.get('ic_ir')}",
                  file=sys.stderr, flush=True)
        except Exception as ex:  # noqa: BLE001 — 单因子失败不烧整轮
            entries.append({"name": name, "error": f"{type(ex).__name__}: {ex}"[:200]})

    snap = {
        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "factor_state_root": str(Path(args.factor_state_root).resolve()),
        "env_id": env_id,
        "n_candidates": len(cands),
        "battery": bool(args.battery),
        "entries": entries,
        "chosen": list(args.choose or []),
        "discipline": ("一轮比完就定；不因 select 数字回头改因子再比（改 = "
                       "新因子回 dev 重新计价）；后续特征挑选以本快照为据"),
    }
    sroot.mkdir(parents=True, exist_ok=True)
    atomic_write_json(snap_path, snap)
    _emit({"ok": True, "ts": snap["ts"], "n_candidates": len(cands),
           "chosen": snap["chosen"],
           "snapshot": str(snap_path),
           "entries": [{"name": x.get("name"), "track": x.get("track"),
                        "sel_ic_ir": x.get("sel_ic_ir"),
                        "sel_net_annual": x.get("sel_net_annual"),
                        **({"half_life": (x.get("battery") or {}).get("half_life_days")}
                           if x.get("battery") else {})}
                       for x in entries]}, args.out_file)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="dsh_strategy_lab",
        description="策略层 harness CLI（唯一评估路径）")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def _common(sp):
        sp.add_argument("--state-root", default=None,
                        help=".strategy-lab 根（默认 cwd/.strategy-lab 或 "
                             "DSH_STRATEGY_LAB_STATE_ROOT）")
        sp.add_argument("--factor-state-root", default=None,
                        help="因子层状态根（只读——factor registry 交叉核对）")
        sp.add_argument("--out-file", default=None,
                        help="全量结果落盘路径（响应打印紧凑投影）")

    sp = sub.add_parser("build-env", help="面板 spec → env npz")
    sp.add_argument("--env-spec", required=True)
    sp.add_argument("--out", required=True)
    _common(sp)
    sp.set_defaults(func=lambda a: (_emit(build_env_npz(
        a.env_spec, a.out), a.out_file), 0)[1])

    sp = sub.add_parser("evaluate", help="development/walk_forward/test 评估")
    sp.add_argument("--source-file", required=True)
    sp.add_argument("--env-npz")
    sp.add_argument("--env-spec")
    sp.add_argument("--stage", default="development",
                    choices=["development", "walk_forward", "test"])
    sp.add_argument("--factor-refs", default=None,
                    help="JSON [{source_hash, horizon}]（增量基线/test 需要）")
    sp.add_argument("--params-file")
    sp.add_argument("--wf-config")
    sp.add_argument("--cost-file")
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--timeout", type=float, default=None)
    _common(sp)
    sp.set_defaults(func=cmd_evaluate)

    sp = sub.add_parser("submit", help="事务化准入（全过才写 registry）")
    sp.add_argument("--source-file", required=True)
    sp.add_argument("--env-npz")
    sp.add_argument("--env-spec")
    sp.add_argument("--factor-refs", required=True)
    sp.add_argument("--params-file")
    sp.add_argument("--wf-config")
    sp.add_argument("--cost-file")
    sp.add_argument("--seed", type=int, default=42)
    sp.add_argument("--timeout", type=float, default=None)
    sp.add_argument("--name", default=None)
    _common(sp)
    sp.set_defaults(func=cmd_submit)

    sp = sub.add_parser(
        "factor-select",
        help="因子 select 比较 → 策略层留痕快照（+可选衰减/稳健性体检）")
    sp.add_argument("--env-id", default=None,
                    help="因子层 envId（默认取 data-config 第一个环境）")
    sp.add_argument("--battery", action="store_true",
                    help="附特征体检：滞后 IC 衰减剖面 + 年度/滚动/bootstrap 稳健性")
    sp.add_argument("--choose", nargs="*", default=None,
                    help="记录选定的特征子集（决策留痕；--choose-only 可后补）")
    sp.add_argument("--choose-only", action="store_true",
                    help="只更新既有快照的 chosen 字段，不重跑比较")
    _common(sp)
    sp.set_defaults(func=cmd_factor_select)

    sp = sub.add_parser("trail", help="评估账本（最近 N 条）")
    sp.add_argument("--last", type=int, default=10)
    _common(sp)
    sp.set_defaults(func=cmd_trail)

    sp = sub.add_parser("status", help="状态汇总")
    _common(sp)
    sp.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    try:
        return int(args.func(args))
    except (CliError, StrategyError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
