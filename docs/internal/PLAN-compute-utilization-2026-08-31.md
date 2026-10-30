# PLAN — 计算资源利用效率（层 0-3）＋ bridge 阻塞根治

日期：2026-08-31 ｜ 状态：设计定稿待实施（用户已认可方向，本文含自审发现）
前置：PLAN-lane-isolation-2026-08-31.md（lane 已上线）、杀进程事故修复（client 0.1.2）
机器：16 逻辑核 / 63GB RAM（工作时段空闲 ~40GB）

## 1. 问题与账

**症状**（两会话全天 factor_* 耗时实测）：evaluate/causality/batch 单次 150-322s；
双会话并行后所有调用排队——`factor_record_trail`（本应 0ms）实测排 99s/274s，
`factor_status` 排到 360s 超时；超时不取消在途计算 + agent 按指引重试 → 队列滚雪球。

**根因链**：
1. 单线程 bridge 串行 dispatch——同一时刻至多 1 个同步 worker + 2 个异步作业
   （`_MAX_ASYNC_JOBS=2`），16 核常态只烧 1-3 核；
2. 全A个股面板（1084 万行）单次评估纯计算就要 2-5 分钟；
3. 每次评估重复支付固定税：267MB env.npz 压缩写盘 ~10s + worker 冷启动/解压 ~20-30s。

## 2. 拍板记录（2026-08-31）

- 层 0-3 全部实施；层 4（常驻热 worker 池）量了再上（v2）。
- 参数：并发容量 **K=6**（evaluate 当量）、队列深度 **Q=20**、
  客户端超时 **900s**、worker 墙钟 base **600s**（见 §4 R1——900/600 是耦合决策）。
- 定价口径不动：n_trials/deflation 全局；本计划全部是调度层改动。

## 3. 分层设计

### 层 0a — 超时上调（耦合三件套，缺一不可）
- `cordis.patch.yml` factor-mining config 加 `requestTimeoutMs: 900000`；
- worker 墙钟 base 300s→600s：bridge 读 `DSH_FACTOR_WORKER_TIMEOUT_MS`
  （`Bridge.__init__` 默认 600_000；jobs 伸缩公式不变：`base × cpu/jobs`）；
- 余量关系保持：request = worker_base × jobs 伸缩上限 + 60s。
- **R1（自审发现，0a 原方案有洞）**：360s 超时来自插件自身默认
  （`requestTimeoutMs: z.number().default(360_000)`，注释明写 360=300+60），
  而 worker base 300s 才是真执行上限——09:14 实测 322.5s 的 evaluate 已贴
  jobs 伸缩后的 ~320s 线。**只调客户端超时，慢因子照样被 worker 墙钟杀**。
  两数必须一起动，且 worker base 目前无配置口（新增 env）。
- host 侧 timeout-policy 按 per-tool `timeoutMs` 声明启用，factor 工具未声明
  → 不构成第二道帽（已核实 guard/timeout-policy 源码）。

### 层 0b — env.npz 指纹缓存
- `_run_worker` 的 `write_env_npz(wdir/env.npz)` 改为内容寻址缓存：
  `stateRoot/worker_env/<fingerprint>.npz`（+ `.meta.json`）。
- 键 = `_env_full_fingerprint(env_id)`（数据文件内容+口径+引擎版本——
  trail 条目每评必算，无新增成本）；写一次终身复用，变更=新文件。
- 写序：meta 先、npz 后（npz 存在性 = commit 标记）；tmp+os.replace 原子；
  并发写竞争幂等（同字节，同 store_factor_source 模式）。
- 写成功后清理非当前指纹文件（每个 267MB，防积累）。
- 顺带消灭截断文件事故形态（2026-08-31 11:27 被杀时的半截 npz）。

### 层 0c — `factor.evaluate` 异步化
- 进 `_HEAVY_METHODS`；提交即返回 `{job_id, status, note}`（batch/composite
  同款契约，note 自教轮询）。
- test 锁 claim-then-compute 前移到 **submit 时同步完成**（worker 内检查不变）；
- 计算在 job 线程的 `_run_worker`（现状机制），trail 写入/池对表/loop 指令
  在轮询取回时（`_async_result` → `_wrap_diagnosis`，lane 归提交线——已上线）；
- **R2（契约分叉点，必须自描述）**：eval_cache（C1）命中时**同步直接返回
  diagnosis**（重算才是浪费），未命中才返回 job_id。响应自带判别字段
  （有 `job_id` → 轮询；有 `verdict` → 缓存命中即终态）。SKILL.md 与
  factor_evaluate 工具描述同步改写。
- walk_forward / noise_test / day_perm_test / check_causality / registry_submit
  本期保持同步（受 0a 保护）；causality 若持续 >600s 列 v2 异步化候选。

### 层 1 — 并发槽 + 真队列
- `_MAX_ASYNC_JOBS=2` 语义升级：**容量 K（权重计）+ 队列 Q**；
  槽满不再拒绝，入队返回 `{job_id, status: "queued", queue_position}`；
- **R3（权重防超订）**：batch 内部还吃 cpu-1 核池。v1 权重：
  evaluate/audit=1、composite=2、batch=4；容量按权重和 ≤ K=6 判定。
  接受轻度超订（effective_wall 按 jobs 伸缩已兜底），动态核分配列 v2；
- job.json **入队即落盘**（status=queued→running→done/failed）——顺带修复
  现存「提交与轮询的竞态」（今天靠 `_async_jobs` 内存字典兜底）；
- 桥重启收尸：启动时把盘上 queued/running 作业标 failed（零入账，agent 重试）；
- status 的 workerRuns 增队列段：排队作业列表 + 位置；
- 配置：`DSH_FACTOR_SLOTS`（默认 6）/`DSH_FACTOR_QUEUE`（默认 20）。

### 层 2 — 在途去重
- 提交时按 `(method, envId, source_hash, stage, horizon, 关键 params)` 查
  在途表：命中返回**同一 job_id**（响应注明 dedup: true）；
- 消灭重试风暴重复计算（「超时→立即重试」今天会把同一因子算两遍）；
- 跨线去重是特性：同因子=同试验（内容寻址），trail 归提交者 lane，n_trials 不变；
- 完成后重复靠既有 eval_cache（C1），在途表只管飞行中。

### 层 3 — 按 lane 公平调度
- 出队时 lane 轮转：跳过连续同 lane 作业（有其它 lane 在等时）；
- lane 已随 job.json 携带（lane 隔离上线时做的），零新增管道。

## 4. 自审发现汇总（按严重度）

| # | 发现 | 处置 |
|---|---|---|
| R1 | 只调客户端 900s 无效——worker base 300s 是真上限，且无配置口 | 0a 改为三件套耦合改 |
| R2 | evaluate 缓存命中同步返回 vs 未命中 job_id 的契约分叉 | 响应自描述 + SKILL 同步 |
| R3 | batch(内部15核池) 与 evaluate 抢槽会超订 | 权重制 1/2/4，接受轻度超订 |
| R4 | test 锁 claim 必须前移 submit | 已入 0c 设计 |
| R5 | job.json 落盘竞态（现存） | 入队即落盘顺带修复 |
| R6 | 桥重启丢内存队列 → 孤儿作业 | 启动收尸标 failed |
| R7 | infra 指引「可立即重试」在队列架构下应先看 workerRuns | 指引文案随本计划改 |
| R8 | 取消语义（queued 可删/running 不可杀） | v1 不做，agent 不用 cancel |
| R9 | npz 缓存无上限积累 | 写后清非当前指纹 |
| R10 | job 线程与主线程状态边界 | 重申不变量：线程只跑 _run_worker，poll/dispatch 全在主线程，队列操作进 _async_lock |

## 5. 明确不做（v2 候选）

常驻热 worker 池（省 20-30s 冷启动税，代价是放弃 D3b 每请求 OS 限额）——
等 0-3 落地后用 out.json 分段计时实测占比再定；causality/noise 异步化；
动态核分配；作业取消；每会话独立 bridge。

## 6. 测试计划

- 单元：队列权重/公平出队/去重命中/收尸（假 job.json）；npz 缓存命中与指纹更换；
- 集成（vitest + worktree python）：双会话并发 submit→poll 全链路；
  evaluate 缓存命中同步返回；杀 bridge 后 respawn 收尸；
- 压测回归：3×8 并发提交不丢作业（P5 同款方法）；
- 既有全量回归绿（test_lane_isolation / test_loop_directive / 其余）。

## 7. 部署与回滚

- 一次部署四件：Python（main cherry-pick）+ TS（tgz 升 0.1.3：evaluate 工具
  描述与轮询引导）+ SKILL.md（契约分叉说明）+ cordis.patch.yml（900s）；
- 回滚开关：`DSH_FACTOR_SLOTS=1` + `requestTimeoutMs` 回 360000 即回到
  现行为（evaluate 异步不可配回退——如需，加 `evaluateSync` 逃生阀再议）；
- 重启由用户执行；进行中会话的作业在旧进程里，重启即收尸（零入账）。

## 8. 验收

- 双会话并行：`factor_status`/`factor_record_trail` P95 < 2s（不再排队）；
- 6 个 evaluate 并发在跑时 CPU 利用 ≥ 6 核；
- 同一 factor 重试风暴下 worker_runs 无重复计算；
- 队列深度 > 0 时 status 可见，且出队按 lane 轮转。

## 9. 实施记录（2026-08-31 当日落地；测试逮出的设计外问题）

| # | 问题 | 处置 |
|---|---|---|
| R11 | `_enforce_causality` 在提交路径同步跑 worker（自审漏网）——新源码因果检测 30-270s 会击穿「秒回提交」 | 因果门随作业线程执行（`causality_gate` 元数据；FUTURE_LEAK 轮询时以作业失败文案送达）；composite 同步对齐 |
| R12 | job.json 读/写竞态：Windows 下目标被并发读者打开时 `os.replace` 立即失败，`_finalize_kept_run` 静默吞错 → **作业永卡 running** + tmp 残留（smoke 测试逮住，间歇复现） | `_job_json_write` 统一原子写 + PermissionError 重试（10×20ms）；finalize 失败 stderr 留痕；`_async_result` 内存登记兜底恢复 |
| R13 | pump 启动循环重复两遍（编辑事故）→ 每个出队作业双启动 | 测试逮住（同 run_id 两条 FAKE 记录），删重复 |
| R14 | env 指纹含冒号（`hash:hash:ver`）是 Windows 非法文件名字符 → 缓存永远走 ephemeral 回退 | 文件名清洗（非 `[A-Za-z0-9._-]` → `-`） |
| R15 | 直接启动路径不推进 `_async_lane_last` → 首次 pump 按 FIFO 取同线，公平性失效 | submit 的 start_now 分支同样记录 lane_last |
| R16 | `worker_timeout_ms` 签名默认 300_000 短路了 env/600s 解析链 | 签名默认改 None，解析链生效（测试锁定默认 600s + env 覆盖） |

| R17 | TS 工具层参数 schema 未声明 job_id——轮询被静默丢弃退化成空 source 垃圾作业提交（生产实证：3 个同名 running）；composite/batch/audit 的 job_id/async 自 08-29 起同样未接通 | 四工具 schema+转发补齐（TS 0.1.4）+ Python 空 source 无 job_id 拒绝 |
| R18 | **batch 去重键恒定**（source 恒空串）——第二个 batch 并入第一个的作业拿到错误结果 | 键补 sources 指纹（异不去重/同去重，回归锁定） |
| R19 | horizon 申报/scan 从未在工具层暴露（v2 菜单系统 dsh web 不可达） | evaluate 工具补 horizon 参数+转发 |
| R20 | 异步族工具 source required:true 拦掉纯轮询调用 | 去 required，Python 空源防线兜底 |
| R21 | worker_env 清扫只在写缓存时跑，ephemeral/tmp 无限滞留 | 无条件清扫（ephemeral 仍按 1h 年龄） |
| R22 | composite 去重键漏成分表（ingredients 只在 params 不在 source）——固定 overlay 换成分扫描（C1/C2 标准工作流）时第二个 composite 并入第一个在途作业、拿到**错误成分的结果** | 键并 `ing:` 成分表指纹（2026-08-31 全弧 review 发现；R18 的 composite 面，回归锁定异不去重/同去重） |
| R23 | done 作业每次轮询都重跑 `_wrap_diagnosis`（trail 写入副作用）——条目挪到 trail 队尾：同线已有更新试验时重轮询旧作业 = 伪断链 + arc 误归零 + latest5 失真（n_trials 有 (hash,stage,horizon) 去重不膨胀；C1 缓存命中路径同款「重评即更新」语义是 08-29 前既有行为，非本次回归） | 首次 wrap 快照落 `diagnosis.json`，后续轮询（含去重并入者）直接取快照——同一试验永远同一份入账数字（红验：摘快照早退则 trail 顺序断言必红） |

## 10. 隔离复审轮（2026-08-31 晚，四路零上下文 agent 冷审 + 交叉裁定）

方法论：4 个互不知情、不携带本 PLAN 结论的评审 agent 分别冷审异步基础设施 / lane 语义 / TS 跨层契约 / 校准门口径；两名 agent 独立以可复现实证撞出同一 P0（异步 batch 语义断裂）。全部发现经人工逐条验证后分级处置。

| # | 问题 | 处置 |
|---|---|---|
| R24 | **异步 batch 系统性语义断裂（P0×2）**：async 分支在逐成员因果门/horizon 校验/pool_std 注入之前 return——FUTURE_LEAK 与 A 类反模式照评（因果门被一条参数旁路）；A1 成员计账全部不可达 + 轮询把批信封当单因子 wrap 出一条空 hash 幻影条目（deflation 少计 → 错误准入）；job_id 取回放在 _require_panel_env 之后（重启后收尸被面板重建/校验挡死）。附带：批成员 _construction_fp 用陈旧 src——全批共享末成员血缘指纹（sync 路径 08-25 起就有） | handler 重构：job_id 前置 → 校验/注入 → async 提交（带 causality_gate+causality_sources）；后处理抽 _postprocess_batch_result（sync 与轮询共用，含 src 先赋值）；_ASYNC_STAGE 摘除 batch，轮询侧走专用分支 + diagnosis.json 快照幂等 |
| R25 | _async_run 因果门只支持单 source——batch 多源没法随作业线程执行 | causality_sources 清单逐成员过门（与同步路径全覆盖对齐） |
| R26 | _same_trial 替换跨线迁移条目 lane：B 线重评 A 线试验后 A 续写 own_tail 回退异族旧条目 → 伪断链 + arc 误归零（隔离评审双 repro 实证；「重评归提交线」未被任何注释认定为拍板语义） | 重评（prior 存在）保留首创线 lane + 不参与断链判定（重评不是新试验）；计价层 (hash,stage,horizon) 全局去重语义不变 |
| R27 | failed 作业轮询统一 -32005：域错误分类失真 + dispatch 对 -32005 记 infra 台账，重复轮询同一失败作业刷屏污染 loop 超时纠偏声道 | _mark_run_failed 存原生 error_code，轮询按码重现；真 infra（无码）回落 -32005；墓志缺失时 stderr 留痕 |
| R28 | _prune_kept_runs 纯按 mtime 剪：排队作业（mtime=提交时刻恰最老）与长跑作业的 run 目录被活删——丢 source/lane/envId 致轮询侧错账，或 pump 启动即炸 | 非终态（queued/running/submitted/无 status）目录永不剪 |
| R29 | _async_start 异常未捕获：job.json 写失败/线程启动失败时权重永久泄漏 + dedup 键永久投毒 + pump 批量启动被单个失败炸断 | 启动失败回滚（权重/登记/dedup 三归还）+ failed 墓志，不向上抛 |
| R30 | G2 噪声门 spread 统计量漏绑 calibration.tail_k（8d87a0c k_frac 可配化时唯独此门未接）：生产 k_frac=0.1 下门在测 top-20% 组差、判的处处是 top-10% | 两处分发（bridge in_process + worker）绑 k_frac=env.calibration.tail_k；顺带清掉 dc6a42a 已废弃的 net_cost 死分支与误导注释 |
| TS-R24 | composite 轮询（job_id only）在工具层被无条件 jsonRecord(args.ingredients) 炸断（工具自己的 description 教的调用方式必错）；batch sources required:true 拦掉纯轮询；batch horizon 从未在工具面声明；registry_submit 未盖 lane（响应内嵌 default 线的 loop 指令） | TS 0.1.6/0.1.4：composite 条件转发 ingredients、batch sources 可选+声明 horizon、evaluate 声明 async 逃生阀、registry_submit 盖 lane；e2e 双锁（composite/batch 仅 job_id 轮询过真桥） |

| R31 | **生产双实证（19:20/20:36）**：worker 子进程继承宿主控制台——宿主重启 Ctrl+C、宿主侧 pwsh 等待被取消等控制台事件打到同控制台所有进程，在途 worker 被杀（rc=0xC000013A STATUS_CONTROL_C_EXIT + KeyboardInterrupt traceback；20:36 那次 agent 还把文案误读成「内存问题」） | worker Popen 加 `CREATE_NO_WINDOW`（win32）：无控制台可被打；宿主真重启时由 Job Object KILL_ON_JOB_CLOSE 兜底（干净的生命周期语义不变） |
| R32 | 因果门阶段（大面板 30-370s）不留痕——job.json 显示 running 但无进展，生产 agent 观测 bridge CPU 近零后误判「排队中」；且 rc=0xC000013A 无识别、错误文案误导向 OS 限额 | 门起止写 `phase=causality_check/evaluating` 进 job.json；无输出分支识别 0xC000013A/KeyboardInterrupt → 文案直指「控制台事件击杀，非因子/内存问题，零入账原样重试」 |

生产账本修复（同日）：19:15 批次旧代码轮询产生的空 hash 幻影条目已移除（46→45，备份 trail_engine.json.bak-20260831-repair1915）；该批 11 成员实为因子实现错误（env 下标访问）——零入账本就正确，无漏计。

### 遗留登记（隔离复审发现、本轮不修）

- **校准线（P1×2）**：① null 校准工具链不传 spread_cost——net 版 landscape 无法经工具复现，指纹变化触发重校时 spread 段静默回毛口径而 tail_net_basis 不变，G3 成本对称性从后门破缺（当前生产 net landscape 是工具路径外生成的）；② light_ic_scan 拿毛 spread_ir 对 net 段 knots 查分位，explore 的 spread_pct 系统性虚高约 40 分位点。需与重校流程一起设计，不是一行修。
- **异步健壮性（P2 簇）**：_run_worker running 墓志写失败静默 run_id=None → 成功作业永卡 running；pid_alive 异常返 False 与注释相反（respawn 重叠期可误标活作业）；PID 复用漏杀；dedup 键用 envId 非内容指纹（数据热重载窗口）；双环境交替时 env.npz 缓存互删可击杀在途读者；因果门 worker 不吃权重槽。
- **lane 语义（P2 簇）**：pass_unadmitted 混全局 accepted_n 与本线 pass_n；diagnosis.json 快照把提交线 loop 指令冻结给异线轮询者（意图层不脏但响应串话）；state_reset 无 lane 边界（registry 非空硬拒已部分兜底）；record_round 返回 arc 恒 0（迁移后读顶层键）。
- **client.ts（P2 簇）**：#start 同步 throw 时 rejected #ready 被永久 memoize（永不 respawn）；pending 清理在陈旧句柄守卫外（startup 超时+慢退出窗口误拒新桥在途请求）；dispose 后竞态调用可泄漏孤儿 bridge。

刻意不暴露（审计确认）：state_reset 的 confirm/reason——2026-08-22 反洗账守卫要求用户显式授权，工具层暴露 = agent 可自助授权；e2e 反向断言锁定。

测试：`python/tests/test_compute_queue.py`（11 例：槽/队列/容量拒绝/权重防头部阻塞/在途去重/lane 公平/启动收尸/npz 缓存命中/worker 模式 e2e 异步契约/async:false 逃生阀/in_process 恒同步/超时默认）+ smoke 测试改异步轮询契约 + 全量回归绿。

## 11. 效率编译层：α 烟测门 + 自动 Numba + LLM 重写（2026-09-01）

### 动机（2026-08-31 生产三连事故）

挖掘 agent 习惯第一版写嵌套循环因子 → check_causality（8× factor(env)）烧满 640s 才被发现 → 向量化重交——每轮浪费 10.7 分钟，同一会话连发三次。根因是时序缺陷：三道既有防线（B 类嵌套 for 警告 / factor_perf 计时 / 超时根因分流）全部排在计算烧完之后才可见。用户定调：小模型既不遵循 prompt 效率纪律、自身优化能力也弱——**修引擎，不修模型**。

### 实证先行（tmp/numba-spike/，生产面板 2574×5347 实测）

- **Numba spike**：纯标量嵌套循环（事故主力形态）njit 后 36×/797×（全面板 0.26s/1.33s）、数值等价 4/4、编译 0.55-12.1s；算法慢类（T²N expanding 排序）njit 负优化（0.8×）——**α 拟合缩放指数（实测 1.61）恰好能识别这类**；numba 0.67 直接吃生产 numpy 2.4.4 / py3.14；已向量化代码编译失败快速无害（0.01-0.4s）。spike 自身两次跑飞（线性外推放行 T²N 怪物）逼出 α 拟合门设计。
- **mimo-v2.5 重写能力门（用户指定模型；glm 系禁用=太贵）**：T3（EWM 递推滤波，最难）严格等价重写一发过（est 9.1s→1.18s）；T2（行算子 nanmean 类，主战场）两轮未过；T1（算法慢类）产码失败。谎报 EXACT 与漏 DECLARE 都被验证兜住。附带坑：Cloudflare 拦默认 UA（403 code 1010，必须自定义 UA）；推理模型 8k max_tokens 会被烧尽（content=null，需 16k + 处理）。

### 设计（用户拍板：仅严格等价自动换用）

```
提交 → _run_worker 入口（所有执行的单一 choke point：同步/异步/因果门内嵌全覆盖）
  ① 透明换用（optimizer_cache 命中严格等价重写 → 换用，hash/registry 锚定原源码）
  ② α 烟测门（fail-open）：512×128+256×64 双尺寸实测 → α 拟合外推 × 方法乘数
     ├─ est ≤ 预算×0.5 → 直跑
     ├─ njit 可救（剥白名单 import 后编译过+等价+编译版进预算）→ params[smoke_engine]=njit
     └─ 超预算 → -32003 零烧名拒绝（实测数字/α/循环行号/指导/重写状态）+ 自动排 engine.rewrite 作业
engine.rewrite（异步权重 1，不走 _run_worker）：mimo-v2.5（≤2 次尝试带错误反馈）
  → 安全扫描（仅 np/pd/math、禁 import/危险调用、≤200 行）→ 烟测 worker 子进程内
  allclose(rtol=1e-6) 严格等价 + 重测投影 → 采纳入 optimizer_cache.json（FIFO 128，
  持久化）/ 失败也记录（同类源码不重烧模型）
```

关键决策：重写换用发生在执行层而 registry/eval_cache/账本 hash 全锚定 agent 原源码——重写与 njit 同类（验证过的等价实现变换），diagnosis.perf 带 rewrite_provenance 溯源；APPROXIMATE 声明永不自动采用（验证才是裁决，声明仅参考）。方法乘数=代码事实：causality 8 / audit 10 / noise 10×240s 特例 / evaluate 族 1×份额 0.5 / composite K+1 / flatness 1+2×参数数。

### 实现

- `python/src/dsh_factor_mining/smoke.py`（新）：尺寸/预算常量、env_subsample、EnvView8/9（namedtuple，numba 两栖属性访问）、fit_alpha/extrapolate（钳 [0.8,2.5]）、method_cost、classify_gate 纯函数、njit_prepare（剥 numpy/pandas/math 裸 import——平台因子惯例是函数内 import，numba 不支持 import 语句，生产真实源码全卡这步）。
- `worker.py`：内部方法 `factor.smoke`（gate/verify 两模式；外推判定全在 bridge 侧）；_compile_for_njit + _njit_accelerate（进程内指纹 memo、任何异常永久回退解释版）；perf 增 smoke 块/engine/rewrite_provenance。
- `bridge.py`：_run_worker 入口钩子（换用+门）+ in_process 分支同覆盖；timeout_s 覆盖参数（烟测 60s 兼抓死循环）；_smoke_env_npz（worker_env/<fp>.smoke.npz 内容寻址，R9 清扫豁免）；_smoke_gate/_smoke_reject_message/_rewrite_status_text/_maybe_enqueue_rewrite/_run_rewrite_job（engine.rewrite 分支进 _async_run）；optimizer_cache.json 惰性加载+FIFO。
- `rewrite.py`（新）：urllib 客户端（自定义 UA/16k tokens/网络重试/空 content 处理）、生产 prompt、安全扫描、parse_reply（DECLARE 缺席容忍）。
- 配置：DSH_FACTOR_SMOKE=1 / _SMOKE_MIN_CELLS=200000（测试小面板天然豁免）/ DSH_FACTOR_REWRITE=1（无 key 静默降级）/ _REWRITE_MODEL=mimo-v2.5（glm 前缀强制回落）/ _REWRITE_BASE_URL。pyproject 加 numba>=0.61（机器级已装 0.67.0）。

### 测试与部署

- `tests/test_smoke_gate.py`（12）+ `test_numba_layer.py`（4）+ `test_rewrite_layer.py`（5，全程假 LLM 零真实调用）：α 数学/三档/乘数、端到端拒绝零烧名、缓存命中、fail-open、小面板豁免、死循环识别、njit 真编译端到端（含与向量化等价实现的 IC 交叉对照）、重写全环（安全拒绝/谎报戳穿/透明换用/溯源/落盘）、glm 误配回落。全量回归 **517 passed, 1 skipped**（496 存量零误伤）。
- 部署：worktree commit → cherry-pick main（editable 即生产）；numba 已装机；SKILL.md L22 硬条款（第一版就向量化/拒绝后等 1-3 分钟原样重交）+ L23/L86 更新；TS 0.1.7（check_causality/evaluate 描述加引擎自动加速与烟测门行为；镜像构建+tgz+pnpm add 已装验证）；生产生效待用户重启。

### 已知边界

- 外推低估由 α 拟合+外层 640s 双兜底；α 在亚毫秒计时上噪声大（双跑取 min 缓解，spike F1c 实证）。
- optimizer_cache FIFO 驱逐后，注册过的慢源码在 submit 各门重评时会再撞门（重写作业重排，瞬态拒绝可恢复）——128 槽对活跃源码规模余量充足。
- in_process 模式烟测探针仍走子进程，但主执行无超时保护（调试模式既有风险，注释注明）。
- 重写作业失败为永久（同指纹不重烧 mimo）；temperature=0 重试产出高度相关，视为特性。

### §11 并发复审与修复（2026-09-01 同日第二轮）

并发面逐一审计（锁纪律/共享状态/竞态窗口）后确认无正确性缺陷，修掉 5 个非阻断问题：

| # | 问题 | 修复 |
|---|---|---|
| F1 | 同 key 烟测 check-then-act 无在途互斥——并发线程各 spawn 烟测 worker（重复 ~5s） | `_smoke_inflight: {key: Event}`：先到者登记+跑烟测，后到者 `wait`（≤烟测超时+60s）后直接读缓存；先到者失败后到者放行（fail-open 不变）。回归：test_gate_concurrent_single_smoke（双线程断言仅 1 次 spawn） |
| F2 | 烟测/验证子进程不计权重槽——burst 下 6 主 worker+6 烟测 worker 瞬时超订 | `_run_smoke_worker` 统一出口 + `Semaphore(2)`：瞬时 smoke 子进程 ≤2；等待上限 120s 超时按 infra fail-open。gate 与 rewrite 验证调用都走此出口 |
| F3 | 拒绝消息在 enqueue 前拼好「已自动提交重写作业」——队列满时 enqueue 静默失败则文案失真 | `_maybe_enqueue_rewrite` 返回真实状态（enqueued/inflight/cached/disabled/submit_failed），消息按状态生成；cached+ok（重写版对更高乘数方法仍超预算）给专门文案「重交无益，需更激进重排」 |
| F4 | engine.rewrite 的 job_id 可从 workerRuns 看到并被 factor_evaluate 轮询——纯取回路径返回伪 diagnosis | `_async_result` 对 method==engine.rewrite 返回信息性摘要 `{"job_id", "engine_rewrite", note}`（明确非评估结果）。回归：test_rewrite_job_poll_returns_informational |
| F5 | batch 慢成员被拒时消息不点名成员 | `_enforce_causality_member` 包裹（sync 逐成员循环 + _async_run 多源循环两处）：拒绝带「批成员「名」:」前缀（A 类硬拒同样受益）。回归：test_batch_member_named_in_reject |

复审确认安全的面（未改动）：`_optimizer_lock` 全访问点在锁内且 `_async_submit` 在锁外（无锁序反转）；`_NUMBA_MEMO` 进程内单线程且 batch 池子进程永不接收包装（无闭包 pickle 问题）；smoke npz 并发写幂等 + Windows replace-撞打开文件失败被捕获走 fail-open；params 变异单消费者；enqueue 查重+登记原子（双提交不重复烧模型）；hash/eval_cache 锚定跨重写完成收敛。备忘：rewrite 作业权重 1 占槽 1-3 分钟为设计内（网络等待为主，自限一次/源码）；测试补 async njit 路径（test_gate_engages_njit_async_path）。

测试：三文件 21→25 例 + 既有 63 例（含 test_efficiency_p4/test_compute_queue 受影响面）全绿；全量回归见提交。
