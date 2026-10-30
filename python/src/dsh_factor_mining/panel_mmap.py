# coding=utf-8
"""面板 mmap 共享缓存（bundle，2026-09-08）。

把 FactorEnv 的 (T,N) float64 矩阵按原始字节落盘为单个 .pbin，加载方用
np.memmap(mode='r') 只读映射——N 个进程共享同一份物理页，替代"每个
worker 把 npz 解压成私有副本"（生产实测 16 worker × ~1.9GB）。

严格等价纪律（效率编译层同款）：float64 原始布局，字节即内存，映射加载
值与内存 env 逐位相同；不做任何 dtype 降级（float32 之类的"优化"一律不
做——口径/deflation 可比性优先）。

文件对：<base>.pbin + <base>.pbin.meta.json。写序 bin 先、meta 后（两者
俱在 = 提交）；tmp + os.replace 原子。加载校验 total_bytes 与字段表，
任何异常由调用方回退 npz 路径（fail-open，正交门同款）。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

#: float64 字段（缺 amount 时自动跳过）；listed 以 uint8 落盘（bool 一字节）
_FLOAT_FIELDS = ("o", "h", "l", "c", "v")


def bundle_paths(base: str | os.PathLike) -> tuple[Path, Path]:
    base = Path(base)
    return base.with_suffix(base.suffix + ".pbin"), Path(str(base) + ".pbin.meta.json")


def save_bundle(base: str | os.PathLike, env, data_quality=None) -> None:
    """把 env 的矩阵原始字节写入 bundle。失败抛异常，由调用方决定回退。"""
    base = Path(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    fields: dict[str, dict] = {}
    chunks: list[np.ndarray] = []
    off = 0
    for name in _FLOAT_FIELDS:
        a = np.ascontiguousarray(getattr(env, name), dtype="<f8")
        fields[name] = {"offset": off, "shape": list(a.shape), "dtype": "float64"}
        chunks.append(a)
        off += a.nbytes
    if env.amount is not None:
        a = np.ascontiguousarray(env.amount, dtype="<f8")
        fields["amount"] = {"offset": off, "shape": list(a.shape), "dtype": "float64"}
        chunks.append(a)
        off += a.nbytes
    if env.listed is not None:
        a = np.ascontiguousarray(env.listed, dtype=np.uint8)
        fields["listed"] = {"offset": off, "shape": list(a.shape), "dtype": "uint8"}
        chunks.append(a)
        off += a.nbytes

    bin_p, meta_p = bundle_paths(base)
    tmp = base.parent / f".tmp-pbin-{os.urandom(6).hex()}"
    try:
        with open(tmp, "wb") as f:
            for a in chunks:
                f.write(memoryview(a).cast("B"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, bin_p)  # bin 在 = 数据在
        meta = {
            "dates": [str(d) for d in env.dates],
            "symbols": list(env.symbols),
            "calibration": asdict(env.calibration),
            "fields": fields,
            "total_bytes": off,
            # Phase 2（2026-09-09）：bridge 侧 mmap 快速装载时 data.load
            # 需要报告 dataQuality——bundle 一并携带，装载时还原
            "dataQuality": data_quality,
        }
        tmp_m = Path(str(tmp) + ".meta.json")
        tmp_m.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_m, meta_p)  # meta 在 = bundle 有效
    finally:
        for leftovers in (tmp, Path(str(tmp) + ".meta.json")):
            try:
                if leftovers.exists():
                    leftovers.unlink()
            except OSError:
                pass


def bundle_valid(base: str | os.PathLike) -> bool:
    """bin 与 meta 俱在且尺寸吻合才视为有效（半写状态 = 无效）。"""
    bin_p, meta_p = bundle_paths(base)
    if not (bin_p.exists() and meta_p.exists()):
        return False
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        return bin_p.stat().st_size == int(meta.get("total_bytes", -1))
    except Exception:
        return False


def load_bundle_mmap(base: str | os.PathLike):
    """只读映射加载 → FactorEnv（矩阵为 memmap，多进程共享物理页）。

    任何结构性异常直接抛出——调用方（worker）捕获后回退 npz 路径。"""
    from .factor.env import Calibration, FactorEnv

    bin_p, meta_p = bundle_paths(base)
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if bin_p.stat().st_size != int(meta.get("total_bytes", -1)):
        raise ValueError(f"panel bundle 尺寸不符: {bin_p}")

    def _rd(name: str) -> np.memmap:
        f = meta["fields"][name]
        return np.memmap(bin_p, dtype=f["dtype"], mode="r",
                         offset=f["offset"], shape=tuple(f["shape"]))

    o, h, l, c, v = (_rd(k) for k in _FLOAT_FIELDS)
    amount = _rd("amount") if "amount" in meta["fields"] else None
    listed = _rd("listed") if "listed" in meta["fields"] else None
    cal = None
    if meta.get("calibration"):
        known = set(Calibration.__dataclass_fields__)
        cal = Calibration(**{k: v2 for k, v2 in meta["calibration"].items()
                             if k in known})
    dates = pd.to_datetime(meta["dates"])
    env = FactorEnv(o, h, l, c, v, dates, meta["symbols"],
                    listed=listed, amount=amount, calibration=cal)
    env.panel_meta = meta  # bridge 快速装载读 dataQuality 等附属信息
    return env
