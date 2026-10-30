# 规划书：策略层 harness（dsh_strategy_lab）— 2026-08-27

> 定位：因子层产出已准入的 alpha（registry），策略层消费它们构造与
> 管理组合——本就在一条线上。本规划把因子 harness 验证过的六条纪律
> （唯一评估路径 / 时点环境 / 事务化准入 / 多重检验计价 / fail-closed
> + 可行动错误 / 预算分层）迁移到策略层，并解决三个策略层特有的新
> 问题：前视面倍增（→契约收窄）、路径依赖统计（→placebo 中心化）、
> 判定基准（→增量 + 启动点敏感性）。
>
> 两个已拍板的形态决策（2026-08-27 对话定稿）：
> 1. **放置 = B 兄弟包**：`python/src/dsh_strategy_lab/`，import
>    `dsh_factor_mining` 共享面；依赖方向物理单向（策略→因子）。
> 2. **工作场所 = ZCode IDE（本 IDE），不使用 DSH**：接口 CLI 优先
>    （`python -m dsh_strategy_lab`），DSH 插件 / drive 闭环 / ts/
>    全部出局；MCP server 缓（需要时再立项）。

## 0. 决策点记录

已定（对话拍板，不再重议）：

| # | 决策 | 结论 |
|---|---|---|
| S1 | 契约 | signal-only：`fit(env截断)→state`（可选，ML）+ `apply(state/env, env)→weights`；模拟器/成交/费用 harness 独占 |
| S2 | v1 范围 | ETF 面板截面组合 + overlay（数据管道复用因子层） |
| S4 | 多重检验 | **要，但便宜**：deflation 在 z 单位下一条公式；账本无条件保留；只数策略试验；因子须已入 registry；不做三层 Šidák |
| S6 | 形态 | 兄弟包 + CLI（ZCode IDE 内由助手驱动）；非 DSH、非 MCP 起步 |

待定（校准期定档，不阻塞 P0-P5 实现）：

| # | 决策点 | 默认 |
|---|---|---|
| C1 | 成本 v1 档位具体数字（佣金/滑点 bps） | 佣金 2.5bps + 滑点 5bps 起步，config 可调 |
| C2 | G1′ placebo 抽样下限与预算 | evaluate 侧 m≥30 / submit 权威重跑 m≥60，预算/下限照噪声门模式 |
| C3 | 启动点扰动门阈值 | v1 报告 + 软信号；对照 matched null 离散度校准后升硬门 |
| C4 | G2′ artifact 门判定细则 | 随机游走面板成本后显著为正即拒；显著性口径校准期定 |

## 1. 放置与形态

- **包**：`python/src/dsh_strategy_lab/`（兄弟于 `dsh_factor_mining`，
  同项目同测试套件）。只允许 `import dsh_factor_mining`，反向禁止。
- **共享面 = import 面**（P0 提炼，行为零变化，测试锁死）：
  `dsh_factor_mining/factor/gates.py` 新公开模块收编——
  - `blp_sigma`（B-LP E[max]，现 evaluate.py / tailgate.py 两处复制、
    注释自认「两处同步维护」，第三消费者出现 = 提炼时机）；
  - `selection_minhash` / `selection_similarity`（tailgate.py）；
  - 矩修正 t（evaluate._dsr_p_from_stats 公开化）。
  env.py / causality.py / noise.py / audit.py 直接 import 不复制——
  env 是唯一经生产校验的时点隔离实现，复制 = 两处维护前视防线。
- **状态**：`.strategy-lab/` 与 `.factor-mining/` 平级；账本互引
  `(source_hash, horizon)`；**生产因子状态只读，零迁移**。
- **DSH 生产面零扰动**：P0 是行为零变化的因子包改动，生产 dsh web
  侧无需跟进重装（重装与否由用户按需决定，无紧迫性）。
- **未来**：共享模块若被两侧边界校验稳定，整体抬升为 `dsh_harness`
  内核包（deferred，非本规划范围——B 是 C 的渐进路径，抬升非重写）。

## 2. 契约（agent 只声明 what，测量归 harness）

```python
def fit(env) -> state:            # 可选（ML）；harness 只给物理截断的 env
    ...                          # 必须 seeded + 确定性
def apply(state, env) -> weights:  # 必有；返回 {asset_id: weight}
    ...                          # 权重不必和为 1，余量 = 现金腿
```

- **weights 语义**：截面目标持仓；绝对收益/择时 = 暴露缩放（余量现金
  腿按现金收益计息）；多因子 = 从多个已准入因子构造。
- **fit/apply 切分日程归 harness**（walk-forward 的折边界由 harness
  定，fit 拿到的 env 物理截断）——泄漏防护结构性成立，且 fit 每折只
  跑一次（训练是超时高发区，单独计时预算）。
- **事件驱动 v1 边界**：事件 = env 特征列，决策仍在 bar 边界；盘中
  反应属 v2 执行回调（只预留定义，不实现）。
- **确定性是硬要求**（截断不变性审计的前提）：seed 由 harness 注入。
- **适配性来源**：契约只约束信息流（env 截断）与执行（模拟器独占），
  对模型类中立——线性/树/NN/规则在 harness 眼里都是 fit/apply。

## 3. 模拟器 v1（harness 独占）

- **成交时点**：t 收盘信号 → t+1 开盘成交（harness 固定，非 agent
  可选项）；涨跌停/停牌不可成交，信号持续则顺延、消失则撤单。
- **费用**：佣金 + 滑点固定 bps（C1）；T+1 约束；现金腿计息。
- **realism 阶梯**：每档写清假设（跨境 ETF T+0、随流动性价差等属
  二档）；`cost_model_version` 进指纹与账本——跨成本档的结果不可比，
  必须可区分。

## 4. 审计五道锁（每次 evaluate 全量跑；门判定只在 submit）

1. **数据禁运**：面板加载器私有，env 只由 harness 构造分发给 worker；
   workflow 中不出现原始面板文件路径。IDE 场景下这是 API 设计 +
   纪律（技术上 Bash 能读任何文件），故配第 5 锁兜底。
2. **代码只在 harness 沙箱执行**：策略代码以文件/字符串递交，在
   worker 子进程内运行（复用因子层 worker 模式：300s 墙 + 四要素
   超时错误 + 超时零写入不烧指纹）；agent 侧从不执行策略代码。
3. **截断不变性（机械前视检查的字面实现）**：env 截到 t 与截到 t+k
   各跑一次，weights 前 t 段逐位一致；任何分歧 = 读过未来，fail-closed。
4. **因果扰动族**：强制执行延迟 +1 bar（真因果策略应温和退化，性能
   不变 = 红旗）；信号日置换（causality.py 复用）；随机游走面板
   artifact 门（noise.py 面板生成器复用——在假价格上赚钱 = 剥削
   模拟器，G2′）。
5. **账本唯一出水口**：性能数字只出现在 CLI 评估响应里，每次评估
   自动记账（计数不依赖自觉）；账本完整性自查（因子 ref 有效性
   交叉核对等）。

## 5. 判据与门链（submit，事务化：全过才写 registry）

基准 = **被动 top-K 持仓**（所引因子、同再平衡期、同费用）——因子层
已测过的东西；策略必须证明 overlay 在成本后加了增量。

| 门 | 内容 |
|---|---|
| G0 | 审计链：确定性 + 截断不变性 + 因果扰动（不过 = 程序性拒收，不进统计判定） |
| G1′ | placebo：matched-turnover 随机信号（同换手/同持仓期）过同一模拟器含费用 → z ≥ 3；evaluate 侧轻量 m≥30（标注 degraded），submit 权威重跑 m≥60（按指纹缓存）——沿用尾线 G1 的轻/权威双档纪律 |
| G2′ | artifact：随机游走面板成本后不得显著为正（C4 定口径） |
| G3′ | deflation：`z_placebo ≥ E[max X](N_eff) + Φ⁻¹(1−α)`（z 已归一化，s0=1）；N_eff = trade_minhash 聚类（交易清单 (day, asset, side) 的 32 哈希 MinHash，血缘阈沿用 0.25；复用 selection_minhash，side 编进 pair 串）；与 z≥3 取大——N_eff 小时 G1′ 保底、大时 deflation 接管（835 量级时 E[max]≈3.2，z≥3 恰不够） |
| G4′ | 增量门：vs 被动基线的 delta（Sharpe/IR），block bootstrap CI 下界 > 0；多维报告（Sharpe/MDD/换手/容量/delta）不坍缩（PASS-FAIL 教训） |
| R | robustness 块（不计 N_eff）：启动点扰动（出生日偏移网格 ≥8 点，报告 delta 中位数/IQR/正占比，对照 matched null 离散度，v1 软门）+ 参数邻域平缓性（flatness.py 复用，跳过的邻居 ≠ 失败） |

数字是结构不是定稿：按因子层教训（解析 s0 实测低估约 2 倍），首批
真实账本上校准后定档（P6）。

## 6. 账本与状态（单一事实源，派生不落第二份）

- `.strategy-lab/strategy_trail.json`：每次评估一条，键 = 指纹 =
  `(factor_refs, 规则源 hash, 参数 hash, wf 配置 hash, cost_model_version)`；
  重评同键 = 更新不新增。
- **计试验**：development / walk_forward 两个 stage 的评估（walk-forward
  为策略层**必经** stage——regime 依赖是策略第一死因；折内参数重选
  不放大 N_eff，记为同指纹展开；wf 配置本身进指纹）。
- **不计试验**：placebo 抽样、随机游走面板、启动点扰动、平缓性探针、
  截断审计——它们是一次假设的探针（同因子层噪声世界不计试验）。
- **test 区一次性消费**，永不进 deflation（尾线 WS3 同款纪律）。
- `.strategy-lab/strategy_registry.json`：准入 registry，含拒收条目
  （程序性/实质性 reject_kind + 理由，供后续策略避坑）；改动前备份
  （沿因子 registry 铁律）。
- 因子层关联：factor_refs 必须全部在因子 registry 且 accepted——跨层
  多重检验就此封口（策略消耗的 alpha 已过自己的门，因子试验不重复
  计价），各层自守 α。

## 7. 实现清单（文件级，P0→P6 顺序执行）

| 阶段 | 文件 | 内容 |
|---|---|---|
| P0 共享面提炼（因子包，行为零变化；与 tail 线规划 D1-D4 正交可并行） | `factor/gates.py`（新） | `blp_sigma` 收编（evaluate/tailgate 两处去重，原处留别名）；`selection_minhash`/`selection_similarity` 公开；`_dsr_p_from_stats` 公开化 |
| P1 骨架 | `dsh_strategy_lab/{__init__,contract,env_adapter,simulator}.py` | 契约校验（fit/apply 形状、确定性 seed 注入）；env 适配（复用 factor env，物理截断 API）；模拟器（成交/费用/T+1/涨跌停顺延/现金腿，cost_model_version） |
| P2 审计 | `dsh_strategy_lab/audit.py` | 截断不变性（逐位比对）；强制延迟退化检查；复用 causality.py 日置换、noise.py 随机游走面板 |
| P3 账本 | `dsh_strategy_lab/{trail,fingerprint,state}.py` | 指纹五元组；strategy_trail 原子写；计/不计试验语义；strategy_registry 事务写 + 备份 |
| P4 门链 | `dsh_strategy_lab/{placebo,gates,increment,robustness}.py` | matched-turnover null（预算/下限 + 缓存）；trade_minhash N_eff + deflation bar；block bootstrap 增量；启动点扰动 + 平缓性 |
| P5 CLI | `dsh_strategy_lab/__main__.py` + worker | `python -m dsh_strategy_lab {evaluate,submit,trail,status}`；worker 子进程墙 + 四要素超时错误 + 超时零写入；紧凑响应投影（防截断，尾线 WS-A 教训前置） |
| P6 校准 | 用真实策略跑首批账本 | null z 分布 ≈ N(0,1) 验证；C1-C4 定档；启动点扰动门升硬与否 |

部署配套：`pip install -e`（或重装）到本 IDE 用的 Python 环境；配套
ZCode skill（strategy-lab 纪律：唯一评估路径 = CLI、代码只递交不私跑、
超时不换方向——放用户级 skills 目录，随 P5 交付）。

## 8. 测试细目

1. **截断不变性**：确定性正例通过；**故意前视的负例必须被抓**（t+k
   数据改变前段权重）——本 harness 的立身测试。
2. **模拟器**：T+1 生效；涨跌停日不成交且信号持续顺延/消失撤单；
   现金腿计息；费用算术；同输入重放逐位一致。
3. **placebo 校准**：合成面板上 null z 分布均值≈0、std≈1（容差内）。
4. **deflation**：trade_minhash 同交易聚类 / 异交易不聚；bar 对 N_eff
   单调；N_eff=1 时 bar < 3 → G1′ 保底生效。
5. **增量**：已知正 delta 的合成策略 CI 符号正确；零 overlay 策略
   vs 自身基线 delta≈0。
6. **artifact**：构造在随机游走面板上「盈利」的假策略（如利用成交
   假设漏洞）必须被 G2′ 拒。
7. **账本**：placebo/扰动/噪声面板/截断审计不计试验；dev+wf 计数；
   test 永不进 deflation；指纹稳定性（同组件同 hash、任一组件变则变）。
8. **事务**：submit 拒收仍记 registry 拒收条目 + 计试验；超时零写入
   不烧指纹；CLI evaluate 响应紧凑投影。
9. 全量回归绿（因子包 P0 改动后 233 项全过）。

## 9. 不做的事

- 不做 DSH 插件 / drive 闭环 / ts/ 任何改动（策略层不在 dsh web 跑）；
- 不做 MCP server（CLI 够用，需要时单独立项）；
- 不做盘中事件回调（v2 预留定义）；
- 不抽 `dsh_harness` 内核包（deferred 至边界被两侧校验稳定）；
- 不做三层 Šidák（各层自守 α，增量判据已是相对的）；
- 不动生产因子状态（registry 只读）；
- 不做容量/冲击成本模型（只报告换手与名义容量，不建门）。
