# 任务书：尾部线评估侧四件套 — 2026-08-25

> 背景：submit 侧尾轨准入（admit_basis=tail，G1/G2/G3）已上线
> （2a07443..8e1cad8 + b670a65 修复批）。评估侧主干复用已完成
> （_top_n_excess 生产模拟器 / evaluate 自动尾块 / 噪声与置换机器
> statistic 参数化 / trail 派生账本），**不新建平行管线**。本任务书
> 补齐四个薄点，使 G1/G3 达到与 IC 线同等的校准、精度与纪律水平。
>
> 生产 stateRoot：`C:\Users\RTyyyy\.factor-mining`（环境键 `etf`，
> 面板 2822×1752，sample_step 见 calibration）。测试基线：pytest
> 201 passed（b670a65 后）。

## 0. 总览与顺序

| WS | 内容 | 动的门 | 依赖 | 估算 |
|---|---|---|---|---|
| WS1 | 尾部经验 null 校准（spread 版 null 地形） | G3 的 s0 | 无 | 1 会话 |
| WS2 | G1 placebo 门判定移 submit + 样本加厚 | G1 | 无 | 1 会话 |
| WS4 | spread 矩入账本 + G3 高阶矩修正 | G3 公式 | WS1（校准一次成型） | 1 会话 |
| WS3 | test/finalize 区尾轨消费定义 | 不动门，动 finalize | WS1-4 完成后收尾 | 1 会话 |
| 统一 | registry 27 条重判对照 + 部署 + 文档 | 全部 | WS1/2/4 | 0.5 会话 |

顺序理由：WS1 与 WS4 都动 G3，先经验化 s0（输入）再修公式（形式），
两者的 registry 重判校准合并做一次；WS2 独立可并行；WS3 是语义收尾
（test 消费品纪律涉及 stage 过滤，放最后避免与 WS1/4 的 trail 改动
交叉）。

**统一纪律约束（四条共用，实现时不许绕）：**
- 账本派生不落第二文件：一切新字段挂 trail_engine 条目或
  null_landscape.json，tail_ledger 保持纯派生。
- 指纹门全覆盖：新 landscape 段必须绑 env_fingerprint（复用
  `_landscape_fingerprint_status`）；不匹配 = 无效 + 提示重校准。
- 原子写 + lossless 消毒（`_lossless` / -0.0 归一）适用所有落盘。
- submit 侧新增计算（WS2 重 placebo）沿用事务语义：基础设施失败 =
  中止不落盘，实质性判定才写拒绝条目。

---

## WS1 尾部经验 null 校准

### 目标
G3 的 s0 从解析式 `1/√n_days`（理论近似）升级为**本面板随机因子的
spread_ir 经验分布 std**。若经验值 ≈ 解析式，门被验证；若偏离，
用经验值（这是发现：解析假设在本面板不成立）。

### 现状与差距
- IC 线已有：`random_gen.run_null_calibration`（random_gen.py:328）
  —— n 棵随机树 × horizon 视图 × **train 区过滤**（`ic.index <
  dev_end`，:365 附近）→ per-horizon IC_IR 分位 → null_landscape.json
  （绑 env_fingerprint）→ bridge `_landscape_pool_std` 取 pool_std。
- 尾线现状：`tailgate.tail_deflation_bar`（tailgate.py:130）s0 纯解析。
  **没有任何 spread_ir 的经验 null**。

### 设计（钉死）
1. **复用同一批树**：`run_null_calibration` 的树生成循环里，对每棵树
   每个 horizon 视图 v 追加计算 train 区 spread_ir——与 tail_metrics
   同口径：行集 `np.arange(0, t_end, sample_step)`，t_end =
   searchsorted(dates, dev_end)；K = round(0.2·n_day)。**不另跑一批
   树**（同批树保证 spread null 与 IC null 同条件，成本 ≈ 每树每
   horizon 一次逐日循环，n=50 × 菜单宽度，秒级）。
2. **landscape 新段**：null_landscape.json 顶层加
   `"spread": {str(h): {p10, p50, p90, p95, std, mean_abs, n}}`，
   与 `ic_ir` per-horizon 结构同构。env_fingerprint 绑定自动生效
   （同一文件）。
3. **读取与降级**：bridge 新增 `_landscape_tail_s0(env_id, horizon)`
   ——指纹门（match）+ spread 段存在 → 返回 std；否则返回 None。
   `tail_deflation_bar` 加可选参 `s0_emp: float | None`：有 → 用
   `s0 = s0_emp`，输出加 `"s0_source": "empirical"`；无 → 解析式 +
   `"s0_source": "analytic"`。**降级不拒绝给门**（与 pool_std 的
   拒绝语义不同——理由：解析式有理论依据且 G3 有 G1 前置门，经验值
   是校准增强；pool_std 缺失时 p 完全不可算才拒绝）。
   旧 landscape 无 spread 段 → 同 legacy 处理（提示重跑校准）。
4. **接线点**：`_registry_submit` 尾轨判定处，从
   `tail_admission(tail_block, ledger, z)` 改为传 landscape s0 进去
   （tail_admission 加可选参 `s0_emp`，内部转给 bar；纯函数性质不变，
   bridge 负责读取与指纹校验）。

### 实现清单
- `factor/tail.py`：新增 `spread_ir_statistic_train(F, fwd, pit,
  step, t_end)`（或在 `spread_ir_statistic` 加 keyword-only `t_end=
  None`，默认全区间不动——noise/day-perm 调用方零感知）。
- `factor/random_gen.py`：`run_null_calibration` 循环内追加 spread
  计算 + `spread` 段写入；`factor.null_landscape` 查询响应自然带出。
- `factor/tailgate.py`：`tail_deflation_bar(n_eff, n_days,
  alpha_track, s0_emp=None)`；`tail_admission` 透传。
- `bridge.py`：`_landscape_tail_s0` + submit 接线。
- **实现前确认**（不猜）：run_null_calibration 内 train 区边界用
  `v.calibration.dev_end` 的回退逻辑（:363-365 有 dev_end None 时的
  0.6 回退）——spread 段必须用同一边界函数，抽公共 `_train_end_of(v)`。

### 验收
- 单测：校准后 landscape 含 spread 段且指纹绑定；mismatch → s0_emp
  None → bar 输出 analytic；submit 全链带 s0_source 标注。
- **生产校准（关键交付物）**：生产面板跑一次 n=50，报告
  s0_emp vs s0_analytic 比值表（per horizon）。比值 ∈ [0.7, 1.4]
  → 解析式验证通过（写入本文档附录）；偏离 → 用经验值并记录数字。
- 回归：pytest 全量。

### 不做
- 不为 spread 另建 null-landscape 文件；不动 IC 分位计算；
  不做 spread 的跨 horizon 相关先验（尾线族几何用名单 MinHash，
  不需要）。

---

## WS2 G1 placebo 门判定移 submit + 加厚

### 目标
z≥3 是硬判据，但判定用的 placebo_z 来自 evaluate 内轻量 null
（自适应 draws = max(10, min(40, 4e6/F.size))，生产面板 ≈ 5M 单元格
→ **10 draws**，σ 相对误差 ~24%）。门的重判定必须在 submit 用足样本。

### 设计（钉死）
1. **架构对齐平坦性门**：evaluate 里的 placebo 保持轻量（自动计数
   语义不变，tail 块照写，字段更名 `placebo_z_light` 语义保留原名
   `placebo_z` 也行——**决策：保留原名**，agent 视图不膨胀；权威值
   在 submit 侧另起字段）。submit 尾轨判定前，引擎自主重跑 placebo：
   draws R = max(60, mining_state.tail_placebo_m 默认 120)，预算自适应
   （先跑 10 draws 实测均时，超 `tail_placebo_budget_secs` 黺 180s 且
   ≥60 已跑即截断，如实报 draws）。
2. **种子纪律**：环境指纹派生（对齐噪声门/day-perm：
   sha256(fingerprint)[:8]），同环境可复现。
3. **公共函数抽取**：tail.py 的 placebo 循环（tail.py:148-167）提为
   `topn_placebo(F, fwd, pit, env, t_end, draws, seed) -> dict`
   （返回 draws/null_mean/null_std/z/periods），evaluate 与 submit
   共用——一处实现两处口径。
4. **缓存**：重 placebo 贵且确定性（seed 固定）——
   stateRoot/placebo_cache.json，键 `(source_hash, horizon, k_frac,
  seed, draws)`，有界 512 条（对齐既有缓存纪律），值含指纹（跨环境
   不得误命中）。缓存命中 → 直接用；未命中算完写缓存。
5. **G1 判定值来源**：`tail_admission` 的 G1 分支改收
   `placebo_z_gate`（submit 重算值）；tail_block 里的轻量值仅当
   submit 无法编译执行 source 时兜底（标注 degraded）。
6. **事务语义**：重 placebo 基础设施失败（编译/超时）= 中止不落盘
   （对齐 G2 spread 门 b670a65 的处理）；截断到 ≥60 draws 不是失败，
   是如实样本量。

### 实现清单
- `factor/tail.py`：抽 `topn_placebo`；`tail_metrics` 改调用它
  （行为不变）。
- `bridge.py`：`_registry_submit` 尾轨段插重算 + 缓存读写；失败中止。
- `factor/tailgate.py`：G1 用 gate 值；diag 加 `placebo_m`
  （实际 draws）与 `placebo_degraded`。
- ledger 取证：submit 的 ledger 条目加 `tail_placebo_m` 与 gate z。

### 验收
- 单测：submit 后 entry/diag 带 placebo_m ≥ 60；缓存命中第二次不
  重算（计数或 monkeypatch 计时）；截断路径如实报告；infra 失败中止
  且 registry 无条目。
- **生产实测**：生产面板单次 _top_n_excess placebo 重放耗时 × R=120
  的总增量，写入本文档附录；若 >180s 预算频繁截断，下调默认 R 或
  记录为已知边界。
- 回归：pytest 全量。

### 不做
- 不动 G2 spread 噪声门的 m=12（另一话题：G2 是 artifact 检测，
  判据 |z|≥3 在 12 世界下已可判，如需加厚单独立项）；
- 不给 evaluate 的轻量 placebo 加预算（它不是门）。

---

## WS4 spread 矩入账本 + G3 高阶矩修正

### 目标
消除 HANDOFF §7.5 已知简化：G3 目前是纯正态 E[max] + 正态分位门槛。
对齐 IC 线 DSR 的高阶矩处理（t 分母偏度/峰度修正，
`_dsr_p_from_stats` 的 denom 结构）。

### 设计（钉死）
1. **只存矩，不存序列**。IC 线存 ic_series_sketch 是因为族相关
   （_LuckSampler R 矩阵）需要；尾线族几何用名单 MinHash，spread
   序列没有账本用途。tail 块新增
   `"spread_moments": {"g3": float, "g4": float, "n": int}`
   （train 区 spread 序列的偏度/峰度/采样日数，evaluate 时从完整
   序列算——round 6 位）。条目增量 ~60B。
2. **G3 公式**：现行 `spread_ir ≥ emax·s0 + z_α·s0` 改为修正 t：
   `t_adj = (spread_ir − emax(N_eff)·s0) · √(n−1) /
   (1 − g3·spread_ir + (g4−1)/4·spread_ir²)^0.5 ≥ z_α`
   ——与 `_dsr_p_from_stats`（evaluate.py:408-428）完全同构：
   sr→spread_ir，sr0→emax·s0，n→采样日数。bar 的 E[max] 本身保持
   正态（IC 线的 _LuckSampler 同样是高斯假设——对齐水平，不超纲）。
   denom clip 下限 1e-8 同 IC 线。
3. **兼容**：旧尾块无 spread_moments → denom=1（即现行公式），
   diag 标 `moments: "legacy"`。新旧门可能翻转边缘判定 → 统一校准
   （见 §统一）对比后再定稿。
4. **输出**：tail_deflation_bar 返回值加 `t_adj`、`denom`、
   `g3`、`g4`；拒绝 reason 附修正后数字（PASS-FAIL 教训：多维报告）。

### 实现清单
- `factor/tail.py`：`tail_metrics` 计算 spread_moments（sp 序列
  在手，零额外成本）。
- `factor/tailgate.py`：`tail_deflation_bar`/`tail_admission` 改公式。
- trail 条目自然携带（tail 块整体落盘，无需改 bridge 写入）。

### 验收
- 单测：构造偏态 spread（手算 g3/g4 → t_adj 一致）；无矩回退 denom=1；
  G3 通过/拒绝边界用修正前后各验一例。
- 回归 + 统一校准（§统一）。

### 不做
- 不给 E[max] 做非正态修正；不存 spread_series_sketch（未来若尾线
  需要序列级族分析再立项，本文档留痕）。

---

## WS3 test/finalize 区尾轨消费定义

### 目标
admit_basis=tail 的因子走 finalize（stage=test 一次性消费）时，
test 区有明确、最小、诚实的尾部数字；不违反 test 消耗品纪律。

### 现状
- `evaluate_test` → `_region_diagnostic`（evaluate.py:876）已含
  topn（_top_n_excess 同一模拟器：净超额/换手/逐年/block bootstrap）
  ——**组合层基础设施已在**，缺的是尾部统计与对照语义。
- `_region_diagnostic` 无 tail 段；test 条目落 trail 时 tail=None；
  `tail_ledger` 无 stage 过滤（当前 test 条目无 tail 所以无泄漏——
  WS3 一旦给 test 条目加尾块，**必须同时做两条防线**，见下）。

### 设计（钉死）
1. **test 区尾块内容（最小集，无 null）**：placebo 是 train 区 null
   语义，test 区不做（test 的判定是真实表现 vs 预先声明，不需要
   null；也不给任何在 test 区反复试的机会）。test 尾块 =
   `{region: "test", spread_ir, tail_ic, k_typical}`——spread 统计量
   在 test 行集（sel_end..end）上算，复用 WS1 的 t_end 参数化。
2. **train 对照（bridge 层）**：`_wrap_diagnosis` 对 stage=test 的
   结果，反查 trail_engine 该 (source_hash, horizon) 的 train 尾块，
   附 `tail_decay: {spread_ir_train, spread_ir_test, ratio,
   sign_flip}`——报告数字，**不自动红旗**（衰减多少算失败是校准
   问题，先给数字；翻号红旗作为 v2，等有样本后定阈）。test_lock 的
   diagnosis_summary 增 spread_ir_test。
3. **两条防线（防 test 数字混入计价/准入）**：
   a. `tail_ledger` 加 stage 过滤：只收
      stage ∈ {development, batch, composite}——test 不是试验，不进
      deflation 计价。
   b. `_registry_submit` 反查 tail_block 时排除 stage=="test"
      （b670a65 加了 horizon 匹配，这里补 stage）——test 区 spread_ir
      绝不可用于 G3 准入（泄漏）。
4. **walk_forward 不新增尾部口径**：尾部时间稳健性由既有 topn
   yearly_net（train/selection 区）覆盖；如确需逐块 spread，等真实
   需求再立项。

### 实现清单
- `factor/evaluate.py`：`_region_diagnostic` 加可选 tail 段计算
  （t0/t1 行界已有）。
- `factor/tailgate.py`：`tail_ledger` stage 过滤。
- `bridge.py`：`_wrap_diagnosis` test 路径的 tail_decay 对照；
  `_registry_submit` 反查排除 test；test_lock summary 扩展
  （evaluate.py evaluate_test 的 lock payload）。

### 验收
- 单测：尾轨因子 finalize → test 结果含 spread_ir_test + tail_decay；
  test 条目带尾块但 tail_ledger 不含它；submit 反查跳过 test 条目
  （构造同名 hash 双 stage 条目验证取 train）；红线：任何把 test
  spread_ir 送进 G3 的路径都必须有测试证明不存在。
- e2e：一条尾轨因子完整走 evaluate(dev) → submit(tail) →
  evaluate(test) 全链。
- 回归：pytest 全量。

---

## 统一校准与验收（WS1/2/4 完成后一次做）

1. **registry 27 条重判对照**（19 accepted + 8 rejected）：
   - 重跑 G1（WS2 权威 placebo）+ G3（WS1 s0_emp × WS4 t_adj），
     产出四列对照表：旧门判定 / 新门判定 / 翻转条目 / 翻转原因
     （s0 变化 or 高阶矩 or placebo 加厚）。
   - 翻转 = 校准发现，**不回写历史 accepted**（对齐 tracks 回填
     纪律：ic 轨取提交时历史判定）——新判定以 `tracks_v2` 字段随
     条目追加标注，溯源戳 `recalibrated_at`。
   - 若假阳性/阴性翻转 > 3 条：门参数（alpha_track / R / s0 来源）
     暂停定稿，数字上报用户过目后再定。
2. **生产校准跑**：WS1 的 s0 比值表 + WS2 的 placebo 耗时表，写入
   本文档附录（接手者的第一手数字）。
3. 全量 pytest（预期 201 + 各 WS 新增 ≈ 215-225）。
4. 部署：python `pip install --force-reinstall --no-deps
   <worktree>/python`；TS 侧预期无改动（新字段全部在 Python 响应
   内）——若工具描述需提及 placebo_m/tail_decay，最后统一改一次
   index.ts 并走完整 DH 构建/pack/patch/pnpm 管线（坑1 坑2 流程
   见 HANDOFF-2026-08-25 §5；**pnpm add 后必须 grep node_modules
   确认 "added 1"，downloaded 0 = 没换**）。重启 dsh web 由用户执行。
5. 回滚：每 WS 独立 commit；门公式回滚 = revert WS4 commit；
   s0 回退 analytic = 删 landscape spread 段（自动降级）；
   G1 回退 = placebo_cache 忽略 + submit 不重算（一个开关
   mining_state.tail_placebo_m=0 → 直接用 evaluate 轻量值，留这个
   逃生门）。

## 风险清单

| 风险 | 缓解 |
|---|---|
| s0_emp 与解析式大幅偏离 → 门突然变紧/松 | 统一校准对照表 + 用户过目阈值（翻转 >3 条暂停定稿） |
| 生产面板重 placebo 超 submit worker 300s | 预算自适应 180s + 下限 60 draws + 先实测单次成本（WS2 验收第一步） |
| spread_moments 从 round 序列算矩（截尾误差） | 矩从完整序列算（evaluate 内序列在手），非 sketch |
| test 尾块混入账本/准入 | 两条防线（ledger stage 过滤 + submit 反查排除）各有独立测试 |
| null_landscape 膨胀（spread 段 × horizon 菜单） | 每 horizon ~10 个标量，可忽略；不存序列 |
| WS1 与 WS4 叠加后 G3 语义漂移 | 两者合并在同一次统一校准里验收，分开 commit 但校准一次 |

## 附录（2026-08-26 回填，生产面板 2822×1752 / stateRoot `~\.factor-mining`）

### A1 s0_emp vs analytic 比值表

| horizon | train 采样日 | s0_analytic=1/√n | s0_emp（n=40 随机树） | 比值 | 判定 |
|---|---|---|---|---|---|
| 20 | 86 | 0.1078 | 0.2208 | **2.05** | 偏离 [0.7, 1.4] → **用经验值** |

- 解析假设在本面板不成立（校准发现）：随机树因子经 ts 算子高度持续，
  跨采样日 spread 相关使有效样本 ≈ 1/0.2208² ≈ 20.5 ≪ 86。
- 门变化：G3 的 s0 从 0.108 → 0.221（≈2×收紧；N_eff=5 时 legacy 口径
  required ≈ (emax+1.955)×s0）。回滚杆：删 landscape spread 段自动降级。
- 校准成本：n=50 全程 45s（含 spread 段）；spread 段 n_valid=40/50。

### A2 生产 placebo 单次耗时 / R=120 总增量

- 单次重放（截面置换 + _top_n_excess 全 replay）≈ **0.02s/draw**；
  因子本体计算 ~1.7s。
- R=120 总增量 ≈ **3-4s/submit**——预算 180s 富余，无截断路径触发。
- 29 因子重判全程（编译+尾块+重 placebo）145s。

### A3 registry 重判对照（29 条 = 19 accepted + 10 rejected；任务书
### 写 27 后新增 2 条 rejected，全部纳入）

- **翻转 0/29**（≤3 → 门参数定稿，无需暂停上报）。tail 轨在
  2026-08-25 回填时已全数拒收（G3 门 0.31 vs 最好 spread_ir 0.233），
  新门只紧不松，无反向翻转空间。
- N_eff = 5（回填时 3；dk 族新条目入账，名单聚类结果）。
- **G1 placebo 加厚的实质发现**：轻量 10-draw null 低估 σ → z 系统性
  虚高。旧轻量 z≥3 的 5 条中 3 条跌下 3：volume_decay_30（3.79→2.23）、
  tsi_ar2_obv_composite / tsi_obv_composite_final（3.79→1.95）。
  120-draw 下 G1 幸存（z≥3）：dk_ewma_filtered 4.20、amihud_regime_asym
  3.18、dk_ewma 3.16、amihud_regime_w120 3.04、amihud_w120 3.01。
- G3：s0_emp×2.05 使门 ~2×；全部条目维持拒收（t_adj 最优
  dk_ewma_filtered 亦未过 N_eff=5 的新门）。
- tracks_v2 已随 29 条写入（recalibrated_at=2026-08-26，含 g1_z_heavy/
  g1_m/g3_required/g3_t_adj/g3_denom/s0_source/n_eff）；历史 accepted
  未回写。备份：`registry.json.bak-v2-20260826-010605`；
  landscape 备份：`null_landscape.json.bak-pre-spread-20260826`。

### 部署记录（2026-08-26）

- Python：`pip install --force-reinstall --no-deps <worktree>/python`
  完成；site-packages 验证 `_landscape_tail_s0`/`topn_placebo`/
  `TAIL_LEDGER_STAGES`/`tail_deflation_bar(s0_emp, moments)` 在场；
  安装版对生产 stateRoot 冒烟：fingerprint=match，s0_emp=0.2208 生效。
- TS 侧：无改动（新字段全部在 Python 响应内）。
- 重启 dsh web 由用户执行。
- 测试：全量 220 passed（基线 201 + WS1 5 + WS2 6 + WS4 5 + WS3 3）。
- commit 链：507a607（WS1）→ 9d983fe（WS2）→ d5ed63f（WS4）→
  a5525ea（WS3）。
