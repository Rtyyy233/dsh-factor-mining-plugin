# 任务书：灵感源自主切换 — 论文冷启动 / 随机因子探索 — 2026-09-01

> 动机（2026-09-01 用户指令）：引擎应该有「自主让模型选择从头从论文中
> 找新灵感，或者从随机因子中找灵感」的能力。现状取证：
>
> 1. **灵感源选择被引擎机械判据垄断**。`_pick_strategy`（bridge.py）
>   的优先级表 R1-R8 里，换灵感源的动作只有 `literature` 一种，且只在
>   「IC+尾部线双枯竭 <-0.10」（R1）或「direction_budget 耗尽且双枯竭」
>   时触发——纯被动、判据单一。`rotate` 只说"换一个方向"，不指定
>   灵感来自哪里。agent 的语义判断（"这个方向的数据结构不支持当前
>   假设族"）没有任何合法表达通道：`next_hypothesis` 是回显义务，
>   穷尽宣言被 `_surrender_match` 拒收（→ rotate）。判断力被降格成
>   执行力。
> 2. **随机因子基础设施全在，但没有策略地位**。`factor_random_generate`
>   (mode=explore) 已实现：随机生成 n → 轻量 IC 扫描 → top_k 源码进
>   标准管线（烟测/因果/评估）；算子集可配（factor_operators）；null
>   地形门已校准（新因子须超 null p95 才有趣）。但 `_STRATEGY_DIRECTIVES`
>   六种策略类型（continue/refine/rotate/literature/compose/query）
>   没有任何一种把 agent 引向随机探索——loop 指令层面这条路不存在，
>   agent 在 turn 内自发调用是在与注入器的 continue 指令竞争，语义上
>   不合法。
>
> 技术路线钉死：**TS 零改动**。已核实注入器（drive.ts）只转发引擎
> 策略原文，声明通道走 `next_hypothesis` 文本解析（Python 侧），
> 全部改动落在 bridge.py。

## 0. 决策点（待拍板，附建议）

| # | 决策点 | 建议 |
|---|---|---|
| D1 | agent 声明灵感重置是「声明即切」还是「声明后需引擎判据背书」（如族边际 <-0.05 才放行） | **声明即切，但限频**（见 D3）。agent 判断"方向讲不通"的信息引擎判据捕捉不到；背书条件会把新能力退回 R1 同款判据。风险用限频兜底而非判据兜底 |
| D2 | `random` 策略只走 agent 声明触发，还是也进引擎自动优先级表（R1 双枯竭时 literature/random 轮转） | **两处都要**：声明通道是自主权，R1 轮转是引擎自己的多样性保险（连续 N 次 literature 后仍未破局 → 试一次 random）。轮转计数器仿 `lit_search_count`（papers.searches）建 `random_runs` 台账键 |
| D3 | 防滥用约束 | 每方向段灵感重置声明 ≤2 次（第 3 次起降级 pending_rejected+点名）；random 策略每方向段 ≤1 次；`factor_random_generate` 的 explore 幸存者进 trail 时标 `origin: "random"`（归因分析可区分假设驱动 vs 随机幸存，入册照常走全门链） |
| D4 | literature 冷启动语义强度：复用现有 literature 指令，还是更强语义（断链 + 清 pending + papers 字段强制） | **更强语义**。现有 literature 是"补一发假设"，冷启动是"当前方向整体作废"——应同 direction_budget 的强制路径：断链重置预算 + pending 作废 + 轮转种子照常 |

## 1. 设计

### WS-A `random` 策略类型（指令 + 调度）

- `_STRATEGY_DIRECTIVES` 新增 `random`：引导 `factor_random_generate
  (mode='explore')` → top_k 幸存者逐个走标准管线（烟测→因果→评估），
  幸存者是**选择不是结论**（工具描述已有此措辞，指令沿用）；
  幸存者中最高 |IC_IR| 未超 null p95 时如实报告并按普通失败入账。
- `_pick_strategy` 接入两处：声明通道强制路径（WS-B）与 R1 轮转
  （D2）。key 格式沿用 `{t}:{agent_rounds}:{n_trials}`。
- 台账：`ledger.json` 新键 `random_runs`（计数，仿 papers.searches），
  R1 轮转用 `lit_search_count % 3 == 2` 之类的确定性轮转（不引随机数，
  可复现原则与 `_LITERATURE_SEEDS` 同源）。

### WS-B 声明通道（agent 自主权的合法入口）

- `next_hypothesis` 前缀语法：`[灵感重置:literature]` / `[灵感重置:
  random]` + 一句理由。`_surrender_match` 之前先匹配前缀——命中即
  灵感重置声明（不是穷尽宣言，不拒收），引擎 ACK：
  下一轮 `_pick_strategy` 强制挂对应策略（优先级高于 R3-R8，同
  direction_budget 的强制路径），断链重置 arc/簇预算，pending 作废。
- 未带理由或理由为空 → 按普通 pending 回显（不 ACK）。
- 限频（D3）触顶 → 降级 pending_rejected + 点名理由，策略走常规表。
- loop 响应加回执字段：`inspiration_reset: {acknowledged, mode,
  quota_left}`，agent 可见配额（与 pending_rejected 同一可见性原则）。

### WS-C origin 标注（归因可区分）

- `_wrap_diagnosis` / trail 条目新增 `origin` 字段：`"hypothesis"`
  （默认）| `"random"`（explore 幸存者直进管线）| `"literature"`
  （papers 字段非空已有此信息，补显式键保持三值枚举一致）。
- trail_summary / registry 条目透传；不改历史条目（缺键读作
  hypothesis）。

## 2. 不做的事

- 不给 random 幸存者开任何门链豁免（烟测/因果/noise/deflation 全走
  ——随机幸存者的多重检验风险更高，deflation 计价本就全量计数）。
- 不动注入器（TS 零改动已核实：drive.ts 只转发 loop.strategy 原文）。
- 不做 agent 声明"继续当前方向优先级提升"之类的反向通道——声明
  通道只用于换源，不用于续命。

## 3. 部署与验证

- 部署路径勘定（实现时落定）：**无 tgz 环节**——生产 bridge 经
  `python -m dsh_factor_mining.bridge` 启动，包为可编辑安装（pip
  -e 指向主仓库 `projects/dsh-factor-mining-plugin/python/src`），
  pkgs/ 里的 dsh-factor-mining-python tgz 只含 TS provider 壳。
  部署 = 改动 cherry-pick 落 main 树 + 手动重启 dsh web。
- 台账落点实现偏差：random_runs 不进 ledger.json（那是 submit 审计
  流水），落 mining_state 顶层键——与配额计数同一读写基础设施。
- 验证：单测 python/tests/test_inspiration_reset.py（声明解析/写入口
  旁路/ACK/限频/random 配额/断链重置/轮转确定性/origin 三值/紧凑投影
  /registry 透传）+ 全量 pytest 回归。
