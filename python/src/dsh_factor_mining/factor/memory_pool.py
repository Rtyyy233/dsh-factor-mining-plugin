# coding=utf-8
"""有界因子记忆池：数值指纹 + 结构签名的重复检测（资源与挖掘次数无关）。

设计（进化算法种群思想 + 老项目双库思想融合）：
- active set（容量 64，可配）：评估通过/待审的强因子，按 IC_IR 强换弱滚动
- falsified set（容量 32，可配）：正式证伪因子的簇代表（防重复挖坟），LRU 滚动
- 指纹 = 不重叠采样日的 F 子矩阵（float32，约 0.9MB/因子）——evaluate 本来就算出 F，
  顺手采样存储，零额外因子执行
- 全史（trail/大库）只存文字，不参与数值比对——审计职责不是去重负担

对表成本：池有界（96）→ 新因子对表 = 常数成本（粗筛拉平相关 + 精算逐截面相关）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

from ..discipline import signature_similarity, source_fingerprint, structure_signature

DEFAULT_POOL_CONFIG = {
    "active_capacity": 64,
    "falsified_capacity": 32,
    "duplicate_corr": 0.90,   # 数值指纹相关阈值（疑似重复）
    "method_sig": 0.70,       # AST 签名相似阈值（方法疑似重复）
}


def _cs_corr_mean(A: np.ndarray, B: np.ndarray) -> float:
    """逐截面秩相关的时序均值（对表精算）。A/B 形状 (T_s, N)。"""
    import pandas as pd
    ra = pd.DataFrame(A).rank(axis=1, pct=True).values
    rb = pd.DataFrame(B).rank(axis=1, pct=True).values
    corrs = []
    for t in range(A.shape[0]):
        m = np.isfinite(ra[t]) & np.isfinite(rb[t])
        if m.sum() < 30 or ra[t][m].std() == 0 or rb[t][m].std() == 0:
            continue
        corrs.append(float(np.corrcoef(ra[t][m], rb[t][m])[0, 1]))
    return float(np.mean(corrs)) if corrs else float("nan")


def _flat_corr(A: np.ndarray, B: np.ndarray) -> float:
    """拉平相关（粗筛，O(T_s·N) 点积）。"""
    m = np.isfinite(A) & np.isfinite(B)
    if m.sum() < 30:
        return float("nan")
    a, b = A[m], B[m]
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


class MemoryPool:
    """有界双池。存储：stateRoot/pool/{active,falsified}.json + pool/fp/*.npy"""

    def __init__(self, state_root: str | Path, config: dict[str, Any] | None = None):
        self.root = Path(state_root)
        self.fp_dir = self.root / "pool" / "fp"
        self.cfg = {**DEFAULT_POOL_CONFIG, **(config or {})}
        self._active = self._load("active")
        self._falsified = self._load("falsified")

    # ---- 存储 ----
    def _path(self, which: str) -> Path:
        return self.root / "pool" / f"{which}.json"

    def _load(self, which: str) -> list[dict[str, Any]]:
        p = self._path(which)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, which: str, entries: list[dict[str, Any]]) -> None:
        p = self._path(which)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 原子写：崩溃不留半截 JSON（半截会让 _load 兜底成空列表=整池丢失）
        import os as _os
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
        _os.replace(str(tmp), str(p))

    def _fp_path(self, key: str) -> Path:
        return self.fp_dir / f"{key}.npy"

    def _load_fp(self, key: str) -> np.ndarray | None:
        p = self._fp_path(key)
        if not p.exists():
            return None
        try:
            return np.load(p, allow_pickle=False)
        except Exception:
            return None

    def _store_fp(self, key: str, fp: np.ndarray) -> None:
        self.fp_dir.mkdir(parents=True, exist_ok=True)
        np.save(self._fp_path(key), fp.astype(np.float32))

    def _evict_fp(self, key: str) -> None:
        try:
            self._fp_path(key).unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 指纹 ----
    @staticmethod
    def sample_fingerprint(F, sig_idx) -> np.ndarray:
        """不重叠采样日的 F 子矩阵（float32）——evaluate 已算出 F，零额外执行。"""
        return np.asarray(F, dtype=np.float32)[sig_idx]

    # ---- 对表 ----
    def check(self, F, sig_idx, source: str) -> dict[str, Any]:
        """新因子对双池对表。返回 duplicate_suspect / method_suspect（提醒不是否决）。"""
        new_fp = self.sample_fingerprint(F, sig_idx)
        new_sig = structure_signature(source)
        result: dict[str, Any] = {"duplicate_suspect": None, "method_suspect": None}

        for pool_name, entries in (("active", self._active), ("falsified", self._falsified)):
            best_num, best_entry = None, None
            best_sig, best_sig_entry = None, None
            for e in entries:
                # 粗筛（拉平相关）→ 精算（逐截面）
                old_fp = self._load_fp(e["key"])
                if old_fp is not None and old_fp.shape == new_fp.shape:
                    flat = _flat_corr(new_fp, old_fp)
                    if np.isfinite(flat) and abs(flat) > 0.5:
                        cs = _cs_corr_mean(new_fp, old_fp)
                        if np.isfinite(cs) and (best_num is None or abs(cs) > abs(best_num)):
                            best_num, best_entry = cs, e
                sim = signature_similarity(new_sig, e.get("signature") or {"set": [], "seq": [], "n": 0})
                if sim > (self.cfg["method_sig"] - 0.1) and (best_sig is None or sim > best_sig):
                    best_sig, best_sig_entry = sim, e
            if best_num is not None and abs(best_num) >= self.cfg["duplicate_corr"]:
                result["duplicate_suspect"] = {
                    "pool": pool_name, "corr": round(best_num, 4),
                    "name": best_entry.get("name"),
                    "note": (f"与 {pool_name} 池因子「{best_entry.get('name')}」截面相关 "
                             f"{best_num:+.3f}（≥{self.cfg['duplicate_corr']}）——疑似重复探索，"
                             "先查 factor_query_paths / 论证本质差异再继续"),
                }
                break
            if best_sig is not None and best_sig >= self.cfg["method_sig"] and not result["method_suspect"]:
                result["method_suspect"] = {
                    "pool": pool_name, "similarity": round(best_sig, 3),
                    "name": best_sig_entry.get("name"),
                    "note": (f"与 {pool_name} 池因子「{best_sig_entry.get('name')}」结构签名相似 "
                             f"{best_sig:.2f}——方法维度疑似重复（同一公式换数据/字段）"),
                }
        return result

    # ---- 入池 ----
    def offer_active(self, F, sig_idx, source: str, name: str, ic_ir: float,
                     fingerprint: str | None = None) -> dict[str, Any]:
        """强因子入 active 池（强换弱滚动，容量有界）。"""
        key = source_fingerprint(source)
        if any(e["key"] == key for e in self._active):
            return {"admitted": True, "replaced": None, "note": "已在池中（更新）"}
        if any(e["key"] == key for e in self._falsified):
            return {"admitted": False, "note": "该因子在证伪池中，拒绝入强池"}
        if not np.isfinite(ic_ir):
            return {"admitted": False, "note": "IC_IR 非有限"}
        entry = {"key": key, "name": name, "ic_ir": float(ic_ir),
                 "fingerprint": fingerprint, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "signature": structure_signature(source)}
        replaced = None
        if len(self._active) >= self.cfg["active_capacity"]:
            self._active.sort(key=lambda e: e.get("ic_ir", -np.inf))
            weakest = self._active[0]
            if weakest.get("ic_ir", -np.inf) >= ic_ir:
                return {"admitted": False, "note": f"弱于池内最弱（{weakest.get('ic_ir')}），不入"}
            replaced = weakest.get("name")
            self._active = self._active[1:]
            self._evict_fp(weakest["key"])
        self._active.append(entry)
        self._store_fp(key, self.sample_fingerprint(F, sig_idx))
        self._save("active", self._active)
        return {"admitted": True, "replaced": replaced}

    def offer_falsified(self, source: str, name: str, F=None, sig_idx=None) -> dict[str, Any]:
        """正式证伪因子入 falsified 池（record_explored 时调用；LRU 容量滚动）。"""
        key = source_fingerprint(source)
        self._falsified = [e for e in self._falsified if e["key"] != key]
        entry = {"key": key, "name": name,
                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "signature": structure_signature(source)}
        if F is not None and sig_idx is not None:
            self._store_fp(key, self.sample_fingerprint(F, sig_idx))
        if len(self._falsified) >= self.cfg["falsified_capacity"]:
            evicted = self._falsified.pop(0)
            self._evict_fp(evicted["key"])
        self._falsified.append(entry)
        self._save("falsified", self._falsified)
        return {"admitted": True}

    def stats(self) -> dict[str, Any]:
        return {"active": len(self._active), "falsified": len(self._falsified),
                "capacity": {"active": self.cfg["active_capacity"],
                             "falsified": self.cfg["falsified_capacity"]},
                "config": {k: v for k, v in self.cfg.items()
                           if k in ("duplicate_corr", "method_sig")}}
