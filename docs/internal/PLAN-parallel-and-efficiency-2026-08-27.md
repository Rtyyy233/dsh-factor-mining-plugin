# 规划书：多任务并行化 + 计算效率约束（P0-P5）

日期：2026-08-27 · 分支：worktree-residual-holding-correlation · 状态：已拍板，实施中
前置讨论：本文件是「因子挖掘并行三层方案 + 超时二维判据 + 效率四层约束」讨论的定稿任务书。

## 背景与目标

因子挖掘要支持多任务并行。核实结论：worker scratch（mkdtemp）、tmp+os.replace、env 只读缓存、熵源 seed 天然并行安全；但所有状态写入（trail/双池/registry/mining_state）是读-改-写全文件，并发追加互相覆盖丢条目；两侧 test_lock 消费均有 TOCTOU 窗口；策略 run_dir 秒级时间戳同秒碰撞；唯一现存锁（bridge 启动 PID 锁）只挡第二个 bridge 进程，不保护写入。

交付五件事：

1. **层 1**：单会话计算并行——evaluate_batch 进程池化
2. **层 2**：同 stateRoot 多进程安全——变更级写锁 + test_lock 原子消费 + run_dir 防碰撞 + 杀进程树补齐
3. **超时二维判据**：墙钟硬顶 + CPU 时间归因，CPU 饿死→infra_failure；墙钟上限按声明份额伸缩
4. **效率约束四层**：AST 硬拒 / 机器校准 CPU 预算 / 效率入账 / 正向引导
5. **层 3 纪律成文**：并行纪律（env identity 规则、共享账本诚实、jobs 分核）

## 决策记录（全部已拍板）

| # | 决策 | 结论 |
|---|------|------|
| D1 | 超时判据 | 二维：墙钟硬顶保留 + CPU/墙钟比值归因（≥0.7 视为真慢；低比值视为饿死；中间保守按真慢但附数据） |
| D2 | 饿死动作 | 立即报 infra_failure（不自动重试）：错误带 cpu/wall 实测数 + 「重试同一因子不计新账」指引；零写入不计账；计入 loop.infra_failures |
| D3 | 墙钟伸缩 | effective_wall = clamp(base × cpu_count / jobs, base, 4×base)；jobs=DSH_FACTOR_JOBS 默认 cpu_count−1（≈1×base）。仅因子侧 batch 生效（策略侧单 worker 无自超订，不加自动伸缩，仅可配超时） |
| D3a | A 类硬拒 | 五项：iterrows / itertuples / apply(lambda) / applymap / 循环体内 concat-append（pd.concat、np.concatenate、np.append）。causality 前置拒绝评估、不计 trial；B 类（嵌套 for 等）保留警告 |
| D3b | 机器校准 | 一次性参考负载 → ref_cpu_s 存 stateRoot/machine-calib.json（带引擎版本）；预算用绝对 CPU 秒，校准用于相对成本（cpu_rel）与跨机可比 |
| D3c | 成本入账 | v1 只展示不参与 3/6/9 升级阶梯；trail 记 cpu_s+wall，registry 记 cpu_rel |
| D4 | 入册效率门 | 不加新硬门——噪声门 blocked 路径换 CPU 尺子后即天然硬门 |
| D5 | 第二 bridge | 放行：启动独占 PID 锁退役，改为变更级写锁；活跃实例登记为报告性信息 |

## 非目标

- null landscape（null 校准）并行化——一次性成本且已缓存，后续需要再做
- 全局排队信号量（跨进程 slot）——v2；二维判据落地后假超时危害已消除
- 跨 stateRoot 的 N_eff 聚合——纪律先行（P5），机制后续
- daemon/服务化
- 生产因子状态迁移——生产 root 只读铁律不变

## P0 写锁层（层 2 核心）

**state.py（因子侧）新增写锁原语**：`state_write_lock(state_root)` 上下文管理器——锁文件 `stateRoot/.write.lock`（新文件，不复用旧 `.lock`），Windows `msvcrt.locking` / POSIX `fcntl.flock`，独占、超时 10s、超时报可行动错误（「另一进程长时间持有 root 写锁，检查是否有僵死 bridge/CLI」）。规则：**锁内读-改-写，锁外只读**。

**改造点（全部改走锁内读改写）**：
- 因子侧：`append_json_entry`（state.py:84）、`_append_engine_trail_batch`（bridge.py:587）、registry 写（submit/update）、memory_pool `_save`/`offer_active`/falsified 入池、`mining_state.json` 归零写入
- 策略侧：`append_trail_entry`（state.py:61）、registry 写、test_lock 消费

**池去重防滞后**：offer_active 判重前锁内重读池文件（跨进程缓存最多滞后一次的已知限制消除在关键路径上）。

**test_lock 原子化（claim-then-compute）**：
- 因子侧：~~worker 不再写锁（evaluate.py:917-935 移除）。bridge 编排 claim~~ **实施勘误（P0 as-built）**：claim/finalize/release 做成 evaluate.py 的三个自包含原语（`claim_test_lock` / `finalize_test_lock` / `release_test_lock`），`evaluate_test` 内部组合——claim 在写锁内 check-and-set（consumed→拒；他进程 live claim→拒；stale claim 按 PID 存活回收），计算成功后锁内 finalize。自包含方案对直调 API 与 worker 路径统一生效，无双模式复杂度；bridge 预检只读展示 claim 态
- 策略侧：`consume_test_lock`（state.py:122）包进写锁即原子 ✓（按计划）

**run_dir 防碰撞（策略侧）**：`runs/<时间戳>-<pid>-<uuid4[:8]>`（__main__.py:103）。

**杀进程树补齐（策略侧）**：移植因子侧 `taskkill /T /F`（bridge.py:1279-1287）到 `_run_worker`（__main__.py:116）。

**bridge 启动锁退役**：`_acquire_state_lock`（bridge.py:4223-4280）从独占改为报告性实例登记（`.bridge-instances.json` 记 pid+启动时间，status 可见几个活跃实例）；stale 检测逻辑保留用于 claim 回收。

**并发测试（P0 验收）**：
- 4 进程 × 25 次 trail 追加同一 root → 100 条零丢失、JSON 全程有效
- 2 进程同时消费 test_lock → 恰一成功
- 2 进程并发 offer_active → 池文件有效且结果符合强换弱语义
- 写锁超时报错可行动

## P1 CPU 测量与二维超时

**新模块 `procinfo.py`**：`cpu_seconds(pid)`（Windows ctypes GetProcessTimes；POSIX 读 /proc/<pid>/stat）；`starvation_verdict(cpu_s, wall_s)` 返回 cpu_dense / starved / borderline（阈值 0.7）。

**worker 超时改造（两侧）**：父进程在墙钟到点时取子进程 CPU 秒 → 判决：cpu_dense → 现行四要素错误不变（归因成立）；starved → infra_failure 错误（带两数 + 重试指引 + 不计新账），入 loop.infra_failures；borderline → 按真慢处理附数据。

**CPU 测量（worker 侧）**：worker 用 `time.process_time()` 分段计量（factor 计算 / 评估各段），结果带 cpu_s——供 factor_perf、trail、判据使用。

**OMP 单线程**：两侧 worker spawn 环境注入 `OMP_NUM_THREADS=1`、`MKL_NUM_THREADS=1`、`OPENBLAS_NUM_THREADS=1`、`NUMEXPR_NUM_THREADS=1`（BLAS 自旋空烧 CPU 会把饿死伪装成真慢）。

**伸缩与旋钮**：`DSH_FACTOR_JOBS`（默认 cpu_count−1）、`DSH_FACTOR_TIMEOUT`（默认 300s）；`effective_wall = clamp(base × cpu_count/jobs, base, 4×base)` 仅因子 batch 侧。策略侧：`--timeout` 参数 + 默认 300s 不变。

**测试**：判决函数表驱动；worker 结果含 cpu_s；OMP env 注入；伸缩公式与 clamp；真实饿死路径（小超时 + sleep worker → starved verdict → infra_failure 文案）。

## P2 OS 级硬限额（Job Object / RLIMIT）

**procinfo.py 扩展**：`apply_limits(proc, cpu_s=None, mem_bytes=None)`——Windows：ctypes Job Object（`JOB_OBJECT_LIMIT_PROCESS_TIME | KILL_ON_JOB_CLOSE | PROCESS_MEMORY`，Assign 后父进程持句柄）；POSIX：preexec_fn 设 RLIMIT_CPU / RLIMIT_AS。

**语义**：CPU 限额 = 判据阈值的执法后盾（父进程饿死时仍确定性击杀）；内存限额防膨胀因子把整机拖进 swap 饿死其他会话——超内存是独立错误类（MemoryError 结构化上报），≠ 慢。KILL_ON_JOB_CLOSE 顺带兜底父崩溃孤儿。

**接入**：两侧 worker spawn 处统一走 apply_limits；限额值 = 该 worker 的 effective CPU 预算。

**测试**：烧 CPU 子进程小限额被杀且判 cpu_dense；超内存子进程报 MemoryError；孤儿兜底（杀父后子进程消亡）。POSIX 路径 skipif 非 POSIX。

## P3 层 1：evaluate_batch 进程池

**编排**：bridge 收到 batch → env 序列化 **一次**（共享 npz，多 worker 并发只读安全）→ `ThreadPoolExecutor(max_workers=jobs)` 并发跑现有单因子 worker 路径（每因子独立 mkdtemp scratch + request + result；线程只等 subprocess，GIL 无碍）→ 全部收齐后父进程**串行**做账（engine trail 一次批量追加、池 offer 锁内逐个）。零写入纪律不变：子进程只算，父进程只写。

**部分失败**：单因子异常/超时只标记该因子（保留现有错误分类与四要素/infra_failure 文案），不影响同批其他因子。

**jobs 语义**：max_workers = DSH_FACTOR_JOBS；与 P1 伸缩公式共用同一声明。

**测试**：6 个慢因子（各 ~1.5s CPU）jobs=4 vs 顺序 → 墙钟 < 顺序 70%；并行结果与顺序逐一相等（确定性）；掺一个抛错因子其余完好；jobs=1 退化为现行为。

## P4 效率约束四层

**Tier 1（AST 门禁）**：`scan_inefficiency`（discipline.py:143）内部改 AST 分析（外部签名与返回结构兼容扩展）：A 类五项（D3a）→ `factor_check_causality` 前置硬拒，结构化错误含行号、模式、**可直接抄的替换模板**（如 `df.groupby("symbol").shift(...)` / ops 对应算子），不计 trial（与前视拒绝同通道）；B 类保留警告。`list.append` 循环收集是正确写法，不误伤。

**Tier 2（校准与 CPU 预算）**：`machine-calib.json`（引擎版本、cpu_count、ref_cpu_s=合成面板过固定向量化管线的中位 CPU 秒，版本升级或缺失时自动重测）；`factor_perf`（noise.py:47）外推从墙钟改 CPU 秒；worker evaluate 报告带各段 cpu_s。

**Tier 3（入账）**：engine trail 条目加 `cpu_s`；registry submit 记 `cpu_rel = cpu_s / ref_cpu_s`（无校准时为 null 附注）；trail_summary 暴露族均 cpu_s（仅展示，D3c）。

**Tier 4（引导）**：FACTOR_GUIDE.md 补带实测数字的反模式目录（本机参考面板实测：iterrows X 秒 vs 向量化 Y 秒）；SKILL.md 更新 causality 段（A 类硬拒）与 jobs/并行指引。

**测试**：五项 A 类样例源码逐一被拒且不计 trial；合法小循环因子放行；嵌套 for 仍只警告；校准生成/缓存/版本失效重测；factor_perf 用 CPU 口径；trail/registry 字段在位。

## P5 层 3 纪律成文 + 全量回归

**纪律三条（写入 SKILL.md 与本文档姊妹篇）**：
1. 研究家族 = env identity（数据文件内容哈希+口径哈希）；同 identity 拆多 root 稀释 N_eff = 假门，等同 state.reset 洗 trail
2. 共享账本诚实记账：并行试得越多 deflation 门槛自动升高——这是设计内行为，不是惩罚
3. 并行会话分核：各会话设 DSH_FACTOR_JOBS，加总 ≤ 核数；反复 infra_failure 即超订信号

**并发压力测试**：3 进程对同一 root 混合操作（trail 追加 + 池 offer + registry 追加）→ 账本条数=尝试数、全部 JSON 有效、无死锁（限时）。
**压测实战发现（P5）并修复**：registry 的「read → append → write」读在锁外有丢更新窗口（3×8 压测丢一半条目）——两侧新增锁内读-追加-写助手 `append_registry_entry`，submit 治愈写入改锁内重读过滤。教训入规约：**追加语义一律走锁内助手，禁止手工 read-modify-write**。

**全量回归**：因子 268 + 策略 47 + 新增并发/效率测试全绿；既有行为零回归（单会话语义不变）。

## 风险与已知限制

- 建议锁（flock/msvcrt）只约束走本代码的进程；外部进程直改状态文件不受保护
- 池去重的进程内指纹缓存跨进程可能滞后：offer 关键路径已锁内重读，非关键路径接受至多滞后一次
- Job Object 嵌套分配需 Win8+；POSIX RLIMIT_AS 对 numpy 大页分配偶有误伤，内存限额初值保守（可配）
- run_dir 改名后旧 runs 目录仍兼容读取（只加后缀不改结构）

## 交付节奏

PLAN 落盘 → P0 → P1 → P2 → P3 → P4 → P5，逐阶段提交 + 阶段测试；P5 做全量回归。完成后不自动并入 main（等用户指令），不推送 GitHub。
