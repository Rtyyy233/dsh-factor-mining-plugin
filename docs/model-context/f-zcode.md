# 模型可见上下文全量盘点 · 因子挖掘 · zcode 侧（F-ZC-01 至 F-ZC-06）

日期：2026-09-30。性质：实物取证（每张卡附取证来源），不是回忆录。

## 本文件范围与读法

本文件盘点"因子挖掘系统在 ZCode 宿主（ZCode CLI，终端形态的编码代理，会话与数据存放在用户目录的 .zcode 下）一侧，一般生产实例中模型实际看到的全部上下文"。"zcode 直驱"指：ZCode 会话不使用 DSH 宿主的 factor_* 工具，而是直接在命令行里启动 Python 桥进程（引擎的命令行入口），向它发送 JSON-RPC 请求来驱动因子评估、入册、日志等全部引擎功能。这条挖掘线自 2026-09-04 起承担通宵无人值守挖掘，是 2026-09 的主力生产方式。

一张卡对应一类情境。每卡五个字段：编号与名称、触发时机、完整上下文（逐字全文）、溯源与设计功能（哪段代码或哪个配置产生这块文本、它在设计里解决什么问题）、条件与现状备注（版本开关、默认关、宿主差异、负情境）。"模型看到"指进入模型请求的内容：用户角色消息（含定时任务注入的提醒文本）、命令行工具的 stdout/stderr 文本、技能加载返回的纪律全文、工具清单。长静态文本（技能纪律全文、引擎指令文件全文、三份定时唤醒 prompt 全文、命令行输出样例集、四十件工具参数 schema 全文）放在文末附录 A 至 E，卡内引附录号。

与姊妹文件的关系：`docs/model-context/f-dsh.md` 盘点同一套引擎在 DSH 宿主侧的上下文（系统提示、工具响应内嵌指令区、回合边界注入等）。两文件多次互相引用；同一份指令文本在两个宿主重复出现的地方，卡内注明"跨通道同源"并指出共同源头。

主要取证材料（下文简称）：

- 【运行库】= `C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\analysis\glm_mining\`。直驱现场的完整工作目录：引擎指令文件 ENGINE_ORDERS.md、cron/（定时生成的因子源码与批次产物）、queue/ 与 results/（待办与结果）、runner_*.log（执行器日志）、挖掘脚本（glm_rpc.py、runner.py、engine_orders.py、night_watcher.py、status_loop.py、batch_rangeamt.py、verify_zcode_direct.py 等）。注意：主树 `C:\Users\RTyyyy\Desktop\life-os\projects\dsh-factor-mining-plugin\analysis\glm_mining\` 只含其中已提交 git 的脚本子集；带历史沉淀的完整现场（含 ENGINE_ORDERS.md、cron/、queue/、results/、全部 log）在 worktree 工作目录内（未提交文件），本盘点以 worktree 现场为准。
- 【zcode 会话库】= `C:\Users\RTyyyy\.zcode\cli\db\db.sqlite`（session/message/part 三表，存 ZCode 会话的消息与工具调用记录）。本盘点用 SQL 直接取出历史会话的第一条用户消息（开场白）与定时唤醒消息全文。
- 【zcode 执行日志】= `C:\Users\RTyyyy\.zcode\cli\exec\<会话id>\<调用id>-stdout.log`，ZCode 会话里每条后台命令的 stdout 落盘文件——直驱会话里模型跑挖掘脚本看到的原始输出就在这里。
- 【zcode 宿主日志】= `C:\Users\RTyyyy\.zcode\v2\logs\2026-09-17.log` 等，记录定时任务的投递事件。
- 【zcode 记忆】= `C:\Users\RTyyyy\.zcode\cli\memories\projects\dsh-factor-mining-plugin-83429617c3dd4e0a\memory\`，含通宵操作法（overnight-mining-runbook.md）、临时配置纪律（temp-needs-no-persistent-config.md）、定时任务保险丝约束（cron-fuse-no-self-delete.md）。
- 【988c85da】= `C:\Users\RTyyyy\.dsh\sessions\--C-Users-RTyyyy-Desktop-life-os--\session-988c85da-98f7-4f96-9ee3-5246d5295521\session.v4.jsonl.zstd`（zstandard 压缩的会话转录；2026-09-30 00:18 起在 DSH 桌面宿主新建的会话，因桌面端当时没有 factor 工具而退化为命令行直驱挖矿，是自停边界负情境的实录）。
- 源码【ts】= 主树与 worktree 的 `ts\tool-factor-mining\src\`（index.ts 工具注册、drive.ts 自动推进器）。
- 源码【py】= `python\src\dsh_factor_mining\`（bridge.py 引擎门面与命令行入口）。
- 技能 = `C:\Users\RTyyyy\.agents\skills\factor-mining\SKILL.md`（全文见附录 A）。
- 账本 = `C:\Users\RTyyyy\.factor-mining`（ETF 线）与 `C:\Users\RTyyyy\.factor-mining-stock-h10c30`（个股线）；历史上还存在过 `C:\Users\RTyyyy\.factor-mining-stock`（个股 H20 口径旧根，已退役）。
- 补丁层 = `C:\Users\RTyyyy\.dsh\overnight-drive-patch.yml` 与 `C:\Users\RTyyyy\.dsh\overnight-deadline-patch.yml`（DSH 宿主的启动外挂配置，见 F-ZC-01）。

---

## F-ZC-01 开场白两步法（通宵直驱会话的启动上下文）

**① 编号与名称**：F-ZC-01，通宵直驱会话的开场上下文——宿主补丁启动一步加开场白声明一步，两步缺一不可。

**② 触发时机**：每次布置通宵（或跨小时）无人值守挖掘时一次性发生。第一步发生在挖掘开始前的宿主启动动作里；第二步发生在挖掘会话的第一条用户消息（或定时任务的首轮注入文本）里。白天短程挖掘不触发本卡，直接从 F-ZC-02 或 F-ZC-04 起步。

**③ 完整上下文**：两步各自的模型可见文本。

第一步（宿主启动，模型看到的是宿主行为而非文本）：用外挂补丁文件启动 DSH 宿主。操作法定稿于【zcode 记忆】overnight-mining-runbook.md，逐字：

```text
**启动两步，缺一不可**：
1. `dsh --profile web --patch C:/Users/RTyyyy/.dsh/overnight-drive-patch.yml`（验证过的形态；`dsh web --patch ...` 会把选项混进 web 子命令参数，未验证，勿用）
2. 在挖掘会话里发一句"继续挖"（让会话调到 factor_* 工具即可，哪怕 factor_status）
```

两个外挂补丁文件本体（`C:\Users\RTyyyy\.dsh\overnight-drive-patch.yml` 与 `overnight-deadline-patch.yml`）全文见附录 D-8/D-9。前者把自动推进器的"连续简单注入不超过 5 次"上限解除（driveMaxConsecutiveSimple 置 100000），后者给一次通宵设置收工时刻（driveDeadline 与每 5 分钟兜底巡检）。两个文件头部都写明"临时外挂层、隔夜即弃、不动 profile 持久配置"——这就是外挂层即弃纪律，纪律的裁定记录（2026-09-01 用户纠正，逐字）：

```text
2026-09-01 用户纠正：通宵连跑挖掘是临时需求，我直接改了 `~/.dsh/profiles/web/cordis.patch.yml`（已还原）。临时需求一律走 `dsh --patch <file>` 外挂层——惰性文件、不进 profile、不传参不生效、隔夜即弃。
```

第二步（开场白声明 root 账本与任务）：挖掘会话收到的第一条指令性文本，必须说清挖哪条线（哪个账本根目录）和挖到什么时候。三个不同年代的实发开场白全文见附录 C-4（例行推进版，双线声明最完整）、附录 C-0 之前的会话首条消息、以及【988c85da】的开场三连。代表性的两例逐字：

例一（2026-09-04，直驱线第一条会话的首条用户消息，【zcode 会话库】sess_f1667c3c 序列 1）：

```text
我现在打算调用glm-5.3-flash，也就是让你来做因子挖掘，但是这无法通过DSH来完成，你在现在的环境中，能够通过Harness进行因子挖掘吗？
```

同会话序列 42（任务声明，含线的划分与死线）：

```text
我希望你并行做Ic线和tail线的探索，不要停，不要宣告穷尽，一直按因子挖掘的规则挖掘到明天早上八点，能做到吗？
```

例二（2026-09-30 00:56，【988c85da】行 104，账本声明出现在开场白里）：

```text
做ETF线的因子挖掘，检查一下是否能开始
```

之后 01:05 行 200 一句"开始挖掘"正式起跑。

**④ 溯源与设计功能**：

- 第一步的模块来源：DSH 宿主的启动参数 `--patch`（按 id 整行覆盖插件配置的外挂层，加载序在 profile 层之后）+ ts/index.ts 的 Config 注释（driveDeadline 条目注明"Configure per-run via a `--patch` overlay (ephemeral)"，即每次运行用外挂层配置、用完即弃）。设计功能：通宵挖掘需要宿主侧两个临时行为——注入上限解除（否则连续 5 次简单推进后引擎就停等真人）和收工时刻（到点注入一次收官指令后交还用户）。这两个行为只该活一晚，所以走外挂层而不动持久配置。现状（2026-10-03）：两件事都已退役/升级——连注上限删除（永续纪律）、死线改为会话级工具设置并落盘（factor-drive-state.json，重启不丢、过期自动取消）；本卡 ③ 描述的是取证基线 2026-09-30 的实发操作法，作历史留存。
- "两步缺一不可"的机制原因（runbook 原文）："注入器的武装（factorSessions）是按宿主运行期的内存态：重启后不认识任何会话，该会话不调一次 factor_* 工具，自动推进整夜不动。"即补丁只解决"能推多久"，开场白解决"会话进入推进器的视野"。模块来源：ts/drive.ts 的 factorSessions 集合（会话调用 factor_* 工具才登记，详见 F-ZC-05）。
- 第二步的模块来源：操作者手写（或定时任务模板生成，附录 C-4 的例行推进文本就是可复用的开场白模板）。设计功能：声明 root 账本（挖个股线还是 ETF 线、账本根目录在哪、允许与禁止动哪些根）和任务边界（挖到几点、禁止什么动作）。声明落进直驱脚本后变成代码事实：挖掘脚本把账本根写死在 Bridge 构造参数里（【运行库】runner.py 的 ROOTS 映射：ic 与 tail 线指向 `C:/Users/RTyyyy/.factor-mining-stock-h10c30`，etf 线指向 `C:/Users/RTyyyy/.factor-mining`；batch_rangeamt.py 里 `Bridge(r"C:/Users/RTyyyy/.factor-mining-stock", "zcode-batch-day", 2)`）。跨通道同源：DSH 侧等价物是 factor_root_use 工具的账本绑定（f-dsh.md 卡 F-DSH-08），直驱侧等价物是脚本参数里的 root 字符串，两者最终都指向同一批 state root 目录。

**⑤ 条件与现状备注**：

- 本卡的第一步作用于 DSH 宿主；zcode 直驱会话本身不在 DSH 宿主里，它的"宿主"就是 ZCode 终端会话，无需补丁。2026-09-15 起直驱线的通宵形态改为"守望器后台任务退出唤醒会话"（runbook 2026-09-15 补记，见 F-ZC-03），两步法在直驱线的执行形态演化为：启动 farm/runner 后台进程加挂守望器，开场白职责并入定时唤醒文本。两步法完整生效的形态保留在 DSH 宿主夜跑（2026-09-17 死线外挂即此形态）。
- `dsh web --patch ...` 的写法未验证可用（runbook 注明会把选项混进 web 子命令参数）。
- 外挂层条目按 id 整行覆盖 bundle 层配置，编写时须把其余配置现值一并带上（两个补丁文件注释原文均注明，如 enableArxivSearch 须随行）。

**取证来源**：【zcode 记忆】overnight-mining-runbook.md 与 temp-needs-no-persistent-config.md 全文；`C:\Users\RTyyyy\.dsh\overnight-drive-patch.yml`、`overnight-deadline-patch.yml` 文件本体；ts/index.ts Config 注释；【zcode 会话库】sess_f1667c3c（双线挖掘 Day2）序列 1 与 42；【988c85da】行 9/104/200；【运行库】runner.py ROOTS、batch_rangeamt.py。

---

## F-ZC-02 读单唤醒（引擎指令文件 ENGINE_ORDERS 全文）

**① 编号与名称**：F-ZC-02，被唤醒的直驱会话的第一动作——读引擎指令文件并照令执行。

**② 触发时机**：每次直驱会话被唤醒（定时任务注入、守望器后台任务完成通知、或操作者手动说话）后的第一个动作位。通宵期间每 30 至 45 分钟一次；白天手动会话按需。指令文件本体由 runner 侧在每次评估完成后整体重写，所以每次唤醒读到的都是最新一版。

**③ 完整上下文**：模型读到的文件全文。当前存档样张（【运行库】ENGINE_ORDERS.md，2026-09-17 16:14 版，深化战役激活态）全文见附录 B，共四段结构：

1. 首行 HTML 注释（写给模型的纪律声明，逐字）：`<!-- 本文件由 runner/引擎指令通道在每次评估后重写;zcode 直驱=引擎驱动:模型是执行器,唤醒后第一动作=执行下列指令,完成一条就在回执区记一行,不得自主改挖新构造绕过深化需求 -->`
2. 多行人类可读指令区：`[引擎指令]` 头行（来源文件、时间、状态、轮次）+ strategy 行与指令行 + 深化战役行（目标哈希、现价重算 p、已试/预算）+ 深化队列 top3（哈希、p、sr、horizon、已试变体数）+ `>>` 开头的战役点名行 + obligation 行。
3. 同一份数据的完整 JSON 代码块（strategy/deepen_queue/campaign/tail 等全部字段）。
4. `## 执行回执(时间 | 执行的指令 | 动作/入队名)` 回执区：执行器（模型）每完成一条指令追加一行；重写时保留最近 40 行。

条件块全部触发条件（engine_orders.py 的 format_orders 组装逻辑）：stop_reason 非空才出现 stop_reason 行；strategy.type 存在才出现 strategy 行与指令行；战役 state=active 出现"深化战役[active 进度]"行、state=complete 出现收官行；深化队列非空出现队列行与 `>>` 点名（战役激活时点名指向战役目标，未激活时指向第一颗已试变体不足 3 发的近失，队首老种子不计）；scheduler_picks 非空出现"调度器选点"段（2026-09-29 新增，影子期该键为空、该段不出现）；escalation 与 obligation 非空才出现各自行。

**④ 溯源与设计功能**：模块来源——【运行库】engine_orders.py（直驱版指令通道：latest_loop 取最新结果文件里的 evaluate.loop，digest 摘要成订单，format_orders 渲染多行版，write_standing 原子重写常驻文件）；写入时机在【运行库】runner.py 主循环（每轮评估落盘后调用 engine_orders.write_standing，同时在 runner 日志打 format_oneline 单行版）；night_watcher.py 的 emit_orders 在唤醒输出里同步打印同一份多行版并重写 standing。设计功能：直驱架构把模型移出了逐响应反馈环——引擎每轮 evaluate 响应里附带的 loop 指令（深化队列、方向指令、停走禁令）躺在结果文件里无人读，模型唤醒后自主行动会退化为写新构造烧随机探索。本通道把指令送到模型重新进场的那一点，落实"直驱也必须是引擎驱动、模型只做执行器"的原则（2026-09-15 用户拍板）。消费端三处同源：守望器唤醒行、runner 日志单行、常驻 ENGINE_ORDERS.md，三处消费同一份 digest。跨通道同源：DSH 侧的等价物是工具响应内嵌 loop 指令区（f-dsh.md 卡 F-DSH-03）与回合边界注入（F-DSH-04），三者出自同一个引擎策略组装（bridge.py 的 loop 组装），只是投放通道不同：DSH 推给工具响应和注入器，直驱写文件。调度器任务书（PLAN-scheduler-task-brief-2026-09-29.md §三）把这条链明文定为三通道之一："③ENGINE_ORDERS.md（每次评估后整体重写的持久账本+直驱唤醒读物）"。

**⑤ 条件与现状备注**：

- 文件只在直驱跑批期间存在与保鲜。战役收官后守望器不再部署，曾发生 standing 停更两天、读到的全是旧指令的缝（2026-09-17 修复：改由 runner 每轮评估后自持重写，重写保留回执区只裁到最近 40 行——engine_orders.py write_standing 注释在案）。当前两个生产账本根目录（`~/.factor-mining` 与 `~/.factor-mining-stock-h10c30`）下均无此文件（2026-09-30 查证），它只活在直驱现场【运行库】目录里。
- 文件格式随引擎指令通道四次演进（git 历史快照，代表性三笔）：初始版 64e75ec5（2026-09-15，digest 无战役段，demand 只盯第一颗变体不足的新鲜近失）；战役版 b3d385aa（2026-09-17，新增"深化战役[active]"行与"穷尽由引擎机械判定,模型无权宣布"的战役点名）；保鲜修复版 2598b113（2026-09-17，runner 每轮重写并保留回执区）；调度器版 63304cab（2026-09-29，新增"调度器选点"段与 scheduler_picks 透传）。附录 B 样张为战役版形态（调度器默认关，选点段不出现）。
- DSH 会话看不到此文件（f-dsh.md 卡 F-DSH-12 已核实：DSH 转录零命中）。

**取证来源**：【运行库】ENGINE_ORDERS.md 文件本体（全文入附录 B）；engine_orders.py、runner.py、night_watcher.py 源码；git log 64e75ec5/b3d385aa/2598b113/63304cab 四笔的 engine_orders.py 版本；PLAN-scheduler-task-brief-2026-09-29.md §三；【zcode 记忆】overnight-mining-runbook.md 2026-09-15 补记（"唤醒后第一动作=读引擎指令"）。

---

## F-ZC-03 定时唤醒 prompt（Cron 注入与守望器通知）

**① 编号与名称**：F-ZC-03，定时自动化唤醒 ZCode 会话的提示词，加后台任务完成通知（另一族唤醒）。

**② 触发时机**：两类。甲类：ZCode 平台的定时任务（automation）按周期向绑定会话注入提示词——通宵期间每 30 至 45 分钟一条（推进钟）、每 40 分钟一条（审阅钟）、或死线时刻一条一次性收官钟。乙类：会话自己启动的后台命令（守望器、长跑挖掘脚本）退出时，平台注入一条任务完成通知，把会话重新拉起。频次：通宵夜每会话数十条。

**③ 完整上下文**：三份实发定时唤醒 prompt 全文入附录 C（C-1 例行推进钟 1980 字符、C-2 审阅钟 1015 字符、C-3 收官钟 819 字符），任务完成通知封皮全文入附录 C-5。三份 prompt 的关键特征逐字摘录：

例行推进钟开头（附录 C-1 首两行）：

```text
【通宵双线因子挖掘·例行推进】自动化触发(非真人消息;真人消息永远优先)。你在 dsh-factor-mining-plugin 仓库 worktree 工作区,驱动脚本在 analysis/glm_mining/。
```

审阅钟开头（附录 C-2 首行）：

```text
【farm 审阅钟·zcode 直驱双线夜巡】你是因子挖掘的审阅执行者。按以下顺序工作（单进程纪律，不并行）：
```

收官钟开头（附录 C-3 首行）：

```text
通宵四线因子挖掘收官(死线 08:00 已到)。工作目录 C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\analysis\glm_mining。
```

任务完成通知（附录 C-5 全文，守望器退出时平台注入）：

```text
<task-notification>
<task-id>exec_2121c6c3-56d5-4641-b776-2b3d8a77f7a6</task-id>
<tool-use-id>call_6a7ab45fda894352afd3e8d3</tool-use-id>
<output-file>C:\Users\RTyyyy\.zcode\cli\exec\sess_5d6dc2fe-c261-4f79-bb3c-a59fead22ebc\call_6a7ab45fda894352afd3e8d3-stdout.log</output-file>
<status>completed</status>
<summary>Background command "Watch for new results/stale logs until 06:45 requeue moment" completed (exit code 0)</summary>
</task-notification>
```

模型读到通知后用读文件工具打开 output-file，看到守望器的唤醒输出（WAKE 行加引擎指令块，形态见 F-ZC-04 样例 D-6/D-7）。守望器的部署方式（会话内实发命令，【zcode 会话库】tool part 在案）：`python night_watcher.py "2026-09-13 06:45"`，以后台任务运行，启动回执即"Command running in background with ID: exec_2121c6c3。 You will be notified when it completes."

**④ 溯源与设计功能**：模块来源——甲类：ZCode 平台 automation（CronCreate/CronUpdate 创建；投递事件在【zcode 宿主日志】留痕，如 2026-09-17.log 的 `ZCode task facade sendPrompt 开始 {"textLength":1015}` 即审阅钟每 40 分钟一条的实录）。prompt 文本由操作者（或会话内模型受托）撰写，内容自包含：声明身份（自动化触发非真人）、工作目录、账本与线的划分、步骤清单（技能加载、时间闸、防重叠锁、逐线推进、故障处理）、硬禁清单。乙类：ZCode 平台的后台任务通知机制加【运行库】night_watcher.py（轮询 results 目录与时刻，满足条件即打印唤醒原因退出；2026-09-15 引擎驱动改造后唤醒输出追加引擎指令块并重写 standing 文件）。设计功能：ZCode 会话没有引擎注入器，定时唤醒就是直驱线的"推进器"——推进钟把通宵任务书压缩成每半小时重放一遍的执行令；审阅钟解决"farm 每轮 top 树的推理审阅无法委托给机械脚本"的问题（runner 只能机械取数，promote/variant/skip 判定必须模型在场，runbook 2026-09-17 补记在案）；收官钟保证死线时刻必有一轮收尾；守望器把"有新结果"这一事件转成会话唤醒。跨通道同源：审阅钟 prompt 步骤 1 指定的待办文件 FARM_REVIEW_PENDING.md 由 runner.py 的 review_pending_update 每轮追加（同一份数据的两个消费面）。

**⑤ 条件与现状备注**：

- 定时任务保险丝的历史约束（【zcode 记忆】cron-fuse-no-self-delete.md 在案）：2026-09-04 实测 ZCode 会话工具集只有 CronList/CronCreate 没有 CronDelete，收官后 recurring 保险丝无法自删，只能请用户在自动化面板手动禁用（残余触发每轮只做零动作确认）；同日补充实测：属于某定时任务的会话内 CronCreate 直接被平台拒绝（"Cannot create a scheduled task inside a session that already belongs to a scheduled task"），等效解法是 CronUpdate 现有 automation 扩容；2026-09-13 实测 maxRuns 跑满后任务并未硬停，彻底根除仍需用户手动移除（实例即审阅钟 automation-9fb55719-79f2-4ac3-bef3-808c256a40fc）；2026-09-13 补充普通会话工具集已有 CronDelete。例行推进钟 prompt（附录 C-1）的硬禁清单里"调用 CronCreate/CronDelete/CronUpdate"是 09-04 形态的纪律残留，09-05 之后 CronUpdate 扩容已是实测合法动作。
- 会话标题即定时任务的 prompt 首句（ZCode 用首条消息生成标题），标题清单可当唤醒 prompt 索引：`策略层探索循环每45分钟持续至明早8点`（09-01）、`双线挖掘Day2·每30分钟推进(至09-05 08:00收)`（09-04）、`farm 审阅钟每 40 分钟(双线夜巡至 10:00)`（09-08）、`明早08:00通宵挖掘收官报告(overnight_report_0913.md)`（09-12）。
- 守望器现行版本只保留两个唤醒条件（新结果文件出现、到点），早期版本的"日志停更 30 分钟判进程死"检查已删（会话内 Edit 记录注明理由：runner 日志是事件驱动的，空闲只轮询不写，staleness 不代表死亡）。

**取证来源**：【zcode 会话库】三份 prompt 实发全文（sess_f1667c3c 序列 84、sess_902a2d3c 序列 1507、sess_5d6dc2fe 序列 157）与 task-notification part（msg_mtyq2ihu）；【zcode 宿主日志】2026-09-17.log 三处 sendPrompt 记录；【zcode 记忆】cron-fuse-no-self-delete.md、overnight-mining-runbook.md 09-17 补记；【运行库】night_watcher.py 源码与 watcher_wake.txt；【zcode 执行日志】call_0c60dc44955b475195620359-stdout.log（WAKE 实发）。

---

## F-ZC-04 桥 CLI 输出（Python 桥命令行直接调用的输出形态）

**① 编号与名称**：F-ZC-04，直驱会话里命令行工具返回给模型的 stdout/stderr 文本。

**② 触发时机**：直驱会话的每个执行动作——会话用命令行工具（ZCode 的 Bash、DSH 的 pwsh）跑挖掘脚本或直接调桥命令行，stdout 逐行回流进模型上下文。通宵期间每分钟到每十几分钟一次（取决于评估时长）。

**③ 完整上下文**：输出样例全集入附录 D，九种形态：

- D-1 环境自检脚本输出（verify_zcode_direct.py 的 stdout：`[engine]`/`[ping]`/`[status]`/`[data.load]`/`[trail]`/`[registry]`/`[journal.read]`/`[journal.stats]` 各行，完整落盘 verify_zcode_direct_out.json 全文在附录）。
- D-2 批量评估脚本输出（batch_rangeamt_out.log 全文六行：load ok、逐因子 icir/p/v/red 行、saved 行）。
- D-3 会话内临时挖掘脚本的进度行格式（【988c85da】行 241 实发全文）。
- D-4 runner 执行器日志行（`[MM-DD HH:MM:SS]` 时间戳 + 评估名 + verdict/defl_p/red 计数 + GREEN→submit + accepted，runner_ic.log 实发样段）。
- D-5 引擎指令单行版（runner 日志内的 orders 行：`orders: state=running strategy=continue deepen=3 战役[36/36]:85bbaab7cbe6 k=1/200 >>引擎战役:对目标出结构型变体`）。
- D-6 守望器唤醒输出（`WAKE: new result(s): ['day13_rangeamt20_ic.json']`）。
- D-7 引擎指令通道命令行输出（`python engine_orders.py` 的多行指令加 `standing -> <文件路径>` 收尾行）。
- D-8/D-9 两个通宵补丁文件全文（F-ZC-01 引用）。
- 错误形态：桥进程的 JSON-RPC 错误以 `rpc_error` 对象回流，脚本原样打印（实发一例：`registry.submit RPC_ERROR: {"code": -32003, "message": "噪声硬门无法判定（有效世界不足 0/50，事务中止未落盘）——因子输出覆盖异常（全 NaN/形状错），先修因子再提交", "data": {}}`）；桥命令行自身的 `--probe` 探测输出为单行 JSON（`{"ok": true, "version": "0.1.0", "schemaVersion": 1}` 形态）。

桥命令行的调用面（模型与脚本共同依赖的入口知识）：桥可执行文件 `dsh-factor-mining-bridge`（pip 安装的入口，指向 python 包 dsh_factor_mining.bridge 的 main 函数）；命令行参数 `--state-root`（账本根目录）、`--lane`（并行线标识，进程级默认 lane）、`--data-config`（数据配置路径）、`--probe`（打印版本退出）；进程从 stdin 逐行读 JSON-RPC 请求、向 stdout 逐行写 JSON-RPC 响应（NDJSON 协议）。glm_rpc.py 的 Bridge 类把这套协议封装成 Python 调用（call 同步、call_poll 异步作业轮询），同时自身也可作命令行跑步骤文件（`python glm_rpc.py --root R --lane L --steps steps.json --out out.json`，逐步骤打印 `step <i> <method>: ok` 与收尾 `done ok -> <out>` 行）。

**④ 溯源与设计功能**：模块来源——py/bridge.py 的 main()（命令行入口、stdio 双向 UTF-8、读写分离双通道：status/paths.query/journal.read 等只读方法走快道并发，写方法与重计算走串行道保序）与各 RPC 方法（factor.evaluate、factor.evaluate_batch、registry.submit、journal.append、paths.append、data.load、state.trail_summary、standby.view 等）；【运行库】glm_rpc.py（NDJSON 客户端与步骤执行器）；runner.py（事件驱动逐 spec 执行器，say() 同时打屏与追加 runner_<line>.log）；night_watcher.py、status_loop.py、engine_orders.py（三类状态输出）；各挖掘脚本（batch_rangeamt.py、verify_zcode_direct.py、会话内临时脚本 tmp/etf_mining_round1.py 一族）。设计功能：直驱线没有工具响应通道，命令行 stdout 就是引擎对模型的全部"说话面"——进度行让模型知道作业还活着、verdict 行让模型拿到裁决、orders 行与 WAKE 行把引擎指令送达、rpc_error 把引擎的拒绝原因（含零写入承诺与修法）原样交给模型。stderr 形态备注：glm_rpc.py 起 bridge 时 stderr 接 DEVNULL，直驱现场留存的 bridge_stderr*.log 四个文件全部为 0 字节（2026-09-13 排查会话产物）——桥的 stderr 被有意丢弃，错误信息统一走 stdout 的 rpc_error 通道。跨通道同源：D-3 的 verdict/defl_p 数字与 DSH 工具 factor_evaluate 的返回是同一个 RPC 方法的两种渲染；D-5/D-7 与 DSH 侧 loop 指令区（f-dsh.md F-DSH-03）同源于 bridge.py 的 loop 组装。

**⑤ 条件与现状备注**：

- D-3 形态的进度行是会话内自写脚本（tmp/etf_mining_round*.py）的约定，非引擎固定输出；不同夜的脚本行格式略有差异（`[1/5]` 计数式、`steps written` 式、`B_DONE` 式均见过实发）。
- 异步作业必须轮询：evaluate 默认异步返回 job_id，不轮询只会看到 `verdict=None ic_ir=None defl_p=None red=0` 的占位行（D-3 实发首版正是如此，模型随后补轮询）。轮询语义在技能纪律与 glm_rpc.py call_poll（job_id 循环、连续 5 次错误放弃）双写在案。
- Windows 控制台编码：所有直驱脚本首行 `sys.stdout.reconfigure(encoding="utf-8")`，防止中文错误信息在 GBK 控制台炸编码（bridge.py main 同款处理，注释注明防乱码）。

**取证来源**：【运行库】batch_rangeamt_out.log、verify_zcode_direct_out.json、runner_ic.log、watcher_wake.txt、bridge_stderr*.log（0 字节实证）、engine_orders.py/runner.py/glm_rpc.py/night_watcher.py/status_loop.py 源码；【zcode 执行日志】sess_f1667c3c 的 call_f17a25b95f4e44cd9d441055（RPC_ERROR 实发）等 153 件；【988c85da】行 241（D-3 全文实发）；py/bridge.py main 与 pyproject.toml 的 `[project.scripts]` 入口声明。

---

## F-ZC-05 自停边界（负情境：命令行直驱会话对推进器不可见）

**① 编号与名称**：F-ZC-05，命令行直驱的挖掘会话对引擎自动推进器不可见——模型自停等口令后无人接管。

**② 触发时机**：当一个会话用命令行直驱方式挖矿（从不调用 factor_* 工具），且模型因某个自然停顿点（弧线收口、里程碑汇报、蒸馏完成）停止发起新动作时。此时引擎侧一切自动推进（continue/rotate 注入、死线收官）对该会话永久静默，直到操作者手动说话或外部定时任务恰好挂在该会话上。这不是偶发故障，是设计行为的边界：推进器的武装条件在结构上排除命令行直驱会话。

**③ 完整上下文**：模型看到的是"什么都没有"——没有注入、没有指令、没有催促。2026-09-30 凌晨的完整实录（【988c85da】，zstandard 解压 511 行逐行核对）：

时间线：
- 00:18:44 用户开场"检查一下因子挖掘的环境配置"（行 9）。
- 00:56:54 用户"做ETF线的因子挖掘，检查一下是否能开始"（行 104）。
- 01:05:05 用户"开始挖掘"（行 200）。
- 01:14:44 用户把审批策略改为 never、沙箱改为全开放（行 278/279，运行时快照原文在案）。
- 00:18 至 01:53 期间模型全程用命令行直驱：写临时驱动脚本（`C:\Users\RTyyyy\Desktop\life-os\tmp\etf_mining_round1.py` 一族，脚本内用 glm_rpc.Bridge 以旧会话线号 `session-d9e26716-f455-4563-9afe-9e7da7d38183` 起 ETF 账本桥）再执行；该会话全部 113 次工具调用清单：pwsh 49、read 32、write 15、grep 8、glob 6、skill 2、web_fetch 1——factor_* 工具调用 0 次（转录 tool/call 事件全量统计在案）。
- 01:53:49 模型完成 kingmix 四腿合成弧（四腿 IC_IR +0.5415，按纪律不提交、转入前向孵化队列 INC-20260930-015214——前向孵化指把因子冻结源码放进 120 个交易日新鲜数据窗口的观察队列），蒸馏推理日志后发出里程碑汇报，末句逐字：

```text
蒸馏九完成（78 次日志写入，9 次蒸馏，live 回到 3315/4000 字符）。说继续，即按活口①开深化变体弧。
```

- 01:53:51 回合结束（行 510 turn/end）。此后会话再无任何消息：引擎推进器没有来，因为它看不见这个会话。

**④ 溯源与设计功能**：模块来源——ts/drive.ts 的武装与门控（factorSessions 集合：会话事件流里出现 `tool/call` 且工具名以 `factor_` 开头才登记该会话，另有落盘绑定账本的会话 ID 批量入册；回合结束事件处理里 `if (!factorSessions.has(session.id)) return` 直接跳过未登记会话——不登记则永不注入）。设计功能：推进器以"会话用过 factor 工具"为 mining 会话的判别式，防止对无关会话误注入。代价是判别式的盲区：命令行直驱会话挖的是同一个引擎、同一个账本，但它的挖掘动作全部藏在 pwsh/Bash 的命令文本里，事件流里没有任何 factor_* 工具名，推进器结构上看不见它。zcode 直驱会话天然全程处于此盲区（ZCode 宿主不加载 DSH 插件，factor 工具根本不存在），其唤醒完全依赖外部定时任务与守望器（F-ZC-03）；DSH 会话在工具面缺失时退化为命令行直驱，同样落入盲区——【988c85da】正是后者（桌面端当时工具面未挂载，f-dsh.md 卡 F-DSH-13 在案）。此卡即任务书确认的设计缺口记录："纯 CLI 直驱的挖掘会话对注入器永远不可见，无 continue/rotate，模型自停后无人接管。"跨通道对照：同一夜里若该会话调过一次 factor_status，静默 60 秒后就会收到 continue 注入（f-dsh.md F-DSH-04）。

**⑤ 条件与现状备注**：

- 缓解路径在盘点日已列出三项待拍板（任务书 A6）：不改（直驱线历来由操作者手动推或定时钟推）；引导层面（开场白优先用工具面）；机制层面（给直驱配看门狗，监控账本落账节奏，停滞时经宿主唤醒对应会话）。推荐先做引导层面。
- 本负情境与 F-DSH-13（桌面工具面未挂载）互为因果链：工具面缺失把 DSH 会话逼进命令行直驱，命令行直驱又落在推进器盲区，两层叠加造成"真挖一小时后静默自停"。
- 会话内并非零引擎触达：模型自己 grep 文件时曾把 loop 文本带进工具结果（f-dsh.md F-DSH-13 注记），但那是模型主动翻文件所得，不是引擎投放。

**取证来源**：【988c85da】session.v4.jsonl.zstd 全转录（行 9/104/200/278/279/235/241/507/510，tool/call 全量计数 113 次含 factor_* 0 次）；ts/drive.ts factorSessions 武装与 turn/end 门控段；任务书 PLAN-desktop-mcp-fix-and-archdocs-2026-09-30.md §〇 已确证事实第 5 条与 A6；f-dsh.md 卡 F-DSH-13。

---

## F-ZC-06 工具面卡（factor 工具全套参数 schema 全文）

**① 编号与名称**：F-ZC-06，因子挖掘工具面四十件的参数 schema 全文。

**② 触发时机**：工具清单（工具名加描述加参数 schema）随 DSH 宿主的每个模型请求头重发（f-dsh.md 卡 F-DSH-01 第五块）；zcode 宿主不加载这套工具，ZCode 会话的请求头里没有它们。本卡收录 schema 全文作为两个宿主的共同契约基准：DSH 侧模型逐字看到它，zcode 直驱侧的脚本作者按它构造 JSON-RPC 参数（同一 RPC 面，技能纪律原文注明"双入口同一 RPC 面：DSH 工具层是 factor_journal_*；zcode/直驱模式（glm_rpc.py 起 bridge、`--lane ic|tail` 即日志线名）用 bridge.call("journal.append", {...}) 同语义调用"）。

**③ 完整上下文**：四十件工具的名称、描述索引、参数 schema 原文全表见附录 E（ts/tool-factor-mining/src/index.ts 每个 defineTool 的 parameters 字段逐字转录，共享常量 stage 单列）。工具名清单（四十件）：factor_drive_deadline、factor_status、factor_root_use、factor_data_probe、factor_config_write、factor_config_validate、factor_load_env、factor_check_causality、factor_evaluate、factor_evaluate_composite、factor_evaluate_batch、factor_walk_forward、factor_noise_test、factor_day_perm_test、factor_audit、factor_random_generate、factor_operators、factor_null_landscape、factor_state_reset、factor_trail_summary、factor_export_report、factor_query_library、factor_query_paths、factor_record_trail、factor_record_explored、factor_record_search_path、factor_registry_get、factor_registry_submit、factor_registry_update、factor_standby_view、factor_standby_combine、factor_incubate_enqueue、factor_incubate_status、factor_incubate_judge、factor_journal_read、factor_journal_append、factor_journal_update、factor_journal_distill、factor_journal_stats、factor_arxiv_search。描述全文已由姊妹文件收录——见 f-dsh.md 附录 G（四十件名称加描述全文），本文件附录 E 只收参数 schema，两处出自同一源码注册段，跨文件同源。

**④ 溯源与设计功能**：模块来源——ts/tool-factor-mining/src/index.ts 的 apply()（四十个 defineTool 注册调用；其中 factor_arxiv_search 注册在 `if (config.enableArxivSearch)` 条件内；factor_record_trail/explored/search_path 三件由共享工厂函数 record() 注册、共用同一 schema；stage 常量被 factor_evaluate 等评估族引用）。设计功能：参数 schema 是工具协议的教学文本——每个参数的 description 写明取值语义、缺省值、何时省略（如 job_id 轮询时 source 应省略）、硬约束（如 flatness_params 的 value 必须以数值字面量出现在源码中）。DSH 模型按 schema 构造调用；直驱脚本作者按 schema 构造 RPC 参数（例如附录 D-2 的批量脚本按 factor_evaluate_batch 的 sources 语义传 JSON 对象）。工具描述把操作协议写成模型可读的说明（描述侧的塑形作用见 f-dsh.md 附录 G），schema 与描述共同构成工具面的全部模型可见文本。

**⑤ 条件与现状备注**：

- factor_arxiv_search 仅在插件配置 enableArxivSearch 为真时注册（桌面与 web profile 均已开）；其余三十九件无条件注册。
- 无参数工具五件（factor_status、factor_null_landscape、factor_trail_summary、factor_incubate_status、factor_journal_stats），schema 为空对象。
- schema 中 type 标 json 的参数（config、sources、ingredients、diagnosis、flatness_params、hashes、entry、override）接受 JSON 对象或 JSON 字符串两种形态（index.ts jsonRecord 边界归一，注释注明防字符串形态崩进 Python 桥）；历史上模型曾因字符串形态传数组触发 -32602 参数错误（f-dsh.md 卡 F-DSH-09 实发在案）。
- zcode 宿主全程无此工具面（F-ZC-05 盲区的成因之一）；桌面宿主 2026-09-30 修复后新会话可见全部四十件（f-dsh.md 卡 F-DSH-13 修复后实录）。

**取证来源**：ts/tool-factor-mining/src/index.ts 注册段（四十件 parameters 逐字转录入附录 E）；f-dsh.md 附录 G（描述全文，跨文件同源对照）；【运行库】直驱脚本对同一 RPC 面的实调用（batch_rangeamt.py、tmp/etf_mining_round1.py）。

---

# 附录

## 附录 A zcode 侧因子挖掘纪律全文（factor-mining 技能 SKILL.md 原文）

来源：`C:\Users\RTyyyy\.agents\skills\factor-mining\SKILL.md` 文件本体逐字（frontmatter 加正文）。本技能在 ZCode 会话里经技能工具加载后全文进入上下文（ZCode 与 DSH 共用同一用户级技能目录，DSH 侧加载实录见 f-dsh.md 附录 C，两处同源）。原文第 34 行"regime 敏感性"一词后带一个单字省略号字符，系原文件自带写法，非本盘点删节。

```text
---
name: factor-mining
description: 因子挖掘方法纪律。当用户要求挖因子、验证因子、评估因子 IC/纯多头时使用。
disable-model-invocation: false
user-invocable: true
---

# Factor Mining Skill

你是因子挖掘 Agent。目标：产出有真实截面排序能力（IC_IR 优秀）的因子。
纯多头能落袋是加分项，不是门槛——IC_IR 优秀本身就是有价值的产出。

## 责任切分

- 你负责：提出假设、写 `factor(env)` Python 源码、失败归因、决定下一个假设。
- 引擎负责：`factor_check_causality`（未来函数）、`factor_evaluate`（IC/IC_IR/column-perm/beta/衰减/top-N）、`factor_audit`（独立审计）、路径查重。
- 你的自由在“挖什么、怎么表达、怎么归因”，不在“怎么验证正确性”。

## 内循环

1. 提新假设前先 `factor_query_paths` 和 `factor_query_library` 查重；**恢复会话/决定下一个方向前先 `factor_trail_summary`**（引擎层 trail 自动记录了每一次评估——包括失败的，瞒不了）。
2. 写 `factor(env)` 源码；只用过去数据（`shift(正数)`、`rolling`、`expanding`、`cumsum`），输出 `(T,N)` 对齐数组。写法契约与效率规范见 docs/FACTOR_GUIDE.md。**实现性能是硬约束（2026-09-01 起引擎烟测门）**：全面板约数百万至上千万格，任何逐 cell 的 Python 循环单遍即分钟级，而 `factor_check_causality` 要跑因子 8 遍、audit 约 10 遍——**第一版就必须向量化**（`rolling`/`ewm`/`shift`/`cumsum`/矩阵广播，pandas/numpy 原生算子）。引擎对每个新源码先在小面板实测外推：预计超预算直接零成本拒绝（零 trial 不烧名，拒绝消息含实测数字与检测到的循环行号）。**引擎自动加速（2026-09-01 起）**：可 numba 编译的循环实现会被自动编译加速（严格等价校验后透明换用，perf.engine=="njit"）；被拒的慢实现会自动排引擎侧重写作业（强模型重写+严格等价验证）——收到烟测门拒绝后**等 1-3 分钟原样重交同一源码**即可，不要反复重试也不要换假设。**函数名必须是 `factor`**——`factor_random_generate` 返回的 source 已符合契约，**原样使用不要改名**；手写因子也命名为 factor。批量评估传 `factor_evaluate_batch` 的 `sources`（JSON 对象 `{"名字": "def factor(env): ..."}`，value 是完整源码）。
3. `factor_check_causality` 是**引擎强制的**：前视因子直接被拒绝评估（无需自觉）。**A 类确定性反模式同样硬拒**（2026-08-27 起）：iterrows/itertuples/applymap/apply(lambda)/循环内 np|pd 前缀 concat-concatenate-append——拒绝发生在评估之前（零计算、不计 trial），错误内嵌可直接抄的替换模板；嵌套 for 等仍只是警告（但实测超预算会被烟测门拦下，见第 2 条）。返回附 CPU 计时（慢=疑似非向量化，CPU 口径不受并行会话挤占影响）与 `inefficiency_warning`。
4. `factor_evaluate(stage: development)`——**默认异步（2026-08-31 起）**：返回 `{job_id, status}` 时传同一 `job_id` 给 `factor_evaluate` 轮询（running/queued=未完，出完整 diagnosis=完成）；**响应直接带 verdict = eval_cache 命中，就是最终结果，无需轮询**。同参数重复提交会自动并入在途作业（dedup: true），不会重复计算。结果顶层有 **verdict**（pass/fail/needs_review）与 **red_flags**（|IC_IR|>5、净年化>200%、test 显著优于 train 等自动标红）：**有 red_flags 必须先解释再下任何结论**；`deflated_train` 是按已试假设数折减后的 p（试得越多门槛越高）——**p>0.05 是入册硬门**（进 red_flags，选择运气不可排除，不显著不入册；N>1 而引擎报「缺池分布基线」时先跑 null-calibration）；`train_sensitivity` 是分界敏感性（只扰动 dev_end，test 绝不扰动）；`duplicate_suspect`/`method_suspect` 是引擎对记忆池的自动对表（数值重复/方法重复）——命中必须先查 factor_query_paths 或论证本质差异。
5. 写失败归因 + 下一步假设，并 `factor_record_trail`（**schema 强制**：round/signal/attribution/next_hypothesis/new_information 五要素——new_information 是第一优先级纪律：这一轮引入了什么新信息源）；证伪具体探索用 `factor_record_explored`（**证伪三条件强制**：exploration 精确定义/evidence 引用具体数字/root_cause 根因；带 source 的证伪自动入证伪记忆池防挖坟），试过的变体用 `factor_record_search_path`。
6. 入册 `factor_registry_submit` 时**带上完整 evaluate 诊断**（引擎校验 receipt：编造数字会被标 verified=false；同一因子换名重复登记、**同一名字重复提交**都被铁律拒绝；red_flags 未清自动拒绝）。描述写错了用 `factor_registry_update` 修正（只许改 signal/note——**不要换名重登、也不要重复 submit，两者都会被拒**）。分享结果用 `factor_export_report`（markdown 结果卡片，含指纹/verdict/null 地形对比）。

## 推理日志（2026-09-12 起，构造面 v7：双层记忆的解释层）

trail 是引擎的事实账本（不可变）；journal 是你的推理轨迹（可变，每 lane 一本）。引擎只当机械仓库管理员（首行约定校验/快照/索引），不读懂你写什么。**数字只引用 trail 事实（source_hash/IC_IR），不发明；ID 终身唯一，死假设不复活（新证据开新条目引旧 ID）。**

- **会话开场**：`factor_journal_read level=1` 装载 live 区。接手别人的日志先做信念审计：每条信念自带校准记录，低分校别的 distrust。
- **每轮写（推理纪律，非模板）**：动笔前先写下“不假思索的第一个念头”——那是分布引力的显影，是本次推理要**离开**的起点，不是结论；再从市场结构出发自由推理（谁在持续做可预测的事/为何没人磨平/留下什么可观测痕迹），每步自问“凭什么”。多候选走**互相独立的推理链**，只差参数的丢一个。
- **评估前注册（核心节拍）**：写好因子源码、**第一次 `factor_evaluate` 之前**，`factor_journal_append` 开 `## <id> registered <标题>` 条目：content 写预期（自由文本：方向/半衰期/换手/regime 敏感性…），**`source` 参数传因子源码原文**——引擎按它计算 ref（你评估前拿不到 hash）。评估响应自动带 `journal_mirror`（预期 vs 实测的机械并置），你用 `factor_journal_update` 结算（resolved/stale + 一句学到了什么）。**没有注册预期的评估是白测**——镜像没东西可照。
- **信念条目**：`## B-n active <标题>` 出生，正文必带置信+证伪条件+trial 引用；校准变化用同状态 update 补 note。
- **弧末蒸馏**：换向前（must_rotate 的 obligation 会提醒）`factor_journal_distill` 提交蒸馏稿：保留信念/开放假设/活教训，丢叙事与已结条目（引擎自动归档+墓志，旧 live 自动快照防洗账）。live 超 ~4000 字符告警——蒸馏是纪律，蒸馏本身是最强的推理练习（“这弧之后什么教训还活着”）。
- 日志可整本重置（记录原因即可）——trail 不灭，解释层可弃。遥测（登记率/校准）在 `factor_journal_stats`。
- **双入口同一 RPC 面**：DSH 工具层是 `factor_journal_*`；zcode/直驱模式（glm_rpc.py 起 bridge、`--lane ic|tail` 即日志线名）用 `bridge.call("journal.append", {...})` 同语义调用——镜像/蒸馏/遥测完全同轨。DSH 会话 lane=会话 id，但日志线名独立解析：宿主配置 `journalLane`（TS 层盖入，基础设施事实）> env `DSH_FACTOR_JOURNAL_LANE` > 会话 lane——配置后 DSH 各会话共享同一条日志线，跨会话信念继承成立；意图层（pending/族链/弧预算）不受影响。

## 假阴性补救体系（2026-09-13 起，批次 0/1 已落地）

- **分层计价（方案 B 影子模式）**：评估前注册（journal registered + source）的候选，evaluate 响应 `deflated_train` 附 `shadow_stratum`——机制层价（M 按 `registered_ledger` 台账注册试验计，α_layer=0.0253 Šidák）。**当前只报不进门**；影子期分布审查后拍板翻转。台账 `registered_ledger.jsonl` 不可 reset（时机证据+复杂度指纹在镜像时落盘）。经济逻辑：注册买窄 null——认真推理的假设不再按随机喷雾的尺子定价。
- **候补线**：`factor_standby_view`（DSH）/ `bridge.call("standby.view", {})`（直驱）——dev 优异未过线存量的派生只读视图（|sr|≥0.75、未入册、族去重、层标签）。复活通道（①重定价/③规则化组合/⑤前向孵化）由引擎机械触发，**agent 无申诉权**：错过就是错过，等条件变化被机器抬起来。
- **反同质化选种（P0a）**：`factor_random_generate` 的 top/top_tail 桶已按"rank + 0.3·(1−max|ρ|)"novelty 选种（对本批+近期已选；novelty_lambda=0 可关）——同族第二名让位给独立候选。选种历史 `explore_seeds.json` 随 mining scope 重置。

## 检查点（A 组核心验收，死约束）

- 三区时间隔离：development 自由看；selection 半消耗；test 一次性锁，只在最终验收使用。
- IC_IR 使用不重叠口径；column-perm |z| >= 3 才算截面结构真实。
- 报告 beta 暴露；|corr| 高要警惕 beta 伪装。
- 批次挖掘后用 `factor_evaluate_batch` 的 deflated_p（N_eff·Šidák 家族校正）；单因子 `factor_evaluate` 的 `deflated_train` 引擎已自动按 trail_engine 硬统计的已试假设数 + 池分布尺度校正（2026-08-18 起）——`deflated p` 不显著就是过了选择门槛也不许宣称显著。
- 多因子合成假设用 `factor_evaluate_composite` 做剥洋葱评估（合成 vs 各成分的增量贡献）——合成前先问：成分各自有真实排序力吗，还是只在合成里「看起来有效」。
- 时间序列只做时间有序分块（`factor_walk_forward`，引擎强制 t1=sel_end 限制在 selection 区——test 区不经 walk_forward 暴露，只走 finalize 的 evaluate_test 一次性消费），禁止对称 K-fold。
- 宣称有效前必须跑 `factor_audit`。

## 第一优先级：新信息源

合成已有因子只能重排已有信息，alpha 上限 = 原料信息量上限。每轮回答：
这一轮引入了什么 known library 里没有的新信息源？答不上来就是合成，不是新维度。
连续 3 轮合成后，下一轮必须挖新维度并引用 arxiv 论文（`factor_arxiv_search`）。

## 自主性纪律（引擎停走指令，2026-08-21 起）

**启动阶段（研究框架决策）由用户定**——三区划分、挖掘起点这类一次性、定错作废的全局决策，必须 `ask_user_question` 让用户拍板，不自行决定。

**挖掘阶段的停走由引擎 `loop` 指令决定，不由你决定**。关键工具响应（`factor_status` / `factor_evaluate` / `factor_evaluate_batch` / `registry.submit` / `factor_trail_summary`）都带 `loop` 对象：

- **`loop.state == "running"` → 必须继续内循环**。写完本轮归因/总结后立即开始下一轮假设——停下来等用户指示、写「本次探索结束」「等待用户决定」然后停笔，都是违规（2026-08-21 trail 实证：round 8/9 两次主动停笔，用户被迫手动推动）。
**历史（本卡取证基线 2026-09-30）：`loop.state == "may_stop"` → 才允许收尾汇报**，停点由机械判据触发（轮次上限/当前簇试验上限/IC_IR 滑窗收敛/finalize）。**现状（2026-10-03 永续化，全文以此为准）：may_stop 退役、值域钉死 {running, must_rotate}，上列停点与 finalize／连败停点全部删除——终态只有死线收官与用户指令两条**（见 mining-never-stops 分篇永续章）（2026-08-24 session.jsonl 实证：导入的 stateRoot 自带 8 入册因子，旧「池满即停」让 agent 33 个 turn 全停，中位 11 分钟一次，用户手动 push 31 次）。
- **穷尽宣告一律拒收**：「因子空间已穷尽 / 天花板 / 理论上限 / 建议换池子换频率」写进 `next_hypothesis` 会被引擎直接拒绝（工具报错）；同款文本即使已存在于旧 trail，回显时也会降级为 `pending_rejected` 并要求重写。实测 12/12 次穷尽宣告全假——每次宣告后同一池子都挖出了新因子族。「想法队列空了」≠「空间挖尽了」，后者只有试验计数和 IC_IR 收敛判据有资格宣判。
- **数据集/池/频率/数据源轮换是用户操作**，不是 agent 能执行的动作，也不是停笔理由——认为需要换数据时，随里程碑汇报**上报**（一句话即可），然后继续在当前数据上挖掘。
- **`[引擎推进]` 前缀的消息是引擎自动注入的下一步指令**（不是真人）：按其中的方向指令继续内循环即可（七类：继续/深挖补正/换方向/文献灵感/中阶合成/正交性审计）。真人消息永远优先；注入消息与 `loop.strategy` 同源，不需要再确认。

## 过拟合检验（2026-08-24 起全自动）

**噪声硬门（submit 自动执行，不可绕过）**：提交时引擎在 M 个随机噪声世界（逐资产匹配波动率的高斯随机游走 OHLCV，PIT 掩码/日历保留）上重跑你的因子。真 alpha 按构造不可预测噪声——**噪声上仍显著（|z|≥3）= 因子在拟合评价管道 artifact**（前视构造/重叠窗口/掩码泄漏），无条件拒收。被拒时先查因果构造；用 `factor_noise_test` 可在提交前自测（返回完整 IC_IR 分布不是二元判决）。

**date_shift 诊断（每次 evaluate 自动附带）**：因子时移 k∈{5,10,21} 后的 IC 应崩向 0；时移后仍显著 = 慢变量与市场 regime 的巧合，定性降级参考。

审计实证（28 次调用）：31% 返回是物理噪声（cross-section/momentum 等词与物理碰撞）、26% 论文级重复、查询全落在当前族词汇内（回音室）、论文→假设无溯源。引擎已机械修复，你只需遵守：

1. **类目默认**：不传 category 时引擎自动加 q-fin 类目过滤——物理噪声已被杀，不必再自行构造"避开物理词"的查询。
2. **论文台账**：引擎记录每篇返回过的论文。重复论文带 `seen_before` 标记并让位给未见论文（`fresh` 字段是本次新论文数）。**同一查询想看更深的论文用 `start` 偏移**，不要反复换措辞撞 top-N。
3. **`papers` 字段必填**：文献驱动的假设写 trail 时，`papers: ["2108.05721"]`（arxiv_id 列表）必填——不引用具体论文的「迁移自论文X」宣称不可审计，会被拒收。
4. **exhausted 论文禁引**：从某论文推导的思路被 `factor_record_explored` 证伪时（explored 条目也带 `papers` 字段），该论文标记 exhausted，后续检索直接排除——禁止换角度从同一篇论文再推导同一思路。
5. **种子查询**：literature 策略指令携带引擎轮转的正交种子（高阶矩/隔夜分解/长记忆/copula/熵/regime 切换/小波/极值/日历效应/网络中心性/最优传输/HAR/跳跃检测/期权隐含——与当前族词汇解耦）。**优先用种子查询**——用自己的词汇做相关性检索只会强化你已有的先验（回音室实测）。
- **`loop.obligation`**：上一轮你写的 `next_hypothesis` 还没消化——执行它，或用 `factor_record_explored` 明确证伪；静默放弃（写了不做的方向）是违规。
- **`loop.escalation`**：家族饱和分级警告，出现即本轮必须照做——
  - 连续 3 次同族试验：换构造思路，别再参数微调；
  - 连续 6 次：本族天花板已测得，必须换信息源维度（新数据列/新算子族）；
  - 连续 9 次：必须 `factor_arxiv_search` 引入文献级假设构造新因子族。
- **`registry.submit` 被拒时**：响应带 `next_moves`（合法出路：结构性新假设/正交组合构造/继续其他维度）与 `forbidden`（state.reset 洗 trail 重注册 = 假门；停下来问用户 = 违规；穷尽宣告 = 违规——数据轮换只能随里程碑汇报上报）。拒绝不是终点，是方向修正信号。
- 「你想怎么继续？」「A 还是 B？」这类挖掘中途问询被禁止。**决策要留痕不留问**：写进 trail 的 attribution/next_hypothesis。
- **test 消费是声明制**：finalize 消费 test 区前，在响应中明确声明「即将消费 test 区（一次性锁死），分界 X~Y」然后执行——test_lock 引擎护栏兜底，无需请示。
- 里程碑（候选入册/终止）简要汇报，不问。
- 可以问的例外：**数据路径**（无 config 且工作区找不到可信数据文件——猜数据 = 删库级风险）；以及用户主动问你在做什么时如实回答，但不借机反问决策。

## 并行与多会话纪律（2026-08-27 起，机制已落地；2026-08-31 意图层 lane 隔离）

多会话/多进程并行已受支持（stateRoot 写锁 + test 区原子消费 + CPU 归因超时），纪律四条：

1. **研究家族 = env identity**（数据文件内容哈希 + 口径哈希）：同一 identity 拆多个 stateRoot 稀释 N_eff = 假门，等同 `state.reset` 洗 trail。不同数据面板/不同口径才允许分开 root。
2. **共享账本诚实记账**：同 root 并行会话的试验全部进同一账本，deflation 门槛自动升高——这是设计内行为（多重检验记账本就是为大量试验设计的），不是惩罚，不要试图绕开。
3. **并行会话分核**：各会话设 `DSH_FACTOR_JOBS`（默认 cpu−1），多会话加总 ≤ 核数。worker 超时报「机器超订 / CPU 饿死」= infra_failure——直接重试同一因子（未计账），反复出现就降并行；报「烟测门·实测超预算」（2026-09-01 起）= 实现慢且引擎加速救不了——按拒绝消息向量化/重排算法后重交，或等自动重写完成后原样重交；报「CPU 密集 / 修实现不换假设」才是老式实现慢。`evaluate_batch` 已进程池化（jobs 并行、部分失败隔离：坏成员标 error 不烧整批）。
4. **意图层按 lane 隔离（dsh web 会话自动携带，无需配置）**：pending_hypothesis 义务、家族链 streak、方向段预算只统计本会话线；其它线的这些状态不会串进你的 obligation。计价与事实层仍全局——`loop.lane_note` 会注明「本线 N / 全局 M（其它线 K）」：bar 因外线试验升高是多重检验的正确行为，不是自己超时的调用完成了；外线的证伪记忆（explored/method_suspect）照常可查，撞车前先看。直连 CLI 并行进程用 `--lane` 或 `DSH_FACTOR_MINING_LANE` 区分。
5. **严禁杀任何 python/bridge 进程（铁律，2026-08-31 事故实证）**：bridge 单线程忙 ≠ 卡死——CPU 在涨就是活着。用 shell 杀 bridge 进程（`Stop-Process`/`taskkill`）= 所有会话的 factor_* 挂起到宿主重启，且在途请求零入账。正确动作永远是：等待 worker 墙钟上限自然超时（会报 infra_failure 且零写入）→ 原样重试同一因子。进程管理是用户的操作，不是你的。

## 冷启动（首次使用，按 status 自引导走）

**纪律（实测教训，违反会浪费整轮对话）**：
- **数据路径问用户仅限无 config 的冷启动**；已有 config 直接 `factor_load_env` 用。
- **环境 ID 用数据本名**（如 etf/stock_smallcap），不要改成 primary——工具的 primary 只是缺省 fallback，单环境时会自动解析。
- config 的正确结构：数据文件路径在 `source.path`（不是根级 path/file），列映射在 `mapping`（不是 symbol_col/date_col）。结构错误会被拒绝并附最小完整示例——照错误信息改，不要盲试多种格式。
- Windows 下跑临时 Python 脚本写成 .py 文件执行，不用 `python -c`（引号/中文会炸）。

1. 先 `factor_status`。看 `dataConfigured` 与 `nextStep` 字段——工具自己会告诉你下一步：
   - `nextStep: "factor_data_probe"` → 未配置。拿到用户给的数据路径后 `factor_data_probe(path)` 探测（parquet 返回全表统计：真实标的数/行数/日期范围）。
2. 列映射**自主定**：probe 的 `suggested_mapping` 直接采用；有歧义（`ambiguous`）时按数据语义自主判断并在响应中报告你的选择——不要问用户。仅当必需列缺失（`missing_required` 非空且无候选）时才向用户要数据说明。
3. 拿不准格式先 `factor_config_validate`（轻量校验+normalized 预览，不落盘），确认后 `factor_config_write(config)` 写入——**不需要传 path**，默认写约定路径 `stateRoot/data-config.json`，写入立即生效（config 传 JSON 对象或 JSON 字符串都可以）。**个股数据不用关数据校验**：停牌/退市/上市时间不齐都是常态，默认 report 模式放行并把缺口摘要放进 load 的 `dataQuality` 返回（上市前/有效期内/末有效日后三类分开计数）；只有怀疑数据文件损坏时才设 `requireFiniteOhlcv: true` 走严格阻断。
4. **市场口径**：config 里每个环境可加 `calibration` 段——`{"profile": "cn_etf_daily"|"cn_stock_daily"|"cn_etf_minute"|"cn_stock_minute", "horizon": 20, "cost_bps": 10, ...}`。个股预设自动启用涨跌停一字板 mask；分钟预设 t0 执行。不写 = 历史默认口径（H=20/10bps）。**换口径 = 换研究，报告时必须声明所用口径**。
5. **三区划分必须由用户确认**（train/selection/test 是研究诚信的分界线，启动阶段的主权决策，不能替用户决定）：拿到 probe 的日期范围后，用 `ask_user_question` 向用户展示「数据 X 到 Y，建议分界 dev_end=A / sel_end=B（各占约 60/20/20；分界宜选市场风格切换点而非机械比例）」，让用户确认或改。**确认后立即回写 config**：`factor_config_write` 把确认的分界写进 `calibration.dev_end/sel_end`（**必须成对**）——不落盘则 `regions_mode` 永远标「auto 未经确认」，会话重启确认状态就丢了。若用户明确说「自动/你定」，保留 auto 保底即可（此时 regions_mode 标 auto 是诚实记录）。test 区是一次性消耗（test_lock 锁死），分界定了就改不回去了。**分界确认后进入挖掘阶段即全自主，不再回来问**。
6. `factor_load_env` 校验并加载环境（返回实际生效的 calibration），然后进入下方"挖掘起点协议"。

**重置状态用 `factor_state_reset(scope)`**，永远不要用 shell 删 stateRoot 目录：scope=mining（轨迹+双池）/landscape（null地形+算子集）/registry（入册候选）/config（回冷启动）/all，每个文件删除前自动备份到 `stateRoot/backups/<时间戳>/`。test_lock 不在任何 scope（test 区纪律锁只能手动删）。

配置即文件：手动编辑 `stateRoot/data-config.json` 也合法，服务会在下次 status/load 时自动重读；`factor_status` 报 `configError` 时按提示修文件即可，无需重启。若报"旧格式 v0"，说明配置来自旧版本，需重新 probe + write。config 顶层可加 `"library": {"path": "known_factors.py"}` 挂载用户已知因子库。

## 挖掘起点协议（数据配置完成后，第一个假设之前）

**起点由用户选**（启动阶段主权决策，同三区划分）：

1. 先汇报（用 `factor_null_landscape` + `factor_operators` 查询）：null 地形是否已校准、生效算子集规模。
2. `ask_user_question` 让用户选：
   - **A. 从随机种子生成器开始**（推荐冷启动）：先 `factor_random_generate(mode='null-calibration', n=50)` 校准池子难度，再 `mode='explore'` 生成候选；**explore 不要传 seed**（引擎运行时自动派生并记录 seed_used——显式传与 null 校准相同的 seed 会被拒绝：整批树会重放 null 校准前缀、信息量为零，2026-08-18 实测踩过）。对 top 幸存者做**结构归因**（哪个算子组合过线），归因只准落在算子语义层面，禁止编造金融叙事
   - **B. 用户提供参考因子**：用户给一批因子（源码或公式），你逐个走标准管线建立基线
   - 用户自定义答案（如「依据我已有因子库做探索」）优先照办
3. 起点确认后进入挖掘阶段——**此后全程自主**，每轮归因把新因子 IC_IR 与 null 地形对比：未超 p95 的大概率是噪声。

## 诚实记录

- trail 记录完整轨迹；explored 只证伪“具体探索”，不证伪“方向”；search_paths 记录试过未结论的变体。
- 源码外挂库（2026-08-29 起）：每次评估/证伪的因子源码由引擎自动按 `sources/<hash前2位>/<hash>.py` 入库（trail 条目的 source_hash 即键）——查历史任何一次试验的完整源码按哈希取文件即可；`state.reset(mining)` 会连库一并备份清除。
- 所有产出写在用户 stateRoot，插件仓库不包含任何用户数据、因子库或挖掘结果。
```

## 附录 B 引擎指令文件 ENGINE_ORDERS.md 存档全文（2026-09-17 16:14 样张）

来源：【运行库】`analysis\glm_mining\ENGINE_ORDERS.md` 文件本体逐字（深化战役激活态；调度器选点段因调度器默认关而不出现；回执区此刻为空表头）。

```text
<!-- 本文件由 runner/引擎指令通道在每次评估后重写;zcode 直驱=引擎驱动:模型是执行器,唤醒后第一动作=执行下列指令,完成一条就在回执区记一行,不得自主改挖新构造绕过深化需求 -->
[引擎指令] source=deep_ic_271cf57e74.json @2026-09-17 16:14 state=running round=0/50
  strategy=continue: 无异常信号——惯性延续
    指令: 继续探索：执行上一轮声明的假设；完成后立即记录归因并构造下一轮假设，不要停下来等待指示。
  深化战役[active 36/36]:目标 85bbaab7cbe6 p_now=0.287 已试=1/200(一颗一颗推进,穷尽=引擎机械判定)
  深化队列(现价重算 p∈(0.05,0.30],按 p 升序 top3):
    1) e6abf9d4d33d p_now=0.0519 sr=0.959 h=10 已试变体=4
    2) afe6bdfdd501 p_now=0.0547 sr=0.983 h=10 已试变体=17
    3) 905388dbe5ee p_now=0.056 sr=0.957 h=10 已试变体=65
  >> 引擎战役(36/36):对目标 85bbaab7cbe6(p_now=0.287,已试 1/200)出结构型变体(结构轴: winsor/换腿/加腿/拆杠杆;新构造:变体配额 2:1;抖动轴失败不构成穷尽;穷尽由引擎机械判定,模型无权宣布)
  obligation: 继续内循环——禁止停下来等用户指示。停点仅由引擎机械判据触发（方向段轮次/当前簇试验上限、IC_IR 改善收敛、finalize），穷尽宣告一律拒收

```json
{
 "source": "deep_ic_271cf57e74.json",
 "ts": "2026-09-17 16:14",
 "state": "running",
 "round": "0/50",
 "stop_reason": null,
 "strategy": {
  "type": "continue",
  "why": "无异常信号——惯性延续",
  "key": "continue:0:3617",
  "directive": "继续探索：执行上一轮声明的假设；完成后立即记录归因并构造下一轮假设，不要停下来等待指示。"
 },
 "deepen_queue": [
  {
   "hash_prefix": "e6abf9d4d33d",
   "sr": 0.959,
   "p_now": 0.0519,
   "horizon": 10,
   "variants_tried": 4,
   "ts": "2026-09-16T20:07:26"
  },
  {
   "hash_prefix": "afe6bdfdd501",
   "sr": 0.983,
   "p_now": 0.0547,
   "horizon": 10,
   "variants_tried": 17,
   "ts": "2026-09-16T18:51:18"
  },
  {
   "hash_prefix": "905388dbe5ee",
   "sr": 0.957,
   "p_now": 0.056,
   "horizon": 10,
   "variants_tried": 65,
   "ts": "2026-09-14T21:50:26"
  }
 ],
 "campaign": {
  "state": "active",
  "target": {
   "hash_prefix": "85bbaab7cbe6",
   "p_now": 0.287,
   "variants": 1,
   "budget": 200
  },
  "progress": "36/36",
  "remaining": 1
 },
 "deepen_note": "深化战役 active:36/36/1 目标 85bbaab7cbe6 k=1/200——引擎逐颗推进,穷尽=authored 变体≥预算(机械判定)IC 线近失（现价 p∈(0.05,0.30]，未入册）——变体对墙几乎免费（相关试验≈同一张彩票）；每颗值得 2-3 发变体（结构轴优先:winsor/换腿/加腿/拆杠杆，抖动轴失败不构成穷尽）；队列非空时新构造:变体配额 2:1，已试变体由引擎计数（仅 authored/假设来源，farm 撞相关不计）",
 "demand": "引擎战役(36/36):对目标 85bbaab7cbe6(p_now=0.287,已试 1/200)出结构型变体(结构轴: winsor/换腿/加腿/拆杠杆;新构造:变体配额 2:1;抖动轴失败不构成穷尽;穷尽由引擎机械判定,模型无权宣布)",
 "obligation": "继续内循环——禁止停下来等用户指示。停点仅由引擎机械判据触发（方向段轮次/当前簇试验上限、IC_IR 改善收敛、finalize），穷尽宣告一律拒收",
 "pending_hypothesis": null,
 "escalation": null,
 "tail": {
  "n_trials_tail": 3606,
  "n_eff": 432,
  "bar": {
   "value": 0.3274,
   "s0_source": "empirical"
  },
  "near_misses": [
   {
    "hash_prefix": "bf0b43097a8d",
    "spread_ir": 0.2798,
    "gap": 0.0476
   },
   {
    "hash_prefix": "a3cbea494c81",
    "spread_ir": 0.2815,
    "gap": 0.0459
   },
   {
    "hash_prefix": "d69037c2c242",
    "spread_ir": 0.2446,
    "gap": 0.0828
   }
  ],
  "note": "尾部线接近门槛的方向（spread_ir 距 bar < 0.10）——若与当前族不同源，优先构造变体"
 }
}
```

## 执行回执(时间 | 执行的指令 | 动作/入队名)
```

## 附录 C 定时唤醒 prompt 全文三件与通知封皮

### C-1 例行推进钟（每 30 分钟，2026-09-04/05 双线夜，1980 字符）

来源：【zcode 会话库】sess_f1667c3c（双线挖掘 Day2）序列 84 实发全文逐字（序列 170 等后续轮次同文重放）。

```text
【通宵双线因子挖掘·例行推进】自动化触发(非真人消息;真人消息永远优先)。你在 dsh-factor-mining-plugin 仓库 worktree 工作区,驱动脚本在 analysis/glm_mining/。

任务:在两条全A个股日线上按 factor-mining 技能纪律并行挖掘,直到 2026-09-04 08:00。
- tail线: stateRoot=C:/Users/RTyyyy/.factor-mining-stock-h10c30(主 lane=glm-tail,farm lane=glm-farm-tail;口径 H10/30bps/前500,tail_net_basis,尾部门槛见 status)
- IC线: stateRoot=C:/Users/RTyyyy/.factor-mining-stock(主 lane=glm-ic,farm lane=glm-farm-ic;口径 H20/10bps,标准 IC 基准)
- 禁动 ETF root C:/Users/RTyyyy/.factor-mining。

步骤:
0) 先调用 factor-mining 技能,全程遵守(内循环、trail 五要素、red_flags 先解释、禁止穷尽宣告、禁止中途问询)。Windows 下临时脚本一律写 .py 文件执行。
1) 时间闸:若本地时间 ≥ 2026-09-04 08:00 → 读 analysis/glm_mining/farm/tail 与 farm/ic 的 farm_index.jsonl 及最新 cycle_*.json,写收工纪要到 analysis/glm_mining/overnight_report.md(各线:新增试验数、verdict 分布、最佳因子源码与表达式、未消化方向),然后结束,不新开任何评估作业。
2) 防重叠:若 analysis/glm_mining/locks/round.lock 存在且修改时间距今 < 60 分钟 → 本轮直接结束。否则 mkdir -p locks 并写入 round.lock;本轮结束时必须删除它(try/finally 保证)。
3) 每线各推一轮(先 tail 后 IC):
   a. farm 归因:读该线 farm/ 最新 cycle_*.json 的 batch_result,统计 verdict 分布与最佳成员;归因写入叙事 trail 的方法=glm_rpc.py CLI 发 {"method":"paths.append","params":{"layer":"trail","entry":{round/signal/attribution/next_hypothesis/new_information 五要素}}}——注意不是 state.mining.record(那只是轮次记账)。round=该线 trail.json 最后一条的 round+1;lane 由脚本 --lane 参数自动打标。
   b. 新假设:基于 a 的证据与本 lane pending_hypothesis,自拟 1-3 个新向量化因子(信息源优先正交,可借 farm 幸存源的算子结构;第一版必须向量化,函数名必须 factor),源码存 analysis/glm_mining/cron/src/,用 round1.py --jobs 2 提交并等完整 diagnosis(不许弃单:作业在途绝不提前关 bridge)。
   c. 入册纪律:verdict=pass 且无 red_flags 且 deflated p≤0.05 才允许 registry.submit(带完整诊断);selection/walk_forward 可用,但 stage=test 严禁(一次性锁),state.reset 全 scope 禁止。
   d. 硬禁:杀任何 python/bridge 进程;宣告穷尽/空间挖尽/停笔等用户;调用 CronCreate/CronDelete/CronUpdate。
4) 故障处理:bridge 调用连续 infra_failure(CPU 饿死/worker 超时)→ DSH_FACTOR_JOBS 降为 1 重试一次,仍败则记录后跳过该线,不要反复重试。
5) 全程 DSH_FACTOR_JOBS ≤ 2(round1.py 参数已控);farm 是长活后台任务,不要重启,只读产出。
参考:Round 1 已完成(日内反转×量,tail 0.238/IC 0.256,needs_review),trail 归因已入账,方向=量能条件化(量比>1.5 mask + 隔夜正交)。
```

### C-2 farm 审阅钟（每 40 分钟，2026-09-17 双线夜巡，1015 字符）

来源：【zcode 会话库】sess_902a2d3c（farm 审阅钟会话）序列 1507 实发全文逐字（02:55/03:35/04:15 等每 40 分钟同文重放，【zcode 宿主日志】2026-09-17.log 的 sendPrompt textLength=1015 与此互证）。

```text
【farm 审阅钟·zcode 直驱双线夜巡】你是因子挖掘的审阅执行者。按以下顺序工作（单进程纪律，不并行）：

1. 读 C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\analysis\glm_mining\FARM_REVIEW_PENDING.md 的表行（|ir|>=0.5 的 farm 树），对照 farm_review.jsonl 已审条目找出未审增量。
2. 对每棵未审树做推理审阅（协议：新颖结构先腿分解+机制假设；已知家族才用带距判 skip；尺度退化/饱和装饰/恒定因子=病理标注）：verdict=promote(新颖且带内→预注册+评估)/variant(带内近失→结构变体思路写明)/skip(理由)，逐条追加写进 farm_review.jsonl。
3. variant 树：用 runner.append_campaign_queue(root, hash, note) 回流对应 root 的 deepen_campaign.json（ic/tail 各自 root；hash 可从该 cycle 文件的 meta/factors 溯源或收割 results 里找）。
4. promote 树：单进程桥评估（glm_rpc.Bridge，root 按 line），dev pass 且 p<=0.05 走 load-first 提交；贴线近失出结构变体（量腿加权/窗微调先例）。
5. 检查两线健康：runner_ic.log/runner_etf.log 无 NameError/连续 error；进程死了不重启只记录上报。
6. 完成后在最终回复里给一行战报（审了几棵/promote/variant/skip/入册）。

若 FARM_REVIEW_PENDING.md 无未审增量且两线日志健康：回复"审阅钟：无增量，两线健康"即可，不做其他动作。若已是 09:50 之后：做收官——停掉 farm/runner 进程（先 powershell 查 PID 确认命令行含 farm.py/runner.py 再 taskkill //PID，绝不按名字批量杀），统计两线周期数/收割数/入册数写最终战报。
```

### C-3 收官钟（一次性，2026-09-13 08:00，819 字符）

来源：【zcode 会话库】sess_5d6dc2fe（收官报告钟会话）序列 157 实发全文逐字（死线时刻准点送达）。

```text
通宵四线因子挖掘收官(死线 08:00 已到)。工作目录 C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\analysis\glm_mining。

1. 确认进程状态:farm 四线应已于 06:50 自行退出,双 runner 应已于 07:50 退出(日志 runner_ic.log/runner_tail.log 末行应有 "runner exit (deadline)")。仍在跑的记录 PID 汇报即可,绝不杀进程。
2. 汇总 results/ic/ 与 results/tail/ 全部今晚结果(mtime ≥ 2026-09-12 23:45):逐个 verdict/ICIR/defl_p/red_flags 归因;数入册(runner 日志里 GREEN→submit accepted=True 的条目;或对 registry.get 的 entries 数 vs 基线 IC25/tail34)。
3. 写收官报告 overnight_report_0913.md(参考 overnight_report_0907.md 的结构):战果(入册数与关键数字)/构造谱系与归因/零入册时的诚实负结果(near-miss 清单与差距)/farm 四线解剖统计(批数/成员数/热树)/机制与坑的新发现/待用户拍板事项。中文,数字一律与结果文件对账。
4. 把收官摘要回写 construction_ledger.md(夜场终账段:光标终值/registry 终数/活口与死名单增量)。
5. 最后向用户汇报:一屏摘要——入册几只(名字+ICIR+p)、最强 near-miss、 farm 产出规模、异常事件(进程重启/烟测拒)、待拍板项。
```

### C-4 直驱线第一条会话的开场问答（2026-09-04，Day2 会话）

来源：【zcode 会话库】sess_f1667c3c 序列 1、12、25、42 逐字（F-ZC-01 例一展开；用户连续四条把环境、数据、任务边界说清）。

```text
序列 1  我现在打算调用glm-5.3-flash，也就是让你来做因子挖掘，但是这无法通过DSH来完成，你在现在的环境中，能够通过Harness进行因子挖掘吗？
序列 12 1.环境应该是全A个股日线，全A ETF不是目前因子挖掘的目标
序列 25 不着急，你先确定环境是OK的，而且是个股日线
序列 42 我希望你并行做Ic线和tail线的探索，不要停，不要宣告穷尽，一直按因子挖掘的规则挖掘到明天早上八点，能做到吗？
```

### C-5 后台任务完成通知封皮（守望器唤醒的载体）

来源：【zcode 会话库】sess_5d6dc2fe 的 part 表 msg_mtyq2ihu 逐字（守望器后台命令退出时平台注入的消息全文）。模型读到后打开 output-file 即见附录 D-6 形态的 WAKE 输出。

```text
<task-notification>
<task-id>exec_2121c6c3-56d5-4641-b776-2b3d8a77f7a6</task-id>
<tool-use-id>call_6a7ab45fda894352afd3e8d3</tool-use-id>
<output-file>C:\Users\RTyyyy\.zcode\cli\exec\sess_5d6dc2fe-c261-4f79-bb3c-a59fead22ebc\call_6a7ab45fda894352afd3e8d3-stdout.log</output-file>
<status>completed</status>
<summary>Background command "Watch for new results/stale logs until 06:45 requeue moment" completed (exit code 0)</summary>
</task-notification>
```

## 附录 D 桥 CLI 输出样例集

### D-1 环境自检（verify_zcode_direct.py，2026-09-17，stdout 行式 + 落盘 JSON 全文）

stdout 行式（该脚本实跑的 stdout 无独立落盘文件，以下各行按脚本 print 语句与逐字落盘 JSON 重构，数值全部取自下方 JSON；另有 [trail] 一行打印 trail_summary 的键名列表，键名未落盘、形态见脚本源码）：

```text
[engine] C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\python\src\dsh_factor_mining\__init__.py
[ping] {'pong': True, 'version': '0.1.0', 'schemaVersion': 1} (0.1s)
[status] focus={"dataConfigured": true, "nextStep": "factor_load_env", "version": "0.1.0", "configError": null}
[data.load] ok=True T=2574 N=5347 (8.6s)
[registry] n=25 keys=['accepted', 'by_track', 'count', 'entries', 'note']
[journal.read] {"keys": ["exists", "hint"]}
[journal.stats] {"exists": false, "hint": "本线日志未开立"}
done -> C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\analysis\glm_mining\verify_zcode_direct_out.json
```

落盘结果全文（verify_zcode_direct_out.json 逐字）：

```json
{
 "engine_file": "C:\\Users\\RTyyyy\\Desktop\\life-os\\.claude\\worktrees\\residual-holding-correlation\\projects\\dsh-factor-mining-plugin\\python\\src\\dsh_factor_mining\\__init__.py",
 "ping": {
  "pong": true,
  "version": "0.1.0",
  "schemaVersion": 1
 },
 "status_keys": [
  "bridgeModulePath",
  "configError",
  "dataConfigPath",
  "dataConfigured",
  "environments",
  "executionMode",
  "libraryConfigured",
  "libraryError",
  "loadedEnvironments",
  "loop",
  "nextStep",
  "pythonExecutable",
  "ready",
  "schemaVersion",
  "stateRoot",
  "version",
  "workerRuns"
 ],
 "status_focus": {
  "dataConfigured": true,
  "nextStep": "factor_load_env",
  "version": "0.1.0",
  "configError": null
 },
 "data_load": {
  "ok": true,
  "T": 2574,
  "N": 5347,
  "secs": 8.6,
  "calibration": {
   "frequency": "daily",
   "horizon": 20,
   "cost_bps": 10.0,
   "execution": "t1",
   "limit_up_down_mask": true,
   "dev_end": "2022-12-31",
   "sel_end": "2024-09-30",
   "regions_mode": "manual"
  }
 },
 "trail_summary_type": "dict",
 "trail_focus": {},
 "registry_keys": [
  "accepted",
  "by_track",
  "count",
  "entries",
  "note"
 ],
 "registry_n": 25,
 "journal_read": {
  "keys": [
   "exists",
   "hint"
  ]
 },
 "journal_stats": {
  "exists": false,
  "hint": "本线日志未开立"
 }
}
```

### D-2 批量评估（batch_rangeamt.py 的输出，batch_rangeamt_out.log 全文逐字）

```text
load ok: True
  day13_rangeamt_corr20_ic: icir=0.6386201362874272 p=0.9999861792728513 v=pass red=0
  day13_rangeamt_log20_ic: icir=0.8456430183448984 p=0.9952058247417291 v=pass red=0
  day13_rangeamt_wz20_ic: icir=0.8751642934213989 p=0.9902503216211428 v=pass red=0
  day13_rangeamt15_ic: icir=0.8555661288797105 p=0.9943097022844287 v=pass red=0
saved -> C:\Users\RTyyyy\Desktop\life-os\.claude\worktrees\residual-holding-correlation\projects\dsh-factor-mining-plugin\analysis\glm_mining\results\ic\day13_batch_rangeamt.json
```

### D-3 会话内临时挖掘脚本的进度行（【988c85da】行 241 工具结果全文逐字）

```text
[1/5] journal.read level=1 ...
[2/5] state.trail_summary ...
[3/5] data.load env=etf ...
      data.load ok=True
[4/5] journal.append registered ...
      register ok=True
[5/5] factor.evaluate (development) ...
      verdict=None ic_ir=None defl_p=None red=0
      loop.state=None round=None
DONE -> C:\Users\RTyyyy\Desktop\life-os\tmp\etf_round1_result.json
```

### D-4 runner 执行器日志（runner_ic.log 开头段逐字）

```text
[09-05 02:04:58] runner start line=ic root=C:/Users/RTyyyy/.factor-mining-stock
[09-05 02:05:09] data.load ok=True
[09-05 02:05:09] evaluate combo_winsor_x_vcorr10 ...
[09-05 02:10:04] combo_winsor_x_vcorr10: verdict=pass defl_p=0.009414800893255826 red=0
[09-05 02:10:04] combo_winsor_x_vcorr10 GREEN -> submit
[09-05 02:14:08] combo_winsor_x_vcorr10 submit accepted=True reject_kind=None
[09-05 02:14:08] evaluate combo_churn_x_vcorr10 ...
[09-05 02:19:18] combo_churn_x_vcorr10: verdict=pass defl_p=0.008675080883264439 red=0
[09-05 02:19:18] combo_churn_x_vcorr10 GREEN -> submit
[09-05 02:23:19] combo_churn_x_vcorr10 submit accepted=True reject_kind=None
[09-05 03:51:04] volmom_magnitude_120: verdict=needs_review defl_p=0.43169326457320906 red=1
[09-05 07:50:06] runner exit (deadline)
```

### D-5 引擎指令单行版（runner 日志内 orders 行，实发三例逐字）

```text
[09-16 03:12:58] orders: state=running strategy=rotate deepen=3[top 7b810b5dd5c3 p=0.0512 vars=4]
[09-17 04:08:58] orders: state=running strategy=continue deepen=3[top afe6bdfdd501 p=0.0527 vars=16]
[09-17 04:36:51] orders: state=running strategy=continue deepen=3 战役[36/36]:85bbaab7cbe6 k=1/200 >>引擎战役:对目标出结构型变体
```

### D-6 守望器唤醒输出（WAKE 行，watcher_wake.txt 与 0912 夜 exec 日志各一例逐字）

```text
WAKE: new result(s): ['day13_rangeamt20_ic.json']
```

```text
WAKE: new result(s): ['sb_e6573a698ec2_r2.json']
```

2026-09-15 引擎驱动改造后，WAKE 行之后同一输出内追加多行引擎指令块（engine_orders.format_orders 渲染，形态同附录 B 指令区）并重写 standing 文件；改造后的唤醒输出无留存 exec 日志（该形态三夜之后未再跑批），文本形态以 night_watcher.py 的 emit_orders 源码与 runbook 09-15 补记为证，标注推定。

### D-7 引擎指令通道命令行输出形态（engine_orders.py main，源码定位）

```text
[引擎指令] <多行指令区，同附录 B 第一、二段>
standing -> <ENGINE_ORDERS.md 绝对路径>
```

无可用结果文件时的输出（源码逐字）：`[引擎指令] 无可用结果文件(引擎未开跑或无 loop)——无指令`。

### D-8 通宵驱动外挂（C:\Users\RTyyyy\.dsh\overnight-drive-patch.yml 全文逐字）

```text
# 临时外挂层（2026-09-01 通宵挖掘用，隔夜即弃）：
# 本文件是惰性的——只有启动时显式传 --patch 才生效，不动 profile 持久配置。
# 用法（重启 dsh web 时）：
#   dsh --profile web --patch C:/Users/RTyyyy/.dsh/overnight-drive-patch.yml
# 作用：解除注入器「连续简单推进 ≤5 次」上限（drive.ts）。
# 注意：本条目按 id 整行覆盖 bundle 层的 tool-factor-mining 行，
# enableArxivSearch 须按 bundle 现值一并带上。
- id: tool-factor-mining
  config:
    enableArxivSearch: true
    driveMaxConsecutiveSimple: 100000
```

### D-9 通宵死线外挂（C:\Users\RTyyyy\.dsh\overnight-deadline-patch.yml 全文逐字）

```text
# 通宵挖掘死线外挂(方案 B,2026-09-17;隔夜即弃,不动持久配置)
# 用法(用前把 driveDeadline 改成真实死线时刻):
#   dsh --profile web --patch C:/Users/RTyyyy/.dsh/overnight-deadline-patch.yml
# 生效条件: dsh web 以本 patch 重启;需要插件 >=0.1.9(driveDeadline/driveWakeIntervalMs)
# 行为: 死线武装后连续简单注入上限自动让位;到点注入一次收官指令后自动推进解除;
#       每 5 分钟兜底巡检,停摆会话(无 turn/end 事件)也会被重新点火。
# 注意: 本条目按 id 整行覆盖 bundle 层的 tool-factor-mining 行,
# enableArxivSearch 须按 bundle 现值一并带上。
- id: tool-factor-mining
  name: '@deepseek-ai/dsh-tool-factor-mining'
  config:
    enableArxivSearch: true
    driveDeadline: "2026-09-18 08:00"
    driveWakeIntervalMs: 300000
```

### D-10 JSON-RPC 错误回流形态（实发两例逐字）

```text
submit steps written
registry.submit RPC_ERROR: {"code": -32003, "message": "噪声硬门无法判定（有效世界不足 0/50，事务中止未落盘）——因子输出覆盖异常（全 NaN/形状错），先修因子再提交", "data": {}}
```

（【zcode 执行日志】sess_f1667c3c 的 call_f17a25b95f4e44cd9d441055-stdout.log。）

```text
load ok: False
```

（runner_etf.log 首段：数据未加载时评估照跑、verdict 全 None——桥不因前置缺失而拒绝请求，靠输出字段暴露。）桥命令行探测输出（`dsh-factor-mining-bridge --probe`，源码定位）：`{"ok": true, "version": "0.1.0", "schemaVersion": 1}`。

## 附录 E factor 工具面四十件参数 schema 全文

来源：ts/tool-factor-mining/src/index.ts 各 defineTool 的 parameters 字段逐字转录（保持源码写法；`stage` 为评估族共享常量，单列于前）。描述全文见姊妹文件 f-dsh.md 附录 G，跨文件同源（同一注册段）。字段约定：`required: true` 标必填；未标 required 的参数均可省略；`type: 'json'` 的参数接受 JSON 对象或 JSON 字符串。

共享常量（factor_evaluate 等引用）：

```ts
const stage = {
  type: 'string' as const,
  description: 'development (free) | selection (semi-consumable) | test (one-shot lock).',
  enum: ['development', 'selection', 'test'] as const,
}
```

### factor_drive_deadline

```ts
parameters: {
  deadline: { type: 'string', description: '死线时刻，ISO 8601 本地时区（如 2026-09-18T08:00）。设置或替换当前死线。' },
  wakeIntervalMs: { type: 'number', description: '可选：兜底巡检间隔毫秒（停摆会话重新点火），缺省 300000（5 分钟）。' },
  clear: { type: 'boolean', description: 'true=清除死线，恢复常规自动推进（连续简单上限重新生效）。' },
},
```

### factor_status

```ts
parameters: {},
```

### factor_root_use

```ts
parameters: {
  root: { type: 'string', description: '目标账本键（见无参调用返回的 roots 列表，如 "etf"；"default"=默认个股账本）。' },
},
```

### factor_data_probe

```ts
parameters: {
  path: { type: 'string', required: true, description: 'Absolute path to the user data file.' },
  layout: { type: 'string', description: 'Optional layout hint: long, wide, per_symbol, multiindex.' },
  dateFormat: { type: 'string', description: 'Optional strftime date format hint.' },
},
```

### factor_config_write

```ts
parameters: {
  config: { type: 'json', required: true, description: 'Complete data configuration object (a JSON string is also accepted).' },
  path: { type: 'string', description: 'Optional config file path; defaults to the state-root convention data-config.json.' },
},
```

### factor_config_validate

```ts
parameters: {
  config: { type: 'json', required: true, description: 'Data configuration object to validate (a JSON string is also accepted).' },
},
```

### factor_load_env

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id from factor_config_write; defaults to primary.' },
},
```

### factor_check_causality

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', required: true, description: 'Python source defining def factor(env) -> np.ndarray.' },
},
```

### factor_evaluate

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', description: 'Python source defining def factor(env) -> np.ndarray. OMIT when polling a job (job_id only).' },
  stage,
  horizon: { type: 'string', description: 'Hypothesis horizon: an integer from the environment menu (e.g. "10"), or "scan" to profile every menu horizon (each an independent full diagnosis). Defaults to the main horizon. Changing horizon = a new bet; accounted per (source, horizon).' },
  job_id: { type: 'string', description: 'POLL: pass the job_id returned by a previous call to fetch its result (works for any async job: evaluate/composite/batch/audit).' },
  async: { type: 'boolean', description: 'Escape hatch: pass async:false to force a blocking synchronous evaluation (default is async-by-default with job_id polling).' },
},
```

### factor_evaluate_composite

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', description: 'Composite Python factor source. OMIT when polling a job.' },
  ingredients: { type: 'json', description: 'Map of ingredient name -> Python factor source. OMIT when polling a job.' },
  job_id: { type: 'string', description: 'POLL: fetch the result of a previously submitted async job.' },
  async: { type: 'boolean', description: 'Submit as a background job (returns job_id) instead of blocking synchronously.' },
},
```

### factor_evaluate_batch

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  sources: { type: 'json', description: 'Map of factor name -> Python source, each defining def factor(env). Example: {"f1": "def factor(env):\\n    import pandas as pd\\n    ..."}. OMIT when polling a job.' },
  horizon: { type: 'string', description: 'Declared horizon for the whole batch: an integer from the environment menu (scan is NOT supported for batch). Defaults to the main horizon. Changing horizon = a new bet; accounted per (source, horizon).' },
  job_id: { type: 'string', description: 'POLL: fetch the result of a previously submitted async job.' },
  async: { type: 'boolean', description: 'Submit as a background job (returns job_id) instead of blocking synchronously.' },
},
```

### factor_walk_forward

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', required: true, description: 'Python factor source.' },
  n_folds: { type: 'integer', description: 'Number of ordered blocks (default 5).' },
  t0_date: { type: 'string', description: 'Inclusive start date (default DEV_END).' },
  t1_date: { type: 'string', description: 'Exclusive end date (default end).' },
},
```

### factor_noise_test

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', required: true, description: 'Python factor source defining def factor(env).' },
  m: { type: 'integer', description: 'Number of noise worlds (default 100, cap 300).' },
  seed: { type: 'integer', description: 'Base seed; defaults to one derived from the environment fingerprint (reproducible).' },
},
```

### factor_day_perm_test

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', required: true, description: 'Python factor source defining def factor(env).' },
  m: { type: 'integer', description: 'Number of random pairings (default 200, cap 500).' },
  seed: { type: 'integer', description: 'Base seed; defaults to one derived from the environment fingerprint (reproducible).' },
},
```

### factor_audit

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', description: 'Python factor source. OMIT when polling a job.' },
  job_id: { type: 'string', description: 'POLL: fetch the result of a previously submitted async job.' },
  async: { type: 'boolean', description: 'Submit as a background job (returns job_id) instead of blocking synchronously.' },
},
```

### factor_random_generate

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  mode: { type: 'string', description: 'explore (default), null-calibration, or collision-card.', enum: ['explore', 'null-calibration', 'collision-card'] },
  n: { type: 'integer', description: 'Number of random factors to generate (default 50).' },
  seed: { type: 'integer', description: 'Reproducibility seed (default 42; collision-card: omit for runtime entropy — seed_used is returned and logged).' },
  top_k: { type: 'integer', description: 'explore only: top sources by |IC_IR| (default 5).' },
  k: { type: 'integer', description: 'collision-card only: concept count, 1-3 (default 2).' },
},
```

### factor_operators

```ts
parameters: {
  action: { type: 'string', description: 'get (default) or set.', enum: ['get', 'set'] },
  override: { type: 'json', description: 'set only: {disable?, windows?, delays?} override object.' },
},
```

### factor_null_landscape

```ts
parameters: {},
```

### factor_state_reset

```ts
parameters: {
  scope: { type: 'string', description: 'mining | landscape | registry | config | all (default mining).', enum: ['mining', 'landscape', 'registry', 'config', 'all'] },
},
```

### factor_trail_summary

```ts
parameters: {},
```

### factor_export_report

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', required: true, description: 'Python factor source (must match a prior factor_evaluate).' },
  name: { type: 'string', description: 'Factor name for the report title and filename.' },
},
```

### factor_query_library

```ts
parameters: {
  query: { type: 'string', required: true, description: 'Hypothesis or keyword text.' },
  top_k: { type: 'integer', description: 'Maximum hits (default 5).' },
},
```

### factor_query_paths

```ts
parameters: {
  layer: { type: 'string', required: true, enum: ['explored', 'search_paths', 'trail_similarity'], description: 'explored (falsified concrete explorations), search_paths (process-level variants), or trail_similarity (structural similarity pre-check).' },
  query: { type: 'string', required: true, description: 'Hypothesis or keyword text (unused by trail_similarity; pass a placeholder).' },
  source: { type: 'string', description: 'trail_similarity only: candidate factor source code.' },
  top_k: { type: 'integer', description: 'Maximum hits (default 5).' },
},
```

### factor_record_trail / factor_record_explored / factor_record_search_path（三件共用同一 schema，record() 工厂注册）

```ts
parameters: {
  entry: { type: 'json', required: true, description: 'One record object written to the user state directory.' },
},
```

### factor_registry_get

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id for contextual metadata.' },
},
```

### factor_registry_submit

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  source: { type: 'string', required: true, description: 'Python factor source.' },
  name: { type: 'string', description: 'Candidate name.' },
  signal: { type: 'string', description: 'Human-readable precise factor definition.' },
  diagnosis: { type: 'json', description: 'The complete factor_evaluate diagnosis object, passed through verbatim from the evaluate call you are registering. Required — submissions without it are rejected.' },
  flatness_params: { type: 'json', description: 'Parameter declaration for the flatness check: [{name, value, step}] — every tunable numeric literal of the source (window lengths, thresholds, weights; NOT structural constants like 252 annualization). value must appear as a numeric literal in the source (mismatch aborts); step is the minimal meaningful step (window 10 -> 1, weight 0.65 -> 0.05). Omit entirely for genuinely parameter-free factors.' },
  admit_basis: { type: 'string', description: 'Admission track: "ic" (default, the IC_IR chain) or "tail" (the tail-spread chain for factors whose top-K group return is strong even with mediocre full IC — requires the auto-computed tail block from factor_evaluate; gates: top-N placebo z>=3, spread noise-gate |z|<3, selection-Jaccard N_eff deflation with cross-track Sidak alpha).', enum: ['ic', 'tail'] },
},
```

### factor_registry_update

```ts
parameters: {
  name: { type: 'string', required: true, description: 'Name of the registry entry to update.' },
  signal: { type: 'string', description: 'New human-readable factor definition (replaces the old one).' },
  note: { type: 'string', description: 'Remark appended to the entry (timestamped, kept as history).' },
},
```

### factor_standby_view

```ts
parameters: {
  min_abs_sr: { type: 'number', description: 'Minimum |sr_hat| for standby membership (default 0.75; will be recalibrated per-stratum after the shadow period).' },
  top_n: { type: 'integer', description: 'Max rows returned (default 30).' },
  family_dedup: { type: 'boolean', description: 'Collapse same-family entries (|rho|>=0.6) keeping the best (default true).' },
},
```

### factor_standby_combine

```ts
parameters: {
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
  k: { type: 'integer', description: 'Combination size (default 4; rule needs >=3 orthogonal members).' },
  ortho_th: { type: 'number', description: 'Pairwise |rho| ceiling (default 0.3).' },
  min_abs_sr: { type: 'number', description: 'Standby membership floor (default 0.75).' },
},
```

### factor_incubate_enqueue

```ts
parameters: {
  hashes: { type: 'json', required: true, description: 'List of standby-line hashes (prefixes OK, from factor_standby_view). Max 50.' },
  window_days: { type: 'integer', description: 'Fresh-window length in trading days (default 120).' },
},
```

### factor_incubate_status

```ts
parameters: {},
```

### factor_incubate_judge

```ts
parameters: {
  cohort_id: { type: 'string', required: true, description: 'Cohort id from factor_incubate_status.' },
  envId: { type: 'string', description: 'Environment id; defaults to primary.' },
},
```

### factor_journal_read

```ts
parameters: {
  level: { type: 'integer', description: '0=INDEX, 1=live full text, 2=one entry by entry_id (default 0).' },
  entry_id: { type: 'string', description: 'Entry id for level 2 (find ids in the INDEX).' },
},
```

### factor_journal_append

```ts
parameters: {
  content: { type: 'string', required: true, description: 'Markdown entry block(s). Each starts with `## <id> <status> <title>`; a registered entry states the expectation in free text (no template).' },
  source: { type: 'string', description: 'The factor source code — REQUIRED for registered entries: the engine hashes it into the ref line (you cannot know source_hash before evaluation). Pass the exact same source string you will submit to factor_evaluate.' },
},
```

### factor_journal_update

```ts
parameters: {
  id: { type: 'string', required: true, description: 'Entry id (from INDEX).' },
  status: { type: 'string', required: true, enum: ['open', 'registered', 'adjudicated', 'resolved', 'stale', 'active', 'retired', 'lesson'], description: 'Target status.' },
  note: { type: 'string', description: 'Optional note (≤300 chars) — resolution cause / calibration update. Becomes the epitaph tail when the entry is later archived.' },
},
```

### factor_journal_distill

```ts
parameters: {
  new_live: { type: 'string', required: true, description: 'Distilled live area full text. Keep: current beliefs (with confidence + falsification condition + trial refs), open hypotheses, live lessons. Drop: narrative, resolved entries (auto-archived).' },
},
```

### factor_journal_stats

```ts
parameters: {},
```

### factor_arxiv_search（仅 enableArxivSearch 开启时注册）

```ts
parameters: {
  query: { type: 'string', required: true, description: 'Methodology query.' },
  max_results: { type: 'integer', description: 'Maximum results (default 10, cap 25).' },
  category: { type: 'string', description: 'Optional explicit arxiv category (e.g. q-fin.ST). Omit for the default q-fin OR-chain filter.' },
  start: { type: 'integer', description: 'Pagination offset (0-based) for deep walks of the same query instead of re-hitting the top-N.' },
},
```
