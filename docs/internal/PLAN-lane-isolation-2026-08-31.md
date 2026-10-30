# PLAN — 并行会话语义隔离（方案 A：lane 一等公民）

日期：2026-08-31 ｜ 状态：已实现，全量回归绿
前置讨论：本文件前两轮对话（诊断 → 「A 方案会影响 trials 记录吗」→ 「那就按A方案做」）

## 1. 问题

dsh web 一个 host 只有一个共享 Python bridge 进程（`factor-mining-python`
owns one persistent bridge），所有并行会话的 `factor_*` 调用从它走。写完整性
由 filelock 保证（P0-P5 压测过），但引擎的**意图层**状态建立在单叙述者
假设上，双线并行时互相污染（2026-08-31 上午 h10c30 root 双线实况转录取证）：

| 污染点 | 机制 | 实况证据 |
|---|---|---|
| pending_hypothesis 义务 | 取全局 trail.json 最后一条的 next_hypothesis | B 线被 obligation 强制消化 A 线的 Parkinson 假设并执行 |
| 家族链 streak | 从全局 trail_engine 尾部回走 | 外线条目截断本线链 → streak/族边际/族收敛全在算混合族 |
| 断链检测 | 新试验 vs 全局最后一条 | 双线交错 → 每条新试验都「断链」→ arc 归零风暴 |
| arc 方向预算 | mining_state 全局计数器 | 预算双倍速消耗；A 线换向解锁 B 线预算 |
| bar/n_trials 跳变 | 全局计数本身正确，但无归属呈现 | agent 把外线 3 条新试验脑补成「我超时的 evaluate 完成了」 |

## 2. 决策（用户拍板）

- **方案 A**（条目打 lane 标签 + 读端过滤），否决 B（每线独立 stateRoot，
  丢事实层共享、deflation 各线独立计数系统性低估）与 C（只加呈现不改语义，
  obligation/族链两个行为级污染不解决）。
- **D1 隔离粒度 = 条目打标**，非文件级分目录：直接 Read 原始 json 的 agent
  看到的是带 lane 标签的自描述账本（标签使原始读也可见归属），不必拆文件。
- **D2 lane 身份 = 请求级**：TS 工具层从 `exec.agent.id`（= SessionId）盖入
  每次 lane 敏感调用。共享 bridge 下进程身份 ≠ 会话身份，请求级是唯一正确
  挂点；直连 CLI 用 `--lane` / `DSH_FACTOR_MINING_LANE`（进程默认，请求级
  优先）。
- **D3 历史数据不迁移**：无 lane 字段条目一律归 `default` 线（= 单线时代的
  唯一叙述者）。mining_state 顶层 arc_rounds 首次写入时迁移进 default 桶
  （读侧有顶层回退，不依赖首次写入触发）。
- **计价层不动**（上一轮对话确认）：n_trials / deflation sr0 / tail bar 按
  全局全量计数——其它线的试验照常抬高本线 bar（多重检验统计正确性）；
  method_suspect / duplicate_suspect / explored 证伪记忆 / registry 全局。

## 3. 隔离边界速查

| 状态 | 口径 | 理由 |
|---|---|---|
| pending_hypothesis / pending_rejected / frozen | 本线 | 意图：谁该做什么 |
| 家族链 streak / cluster_trials / 族收敛 / 族边际 | 本线 | 单叙述者的方向判定 |
| arc 方向预算（lanes[lane].arc_rounds） | 本线 | A 线换向不解锁 B 线 |
| pass_unadmitted（R7 compose 触发） | 本线 | 他线的 pass 不该强制本线组合 |
| rounds_total / last_engine_trail(latest5) / agent_rounds | 本线 | 叙事视角 |
| n_trials / deflation / bar_sigma / tail bar / N_eff | **全局** | 多重检验全量计价 |
| plateau（irs[-20:] 穷尽信号） | 全局 | 搜索史 exhausted 是数据集事实 |
| explored / search_paths / method_suspect / registry / verdict_counts | 全局 | 事实层，跨线共享是收益 |
| tail 遥测（near_misses 等） | 全局 | 从 tail_ledger 派生的计价视图 |

## 4. 实现

**Python（`python/src/dsh_factor_mining/`）**
- `lane.py`（新）：请求 lane 解析链（请求参数 → `DSH_FACTOR_MINER_*` 环境变量
  → `default`）；contextvar 贯穿 dispatch 生命周期（单线程 bridge 安全）；
  `own_entries()` 过滤助手；`normalize_lane`（`[A-Za-z0-9_-]` ≤48 清洗）。
- `bridge.py`：
  - `dispatch` 入口摘 lane（`lane`/`_lane` 键，摘掉后再交给方法处理器）。
  - `_append_engine_trail_batch`：条目打 `lane` 标；断链检测 vs **本线**尾部
    （外线条目既不接链也不截断）；`arc_rounds_bump(reset=True, lane=本线)`。
  - `_paths_append`：三层（trail/explored/search_paths）都打 lane（后两层
    纯溯源，读端不过滤）；entry 里模型自填的 lane 被 contextvar 覆盖——
    归属是基础设施事实，不是模型可声明的元数据。
  - `_loop_directive`：意图层全量换 `own_entries`；arc 读 `lanes` 桶；
    payload 增 `lane` / `n_trials_lane`，外线存在时附 `lane_note`
    （「本线 N / 全局 M（其它线 K）——bar 按全局计价」）。
  - `_state_trail_summary`：latest5 本线；`evaluations.{lane,foreign}` 分线
    计数；`agent_rounds` 本线；verdict_counts 仍全局。
  - 异步重方法：`_async_submit` 把提交者 lane 写进 job.json；
    `_async_result` 轮询取回时用 job.json 的 lane 包住 `_wrap_diagnosis`
    （trail 写入归提交线，轮询者是谁不影响）。
  - `main()`：`--lane` 进程默认旗标。
- `state.py`：`arc_rounds_bump(root, reset, lane)` 分桶维护
  `mining_state["lanes"][lane]["arc_rounds"]`；`_ensure_lane_buckets`
  一次性迁移顶层遗留键（迁移后删除，防混部双计）；`lane_arc_rounds`
  读助手（default 线对未迁移 state 有顶层回退）。
- 去重语义不变：`(source_hash, stage, horizon)` 内容寻址——两线评同一
  因子仍是同一试验（重评者的 lane 覆盖条目标签，n_trials 稳定）。

**TS（`ts/`）**
- `factor-mining/src/types.ts`：`FactorSourceRequest` / `BatchRequest` /
  `TrailSummaryRequest` 增 `lane?: string | undefined`；新 `StatusRequest`。
- `factor-mining/src/index.ts`：`status(request?)`；`appendTrail/Explored/
  SearchPath(entry, lane?)`。
- `factor-mining-python/src/index.ts`：status 转发 request；append* 把 lane
  并入 `paths.append` 参数。
- `tool-factor-mining/src/lane.ts`（新）：`laneOfExec`（`exec.agent.id` →
  清洗）、`laneOfSession`。**只给 lane 敏感工具盖章**：factor_status /
  evaluate / evaluate_composite / evaluate_batch / walk_forward / audit /
  trail_summary / 三个 record。model 面参数零变化（lane 不出现在工具
  schema 里）。
- `tool-factor-mining/src/drive.ts`：注入器 `service.status({lane:
  laneOfSession(sessionId)})`——推进指令按该会话自己的线判。

## 5. 验证

- `python/tests/test_lane_isolation.py`（新，8 测试）：pending 隔离 /
  n_trials 全局计价+lane_note / 外线交错不截断族链 / arc 分线+断链不串线 /
  断链检测忽略外线尾部（防归零风暴）/ legacy 归 default+迁移 / trail_summary
  分线视图 / 模型自填 lane 被覆盖。全过。
- Python 全量回归 exit 0（`-x` 模式）。
- TS：三包 `tsc -b` 干净（harness 镜像构建，`exactOptionalPropertyTypes`
  适配过）；vitest `index.spec` + `tool.e2e`（真实工具管线，PYTHONPATH 指
  worktree 新包）4/4 通过。

## 6. 遗留与边界

- **同因子跨线重评**：内容寻址去重让条目归属跟最后写入者——n_trials 不变，
  本线视角「少一条自己的试验」。可接受（同因子=同试验）。
- **ZCode/直连 MCP 并行**：每会话独立进程，请求级 lane 不会自动出现——
  并行时需显式 `--lane` / 环境变量。单会话用 default 线正确续旧数据。
- **部署顺序**：Python 与 TS 需同版上线（旧 TS 不发 lane → 所有会话归
  default 线 = 现状行为；新 TS + 旧 Python → lane 被忽略，同样安全退化）。
- **dsh web 会话重启**：新会话 = 新 lane（session id 变了）——中断续挖的
  arc/族链连续性以「新线」开始，全局账不受影响。要续线需保持同一 session。
