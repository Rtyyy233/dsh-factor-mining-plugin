# Factor Authoring Guide | 因子写法指南

How to write `factor(env)` functions for the dsh-factor-mining engine — the API contract, PIT-safe vs. dangerous patterns, and vectorization rules. 中文为主要工作语言，关键术语附英文。

---

## 1. The contract | 契约

```python
def factor(env) -> np.ndarray:
    ...
```

- **Input**: a PIT-safe environment object `env` (see §2)
- **Output**: `(T, N)` float matrix — the factor value for every (date, symbol); row `t` must depend **only** on data up to `t`
- The engine verifies causality mechanically (noise-perturbation + NaN-truncation); a factor that reads future data is rejected before evaluation — no exceptions

**Registered diagnostics run on your factor automatically**: rank-IC on non-overlapping signal days, column-permutation z, deflated p, decay, beta exposure, top-N net, sensitivity of the train-region boundary. You write the factor; the engine owns verification.

---

## 2. The `env` object | 环境字段

All matrices are `(T, N)` float64 NumPy arrays aligned to `env.dates` (list of timestamps) and `env.symbols` (list of ids):

| Field | Meaning | PIT note |
|---|---|---|
| `env.o / env.h / env.l / env.c / env.v` | open / high / low / close / volume | known at day close |
| `env.amount` | turnover (or `None`) | known at day close |
| `env.listed` | `(T, N)` bool tradability mask | point-in-time |
| `env.dates`, `env.symbols`, `env.T`, `env.N` | index metadata | — |
| `env.calibration` | market caliber (horizon, cost, execution, regions) | read-only reference |

Wrap arrays in `pd.DataFrame(env.c)` to get dates×symbols frames for pandas operations.

---

## 3. PIT-safe vs. dangerous patterns | 安全与危险写法

**SAFE（只用过去）**:

```python
import pandas as pd
import numpy as np

def factor(env):
    c = pd.DataFrame(env.c)              # (T, N)
    mom = c / c.shift(20) - 1.0          # ✓ value at t uses data ≤ t
    vol = c.pct_change().rolling(60).std()   # ✓ trailing window
    z = (mom - vol)                       # ✓ combinations of trailing quantities
    return z.values                       # return (T, N) ndarray
```

**DANGEROUS（会被因果检测拒绝）**:

```python
def factor(env):
    c = pd.DataFrame(env.c)
    a = c.shift(-1)          # ✗ shift(负数) = 直接读明天
    b = c / c.mean()         # ✗ 全期均值含未来（用 rolling/expanding）
    d = (c - c.iloc[-1])     # ✗ 最后一行 = 未来锚点
    e = c.cummax()           # ✓ cummax 本身 trailing——但任何 "-1" 索引都危险
```

Rules of thumb: `shift(+k)` / `rolling` / `expanding` / `cum*` = safe; `shift(-k)`, full-sample statistics (`.mean()`, `.max()`, `.std()` on the whole axis without a trailing window), negative indexing = future leak.

**Non-deterministic factors** (unseeded `np.random`, external state, network/file reads) are flagged `nondeterministic` by the causality check — a high IC from a random factor is luck, not signal. Seed everything.

---

## 4. Efficiency: vectorize | 效率：向量化（必读）

LLM-written factors that loop are 100–1000× slower on large panels and will hit the worker timeout. The engine AST-scans your source before evaluation; **Class-A anti-patterns are hard-rejected** (evaluation refused, not counted as a trial, zero compute spent — each rejection message carries a copy-paste replacement template), Class-B patterns warn.

**Class-A (hard reject, 2026-08-27 决策 D3a)**: `iterrows` / `itertuples` / `applymap` / `apply(lambda)` / `np|pd.concat|concatenate|append` inside a loop body. These have no legitimate use in `factor(env)` given the ops library. Class-B (warn): nested `for`, `apply(named_fn)`.

**Measured on a reference 2000×500 panel** (this repo's dev machine, 2026-08-27; your machine scales, the ratios don't shrink):

| 写法 | 实测 CPU | 向量化等价 | 倍率 |
|---|---|---|---|
| `iterrows` 逐行回调 | 0.11s | 截面减均值 <0.001s | ~10⁵× |
| 循环内 `np.concatenate` | 2.50s | `rolling(20).mean()` 0.031s | 80×（且 O(n²)，面板越大恶化越猛） |
| 嵌套 `for` 逐元素（B 类警告） | 6.97s | 同上矩阵运算 | >200× |

**✗ Slow (loop) → ✓ Fast (vectorized)**:

```python
# ✗ 逐 symbol 循环
out = np.zeros_like(env.c)
for j in range(env.N):
    out[:, j] = env.c[:, j] / np.roll(env.c[:, j], 20)   # 还有 roll 越界 bug

# ✓ 向量化（一次矩阵运算）
c = pd.DataFrame(env.c)
out = (c / c.shift(20) - 1.0).values
```

```python
# ✗ 逐日截面 rank
for t in range(env.T):
    ranks[t] = pd.Series(env.c[t]).rank()

# ✓ 向量化 rank（axis=1 按行=截面）
ranks = c.rank(axis=1, pct=True).values
```

```python
# ✗ 循环内重建数组（A 类硬拒，O(n²)）
for t in range(env.T):
    out = np.concatenate([out, part])

# ✓ list.append 收集 + 循环外一次性拼接
parts = []
for w in (3, 5, 10):            # 小常数窗口集循环本身合法
    parts.append(c.rolling(w).mean().values)
out = np.mean(parts, axis=0)
```

- Prefer column-level ops, `rolling`, `groupby`, `rank(axis=1)` — never per-row/per-cell Python callbacks
- Accumulate rows in a list and `pd.concat` **once** at the end; never concat inside a loop (rebuilds the whole panel each iteration)
- If two sub-factors share an expensive intermediate, compute it once

**Runtime cost accounting** (2026-08-27 P4): `factor_evaluate` measures your factor in **CPU seconds** (wall-clock is distorted when sessions run in parallel); the diagnosis `perf` field warns early if the submit noise gate (≥10 worlds × single run) would blow its budget. Registry entries record `cpu_rel` (CPU seconds ÷ this machine's calibrated reference workload) — a slow factor keeps paying that tax at every fingerprint sample, walk-forward fold, and noise gate re-run, so vectorize before submit, not after.

**Reuse the operator library** — `dsh_factor_mining.factor.ops` ships 37 vectorized operators (ts_rank / ts_zscore / ts_corr / cs_rank / sigmoid / ...) usable inside your factor source:

```python
def factor(env):
    from dsh_factor_mining.factor.ops import ts_momentum, cs_rank, ts_zscore
    return cs_rank(ts_zscore(ts_momentum(env.c, 20), 60)).values
```

The random seed generator composes these same operators, so library-built and generated factors share one mathematical vocabulary.

---

## 5. Regions & caliber | 三区与口径

Your factor is evaluated under the session's `calibration` (regions `dev_end`/`sel_end` chosen by the user, horizon H, cost, execution t1/t0, limit-up/down mask). Don't hardcode any of these into the factor — the engine applies them uniformly. Report numbers must always name the caliber they were computed under (`factor_load_env` returns it).

---

## 6. What happens to your factor | 流水线

1. **causality** (mandatory gate; cached per source hash; timing + nondeterminism check)
2. **evaluate** on development — verdict/red-flags, deflated p, boundary sensitivity; auto-checked against the bounded memory pools (duplicate/method suspects)
3. stronger candidates → selection region → (once) test region
4. `factor_audit` — independent NumPy re-implementation cross-check
5. `factor_registry_submit` (receipt-verified) + `factor_export_report` for a shareable card

Every evaluation is auto-recorded in the engine trail — failed factors leave a trace too; hiding them is not possible by design.
