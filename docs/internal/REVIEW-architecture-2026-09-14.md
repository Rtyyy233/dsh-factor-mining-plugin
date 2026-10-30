# 架构审查与改进清单（2026-09-14）

- 方法：模块地图 + 数据流走查 + 生产状态文件实测 + 两树对比。全部论断有代码或数据出处。
- 结论预览：**统计内核（计价/门/账本设计）是健康的，不需要动；真正的债务集中在三处——两树分叉治理、静默降级不可观测、状态文件规模化**。另有一项统计一致性问题（跨轨 α 不对称）待拍板。

## 0. 现状地图与关键数字

```
TS 工具层(4 包: fm 0.1.5 / py 0.1.3 / tool 0.1.8 / bundle 0.1.1)
        │  JSON-RPC
bridge.py(7105 行,单类,154 个 except Exception)
   ├─ 引擎核心 factor/: evaluate(DSR) / gates(共享原语) / tailgate(G1-G3)
   │   noise / flatness / permute / random_gen(farm+novelty 源)
   ├─ 账本与补救: state(registry/trail 读写+锁) / journal(v7) / standby(③)
   │   incubate(⑤) / book_ranks(正交门) / discipline(红旗)
   ├─ 执行: worker(子进程池+env npz) / lane / procinfo
   └─ 状态文件群(双 root × 2 线): trail_engine.json 13-15MB / registry /
       registered_ledger.jsonl / journal / pool / null_landscape / book_ranks.npz(192MB)
```

| 指标 | 数值 | 含义 |
|---|---|---|
| bridge.py 行数 | 7105（第二名 evaluate 1384） | 单体集中度 |
| bridge except Exception | 154 处 | 静默降级密度 |
| trail_engine.json | 13-15 MB，每次 evaluate/submit 全量解析 | 规模化税 |
| 两树内容级分叉 | 14 文件（含 bridge/state/worker/filelock/book_ranks + strategy_lab 整包） | 部署分叉 |
| 测试 | 575 绿（+新增缝扫描） | 重构护航能力 |
| worktree 引擎装载 | npz 全量（~9s / worker ~530MB） | 主树 mmap（2.5s/110MB）未回流 |

## A. 事故预防层（最高优先——最近两次事故的直接教训）

### A1. 两树分叉治理

**证据**：09-13 正交门事故的直接根因是修复只落主树未回流 worktree；本轮实测两树仍有 **14 个文件内容级分叉**——worktree 缺主树的 panel_mmap（bridge/state/worker/filelock 的 mmap 改动）与 dsh_strategy_lab 整包；主树靠 cherry-pick 追 worktree。两树各自都是"某段时期的真相"，谁也不完整。zcode 直驱跑 worktree、DSH 跑主树——**同一个门在两个运行路径上行为可以不同**。

**改进**：
1. **收敛为单一事实源**：把 worktree 分支合进 main（merge 或整批 squash），此后 worktree 只做短生命周期特性分支（存在期间必须双向回流，合并即销毁）。这是根治。
2. 过渡期工具：`sync_check.py`——两树 python/src + ts/ 内容级 diff（忽略行尾），输出分叉文件清单与方向；建议挂进会话开场白和收工清单。
3. 规则明确化：任何单点修复提交后，cherry-pick 到另一树是**同一个动作的两半**，不允许只做一半（缝扫描测试防的是"名字对不上"，这里防的是"文件对不上"）。

**成本**：合并动作半天级（冲突主要是行尾+少量真冲突）；工具 1 小时。**风险**：合并冲突需逐个判读（mmap 改动 vs 我的新模块无交集，预期干净）。

### A2. 静默降级可观测性

**证据**：正交门事故在生产跑了 11 笔提交无人察觉——`orthogonality.status=skipped, why=ImportError` 写进了 registry，但没人看：agent 不看、报告不看、遥测不携带。bridge 里 154 个 `except Exception` 全部 stderr-only（部分场合连 stderr 都吞）。设计上"降级不阻断"是对的（评估是事实层），但**降级必须被看见**。

**改进**：
1. 结构化降级日志：`stateRoot/degradations.jsonl`——统一 helper（上下文+异常+时间），替换散落的 stderr write；overnight 报告固定扫一遍。
2. submit 响应顶层加 `gates_health`：`{noise: ran, ortho: skipped(why), flatness: ran, ...}`——五门任何一门 skipped/infra-fail 必须在响应最外层可见，不是埋在字段里。
3. loop telemetry 加门健康位：连续 N 笔同一门 skipped → 直接进 escalation 文案。

**成本**：1-2 小时（helper + submit 汇总 + 遥测位）。**风险**：零（纯观测，不动判定）。

### A3. TS/Python 参数契约测试

**证据**：R17 事故（TS 层未声明的参数被 handler 静默丢弃，轮询变垃圾作业）之后，铁律靠文档记忆；实测 ts/factor-mining 与 ts/tool-factor-mining **没有任何测试**，ts/factor-mining-python 的测试也不含 schema 对账。这是下一个"正交门式"潜伏事故的候选。

**改进**：契约反射测试——扫描 tool 层每个工具声明的 params schema 字段 ↔ python handler 实际读取的 `params.get(...)` 键集合，双向对账（TS 声明未读=死参数；handler 读取未声明=会被丢弃）。跑在 python 测试套件里即可（读 TS 源码做静态扫描，与缝扫描测试同思路）。

**成本**：半天。**风险**：会暴露一批现存不一致（预期有——修复它们正是价值）。

## B. 规模化与健壮性层

### B1. trail_engine.json 增量化

**证据**：13-15 MB JSON，`_trial_stats` 6 个调用点（每次 dev evaluate、每次 batch、每次 submit 重算都全量 `json.loads`+dict 构建）。单次解析 ~200-400ms，一个挖掘夜几百次 evaluate = 分钟级纯税，且随试验数线性恶化。3688 条里每条带 sketch（主要体积来源）。

**改进**（按成本递增三档）：
1. **进程内 mtime 缓存**（最便宜）：Bridge 实例记住 (mtime, parsed)；一次 submit 流程内多次读直接命中。~1 小时。
2. **追加式迁移**：新写 jsonl（对齐 registered_ledger.jsonl 模式），读侧只 tail；旧 json 一次性转换。半天。
3. **distill 式归档**：老条目 sketch 压缩/抽样（E[max|X|] 只需要相关性量级，60 点够用——已有 `_MIN_ALIGN=60` 共识），控制文件上限。

**风险**：低；注意 CRN 采样器 sync 的键序依赖（jsonl 保序即可）。

### B2. 正交门候选秩计算的超时保护

**证据**：submit 路径的正交门在 **bridge 主进程 exec agent 源码**（bridge.py:6327 `exec(source, ns)` 后算 dev 秩），无 worker、无超时——一个慢/死循环因子会挂起整笔提交（其它四门都走并行 worker 有预算）。这是五门里唯一裸奔的。

**改进**：复用 `_run_factor` 的 worker 通道+预算自适应（模式现成）。~2 小时。

### B3. worktree 引擎缺 mmap

已并入 A1（cherry-pick panel_mmap 到 worktree 或直接走合并）。单独看收益：直驱会话装载 9s→2.5s、内存 530MB/worker→110MB。

## C. 统计一致性层（动门或动消费口径，逐项需拍板）

### C1. 跨轨 α 不对称

**现状**：IC 轨 deflated p 判 α=0.05（evaluate.py:1239）；尾轨 G3 用跨轨 Šidák α_track=0.0253。tailgate 注释明说设计意图是"双通道准入=两次通过机会，α 对半分裂"——但只有尾轨执行了分裂，IC 轨还是 0.05。

**白话说**：一个因子有两条路入册（IC 显著 / 尾部组差显著），家族错误率按"每条路各付一半"控制才对得上 0.05 的总预算；现在 IC 那条路付了全额 0.05、尾部那条付 0.0253，合计 1−(1−0.05)(1−0.0253)≈**7.4%**，比声明的 5% 预算超了约一半。量级不大，但它是个没闭环的设计意图。

**选项**：①IC 轨也降到 0.0253（严格对齐意图，IC 轨门槛略升）；②显式接受 7.4% 作为双轨总预算并写入门槛总账（诚实标注，不动门）。我倾向 ①——改动一行+任务书记录，语义干净。注意这是动门，须按纪律（任务书+影子或直接拍板）走。

### C2. 消费端家族代表视图

**现状**：账本现在有 28 只（IC 线），其中 10 只是标注过的家族冗余——"同族=同一张票"目前靠纪律和 notes，策略层消费时没有机械工具。

**改进**：`book.representatives()`——book_ranks.npz 的秩矩阵现成，对在册全体做贪心聚类（|ρ̄|≥0.6），每族返回代表（按 |IC_IR| 或 dual_pass 优先）+成员清单。策略层/RUNBOOK 消费走这个视图，把"按单一有效假设消费"从纪律变成 API。半天，纯派生只读，不动门。

### C3. 噪声门低功效放行标注

**现状**：预算自适应会把噪声门截到 m=18（请求 50）——registry 里有 n_valid 但"低功效下的放行"没有显式标注位（如 `noise_gate.power="truncated-18/50"`）。统计上截断只降判定力不放水，但消费端读账本时应一眼看到哪些入册是低功效判定。1 小时。

## D. 工程卫生层（不疼不动，按需排）

| 项 | 内容 | 何时值得做 |
|---|---|---|
| D1. bridge 拆分 | submit 管线（五门编排+事务语义）与 post-eval 钩子链（journal 镜像/台账/影子计价——实现期就因缩进/顺序出过三次错）抽独立模块，钩子显式排序+独立错误隔离 | 下次再出"钩子顺序/作用域"类 bug 时；575 测试护航下可安全做，但无现实痛感前不动 |
| D2. 配置面收敛 | 156 处 params.get、mining_state 键散落各处（noise_gate_m/day_perm_m/tail_placebo_m/novelty_lambda/...） | 加新配置项再感到疼时：MINING_CONFIG 单点声明+校验 |
| D3. ARCHITECTURE.md | 模块/状态文件/数据流一张地图（门槛已有总账，模块无地图） | 便宜（1-2 小时），建议下个会话顺手 |
| D4. TS 测试覆盖 | tool/fm 两包零测试（契约测试 A3 覆盖最危险的一块） | A3 之后的增量 |

## 建议执行序

1. **本周**：A2（可观测性，1-2h）→ A1 收敛合并（半天）→ sync_check 工具（1h）；
2. **随下一次挖掘夜**：B1 档1（mtime 缓存）、C3（标注位）——都便宜；
3. **需拍板后**：C1（α 对称，一行+纪律）、C2（代表视图，半天）；
4. **排期池**：A3（契约测试半天）、B2（正交门超时 2h）、B1 档2/3、D3；
5. **不动**：D1/D2 直到出现新的现实痛感。

## 明确不列为改进的

- 统计内核（E[max|X|] 采样器/DSR/三尾门/正交门设计/分层影子）——本轮两轮审计（计价审计+门槛总账）后的结论是设计健康，刚修完的地方不折腾；
- 批次 2 翻转、掘金日更、DSH 重启——已排期事项，非架构缺口。
