# 模型可见上下文全量盘点 · 投研系统（bull-case-anatomy）

盘点日期：2026-09-30。执行依据：`analysis/glm_mining/PLAN-desktop-mcp-fix-and-archdocs-2026-09-30.md` 任务 E（E1 卡规格、E2 骨架投研节、E3 取证纪律、E4 验收）。

## 范围说明

- 本文件覆盖投研系统的 10 张情境卡：DSH 宿主侧 T-DSH-01 至 T-DSH-06，zcode 宿主侧 T-ZC-01 至 T-ZC-04。
- 投研系统指 `C:/Users/RTyyyy/Desktop/life-os/modules/investment/research/bull-case-anatomy/`（下称投研根）及其 `research/engine/` 下的 Python 命令行引擎。引擎没有常驻进程：模型看到的一切引擎内容，都是某次会话执行引擎命令后由命令输出带回的。
- 静态长文（persona 配置、技能壳、仓库纪律文件、标准提问文案等）全文收录于文末附录，卡片正文引附录号。
- 取证来源三类：①生产会话转录 `~/.dsh/sessions/--C-Users-RTyyyy-Desktop-life-os--/session-fe9657b1-6199-4a47-a80f-f31b7502b789/session.v4.jsonl.zstd`（zstd 压缩 JSONL，解压后按行取 JSON；下文简称"生产转录"，行号 L*n* 指解压后的 JSONL 行号）；②引擎与配置源码文件（路径逐卡给出）；③生产账本与状态文件（`research/ledger/*.jsonl`、`research/audit_state/*.json`）。个别情境从未在生产留档，按 E3 纪律标注"推定"并说明推断链。
- 引号内的文字一律逐字复制自上述实物。个别引擎输出原文本身含省略号字符（例如隔离门拒绝里的 `reason=…`、status 的 `……另有 N 扇 armed 门` 行、可读性拒绝里被生产转录截断的引用片段），这些是引擎文案或生产转录原样的字面内容，不是本盘点的删节标记；本盘点自身不使用任何省略号或"略"字删节。
- 术语速览（各卡内仍会就地把词解释到能独立读懂）：**线**=一条研究线，账本文件名即线名；**程序**=一组相关研究线的归属（如 growth）；**账本**=append-only 的事件日志（JSONL，带哈希链）；**门**=引擎在命令里做的检查项，不通过即拒绝执行；**armed 门**=此刻真的会拦的门；**档位**=研究深度档（quick/standard/deep）；**工作单**=引擎导出给模型作答的提示词清单文件。

---

# T-DSH-01 预置装载（系统级人设与技能壳）

## ① 编号与名称
T-DSH-01：预置装载（协议与机制指南）——「全景投研」预置给 DSH 投研会话注入的系统级上下文。

## ② 触发时机
- 会话以「全景投研」（comprehensive-investment）预置创建时装载；此后每一轮模型请求中持续在场（系统提示的一部分）。
- 生产转录中该会话共 11 轮，转录留存了两次系统消息渲染（L9 第 1 轮、L581 第 6 轮）——第二次渲染比第一次多出「收工纪律」一段，对应 2026-09-30 当天的配置更新（详见⑤）。
- 频次：每个投研 DSH 会话一份，全程在场。

## ③ 完整上下文
按块列出，每块标触发条件：

1. **persona 前缀（系统人设全文）**——触发条件：预置装载即在场，恒定。
   配置原文（YAML 折叠块，含 `{{model}}`、`{{cwd}}` 两个占位符）见**附录 A**；生产渲染版（占位符已替换为 `glm-5.3-flash` 与实际会话目录）逐字见**附录 B**。
   内容包括：投研根目录的定义与本机路径、bash 每次调用都是新 shell 须先 cd 的指令、冷启动先读投研根 AGENTS.md 的指令、四条铁律（三权分离/默认档开工/研报给人读/前瞻推演先行）、收工纪律、引擎是唯一裁判、拿不准先跑 status 与 `--interface` 的指引。
2. **DSH 通用系统提示头与宿主纪律**——触发条件：恒定。
   生产渲染版系统消息以 `You are an AI agent powered by DeepSeek Harness.` 开头，随后接 persona 前缀，其后是宿主通用段（@路径规则、退出码标注规则、read/glob/grep 工具使用规则、后台任务跟踪规则等）。宿主通用段与投研无关、由宿主注入，本卡只登记其存在与边界；全文形态属宿主盘点范围，不在此复制。
3. **工作区指引（monorepo 根 AGENTS.md 重定向桩）**——触发条件：会话目录在 life-os 根时，宿主把该目录的 AGENTS.md 作为工作区指引以 system-reminder 注入（生产转录 L11 实证）。全文见**附录 C**。它只有一件事：把投研会话指到投研根的 AGENTS.md 并要求引擎命令先 cd。
4. **技能清单提醒（含投研技能壳的描述行）**——触发条件：预置的技能文件系统插件装载后，会话中出现可用技能清单的 system-reminder（生产转录 L13 实证）。投研侧两个技能壳经 `customSkillDirs` 绝对路径补挂进清单：
   - `engine-status`，其描述行为逐字：「`engine-status`: 引擎正向状态视图（status-first）。当用户或会话需要知道某条研究线「现在处于什么状态、下一步能做什么合法动作」、被引擎拒绝后不知道出路、或冷启动不想读引擎源码找接口时使用。命令：tree.py status / --interface。」
   - `quant-discipline`（描述行同其 SKILL.md frontmatter，见附录 E）。
   技能正文不自动注入；模型调用技能时全文进入上下文（生产转录 L59-L60 实证：调用 `skill` 工具加载 engine-status，全文回显）。两个技能壳全文见**附录 D、附录 E**。
5. **预置 description 字段**——触发条件：不进入模型上下文。它是预置注册信息（宿主 UI 展示用）；生产转录的模型可见事件中未出现该字段文本。登记于此是为了说明它的去向，避免误计为模型可见上下文。原文含 2026-09-30 的两条注记（投研根放宽、技能根 customSkillDirs 补挂），见附录 A 首段。

## ④ 溯源与设计功能
- persona 前缀：来源 `~/.dsh/profiles/desktop/cordis.patch.yml` 中 `preset-comprehensive-investment` 条目的 `persona` 插件（`@deepseek-ai/dsh-persona`）`config.prefix` 模板；渲染由宿主在组请求时完成（占位符替换）。设计功能：把投研会话的行为铁律钉在每一轮请求的最上层——研究决策权归用户、开工不打断用户问问题、产出面向人类读者、结论必须先推导未来再等数据校准；同时给出引擎的调用姿势（先 cd、冷启动读 AGENTS.md、拿不准跑 status）。`~/.dsh/profiles/desktop-diag/cordis.patch.yml` 存同款 persona（已逐字 diff，仅宿主 UI 版本号行不同），是诊断环境的同步副本。
- 收工纪律段：同上文件同一条目（2026-09-30 新增）。设计功能：规定会话必须推进到引擎报"无下一步合法动作"或交付完成才收工，把变更类命令尾部的『下一步』提示当作待办队列逐条清空；只允许三种中途停（等用户裁决/不可逆或付费确认/用户限定范围的即答任务）。
- 工作区指引：来源 life-os 根 `AGENTS.md`（投研会话重定向桩），由 DSH 宿主的 workspace-instructions 机制注入。设计功能：DSH 会话常在 git 根（life-os）启动而非投研根，此桩保证无论从哪里启动都能找到投研根并按正确姿势执行引擎命令——这是 2026-09-30「投研根放宽」改动的配套件。
- 技能壳补挂：来源同 patch 文件中 `skill-filesystem` 插件的 `customSkillDirs`（指向 `bull-case-anatomy/.agents/skills`）。设计功能：DSH 的项目根=git 根，默认技能发现找不到投研根下的技能壳；绝对路径补挂后，投研技能（engine-status 正向状态视图、quant-discipline 量法纪律）才出现在会话技能清单里，可被调用注入。
- 技能正文：`engine-status` 壳（附录 D）教模型"拿不准先跑 status、被拒看 armed 门出路、参数问题查 --interface"；`quant-discipline` 壳（附录 E）是发现入口，指路到投研仓库内的权威全文 `research/papers/quant-skill-2026-09/SKILL.md`。

## ⑤ 条件与现状备注
- 版本演变实证：生产转录第 1 轮渲染（附录 B 上段）无「收工纪律」段；第 6 轮渲染（附录 B 下段）已含该段。当前配置（附录 A）含该段。即收工纪律是 2026-09-30 会话进行中生效的更新。
- 负情境一：未选「全景投研」预置的 DSH 会话（桌面默认预置是 standard）没有这套人设、没有投研技能壳补挂——对这样的会话，投研引擎只是磁盘上的一个 Python 脚本，模型不知道它的存在与规矩。
- 负情境二：技能壳正文只有被调用才注入；模型不调用 `skill` 工具时，上下文里只有描述行。
- zcode 会话不经过此预置（zcode 有自己的技能根与系统提示），engine-status/quant-discipline 壳对 zcode 不自动挂载；zcode 侧获得同等内容的途径是直接读投研根的文件（见 T-ZC-01/T-ZC-02）。
- web 宿主的预置形态本次未取证，不作断言。

---

# T-DSH-02 开题回执（默认意图登记与开工提问）

## ① 编号与名称
T-DSH-02：开题回执——模型执行 `tree.py auto-intent` 默认登记新研究线时，引擎返回的回执文本；及配套的开工标准提问文案。
命名说明：任务书 E2 骨架称此卡为"开题回执（七查）"。对引擎源码（engine/ 全目录）、投研根 AGENTS.md、PROTOCOL.md、MECHANISM_GUIDE.md 做全文检索，"七查"一词不存在，也没有七项检查的对应机制。实际机制是：auto-intent 默认登记（免提问）+ 无程序全景新对象的 A/B 两选项提问（引擎标准文案 D1）。本卡按实际机制立卡，"七查"判定为任务书笔误。

## ② 触发时机
- 用户提出研究需求（新开线或旧线重启）时，persona 铁律二要求模型第一条回复即执行 `py -3.13 research/engine/tree.py auto-intent --target <线名>`；每次新线开题执行一次。
- 已登记过意图的线再执行会被拒绝（幂等），见③拒绝形态。
- D1 提问文案仅当"新对象且无程序全景归属"时由模型取用（`--show-d1`）并原文转述给用户；有程序归属的线不问。

## ③ 完整上下文
**回执模板**（源码生成器，占位符以 `{}` 标注）：

```
已登记 #{seq}: {lane}（默认档 deep，auto_default 常设授权{d1}）——第一条回复即研究动作，不阻塞
```

条件块：
- `{lane}` 两分支：线名有程序全景归属时为 `program-cycle（{program}）`；无归属时为 `single`。
- `{d1}` 附加段，仅当 `--asked`（本次已向用户提问）时出现：`，D1 已提问、选 {d1_choice}`；若用户未答复再加 `（无答复按 A）`；若答复为 B 再加 `——先走全景建档流程，其后程序线重登 program-cycle`。未提问时此段为空。
- 后随提示行，触发条件：登记结果为 `single` 且未提问时打印：
  `D1 提示：无全景对象可用 --show-d1 取标准 A/B 文案，经宿主提问工具原文转述（当轮可问可不问；无答复按 A 继续，不阻塞）`

**生产实例**（生产转录 L332，两条新线连续登记，逐字）：

```
已登记 #1: program-cycle（growth）（默认档 deep，auto_default 常设授权）——第一条回复即研究动作，不阻塞
====================
已登记 #1: program-cycle（growth）（默认档 deep，auto_default 常设授权）——第一条回复即研究动作，不阻塞
```

（两行分属 growth-naval-target 与 growth-mil-strategy 两线，各自账本第 1 条。）

**拒绝形态**（源码生成器，逐字）：
- 重复登记：`意图已登记（#{seq} {intent}），免问默认不覆盖——显式改档走 intent-chat / 终端 intent 通道`
- 有程序归属仍想问 D1：`D1 提问只用于无全景新对象——本线已有程序归属 {prog}，直接按程序循环开工，无需 A/B`
- 伪造答复防线（拒绝文案）：`d1_choice 须伴随 question_asked=True——答复前必先真实提问；伪造提问工具的用户答复=硬禁令违规`

**D1 标准提问文案**（引擎持有，模型原文转述、选项不可改写；全文见附录 G）：
```
=== 研究线 {target} 开工确认（引擎标准文案，原文转述、选项不可改写）===
本对象暂无程序全景图，请选择开工方式：
(A) 单线深解剖——基准+单次深解剖直接研究本对象；
(B) 先建全景再研究——先走程序全景建档流程，其后本程序线按程序循环展开。
无答复不阻塞：按 (A) 继续。
```

**入链的常设授权注记**（登记事件的 user_quote 字段；生产账本 `ledger/growth-naval-target.jsonl` 第 1 条逐字，全文见附录 G）：
```
常设授权（2026-09-22 用户指令：开工提问机制减重——已有的提问与选择、强制用户原话机制太重，去掉选项，默认全景投研·深解剖，免前置问答轮；任务书 INTENT_LIGHTENING_MANDATE.md）
```

**旧通道兼容文案**（`intent --show` 的开工提问标准文案与终端交互路径文案，当前主径不用但保留；全文见附录 G）。

## ④ 溯源与设计功能
- 回执打印：`engine/tree.py` main 的 `auto-intent` 分支。设计功能：一句话告诉模型三件事——登记成功（带账本序号）、按什么意图与档位运转（程序循环或单线、恒 deep）、开工不需要等用户回答（不阻塞）。回执本身就是"第一条回复即研究动作"的完成信号。
- 登记逻辑与防伪：`engine/intent.py` 的 `auto_intent`、`d1_question_text`、`declare_intent`（含 `_AutoIntentWriteGuard` 写权限 guard——`auto_default` 标记只有 auto-intent 原语执行期间可写，直写账本伪造会被账本层 schema 门拒绝）。设计功能：把"开工免问"做成有据可查的常设授权而非模型自说自话；授权注记逐字入链，交付门四路授权之一即此标记（见 T-DSH-04 交付门拒绝文案里的③）。
- D1 文案由引擎持有、模型只转发：设计功能是防模型改写选项措辞（例如把"B 先建全景"描述得难以入选）；无答复默认 A 保证不阻塞。
- 账本留痕：`research/ledger/<线名>.jsonl` 的 `intent_declared` 与 `research_started` 事件（生产实例见附录 G 末）。

## ⑤ 条件与现状备注
- 生产转录中该会话登记的 5 条 growth 旧线早已登记过意图，本次新开 2 线均走 auto-intent 且有程序归属（growth），故未出现 D1 提问实例；D1 文案取证自源码（其生产形态为逐字打印，确定性输出，推定与源码一致）。
- 负情境：旧线重启时 auto-intent 被拒（意图已登记），模型看到的是拒绝文案而非回执——这是设计行为，防止默认档覆盖用户显式选过的档位。
- `cf-only` 意图永不默认、永不提问，只能任务书/用户明确指定后走显式通道登记。

---

# T-DSH-03 干活回合引擎回执（status 视图 / 成功回执尾部 / --interface）

## ① 编号与名称
T-DSH-03：干活回合引擎回执——研究推进过程中模型执行引擎命令得到的三类常规输出：status 正向状态视图、变更类命令成功输出末尾的"下一步"行、`--interface` 全命令接口导出。

## ② 触发时机
- `status --target <线名>`：模型拿不准下一步、被拒绝后想看全部门、或按 persona/技能指引例行查看时执行；随时可跑，只读。
- "下一步"行（emit_line）：每个变更类命令（estimate/register/terminate/deliver 等约 40 个，见源码 `MUTATING_CMDS` 集合）成功执行后自动附在输出末尾；每次成功变更一条。
- `--interface`：模型需要查某命令的参数时执行；按需。
- 频次：一个正常干活回合里 status 与 emit_line 是最高频的两类引擎文本。

## ③ 完整上下文
**status 人读摘要——生产实例一**（生产转录 L67，growth-meta3，逐字全文）：

```
== growth-meta3 正向状态（status-first·账本纯函数） ==
相：研究｜档位 deep｜程序 growth
armed 门 6 扇（先解门）：
  - 主干定案门：主干未定案 ['M0']
      → tree.py estimate / crosscheck（取证入账）
      → tree.py terminate --status <终态> --reason <理由>
  - 覆盖清单门：未研究方面 ['层位×数据源矩阵审计', '三门筛选打分', '当日判别预言', '隔离评审非换皮']
      → tree.py coverage（研究后改 status 或 excluded 带原因）
  - 管线完备门：2 项仪器未跑（如 承重分析未运行（有证据池的线必须登记 loadbearing_map））
      → tree.py run-pipeline --target growth-meta3
      → tree.py pipeline-exempt <仪器> --reason <理由>（注意力诊断不可豁免）
  - 机制地图门：未展开项 ['传导层建模']
      → tree.py register --mechanism <机制>（每条机制=一个分支）
      → tree.py coverage（数据不可得的排除带原因）
  - 树级评审门：从未提交
      → tree.py tree-review --verdict close|expand（收口由隔离评审判定）
  - 终态完备门：3 项（如 格 growth程序|拐点|方法面 无终态（结论/停放分歧/预言）——研究未完成）
      → 补齐终态结论（交付的必须是结论，不是未完工工作）
欠账：rounds_used=4
下一步（机械判定）：
  - 解门[主干定案门]：tree.py estimate / crosscheck（取证入账）
  - 解门[覆盖清单门]：tree.py coverage（研究后改 status 或 excluded 带原因）
  - 解门[管线完备门]：tree.py run-pipeline --target growth-meta3
  - ……另有 3 扇 armed 门：tree.py status --target growth-meta3 看全量
--json 出机器读数；--interface 出全命令接口（C4 机械导出）
```

注意：上一段引文里"……另有 3 扇 armed 门"中的"……"是引擎生成器的原文文案（源码字面量，表示"此外还有"），非本盘点删节。

**status 结构与条件块**（源码 `render` 函数逐段对应）：
- 首两行（线名/相/档位/程序）：恒在。相四值：未开表（无意图登记）｜研究｜审计｜已交付。
- `armed 门 N 扇（先解门）：`块：仅当此刻存在会拦的门；每门一行详情加若干行 `→ 出路命令`。门全集群见附录 D（技能壳列了 28 种）。
- `armed 门：0（按相推进）`：无门时打印此行（生产实例见附录 H 第二块 growth-meta3 已交付段）。
- `欠账：`行：各项计数非零才列出（义务未决数/蒸馏欠账/工作单欠答/程序张力/程序待读簇/已用轮数等）。
- `到期/待结算 N 项（前 5）：`块：仅当存在未结算预言、递延义务触发器或停放问题。
- `未解之题 N 类（主动回路的材料面）：`块：仅当线有程序归属且该程序存在未了结事项（未走之路积压/未决张力/到期未复盘预言/押注校准漂移）。生产实例见附录 H 第三块。
- `下一步（机械判定）：`块：恒在；最多前 3 扇门的出路，无门时按相给该相的合法动作；超过 3 扇时以"……另有 N 扇 armed 门"行收尾（同上，"……"为引擎原文）。
- 末行 `--json 出机器读数；--interface 出全命令接口（C4 机械导出）`：恒在。

**"下一步"行（emit_line）——生产实例两则**（逐字）：
- `下一步（机械判定，非方向性内容）：解门[主干定案门]：tree.py estimate / crosscheck（取证入账）；解门[覆盖清单门]：tree.py coverage（研究后改 status 或 excluded 带原因）`（生产转录 L593，pipeline-exempt 成功后）
- `下一步（机械判定，非方向性内容）：监视态：触发器/预言到期看 tree.py status --target growth-meta3`（生产转录 L990，deliver 成功后）

emit_line 与 status 同源但为轻量档：只看线级门（程序级门扫描开销大），最多 2 个动作；固定前缀 `下一步（机械判定，非方向性内容）：`。

**--interface 导出——头部固定四行 + auto-intent 节**（2026-09-30 自 zcode 通道实跑逐字，DSH 通道同源；生产转录 L337 曾把它存为 543 行临时文件）：

```
# 引擎接口机械导出（--interface）
# 生成方式：argparse 内省＋账本载荷字段 AST 提取；无手写区（K5）。
# 交叉约束（字段间依赖、条件必填、格式要求）不在此导出——以各门拒绝文案为准。

## 子命令（argparse 内省）

### absorb-obligation
  --target（必填）
  --seq（必填）
  --line（必填｜覆盖该义务的既有线名）
  --reason（必填）

### anchor-supplement
  --target（必填）
  --adjudication_seq（必填｜要补锚的 tension_adjudicated 条目序号）
  --evidence_ref（可选｜证据锚：线名#序号（须晚于矛盾开立——引擎验货））
  --anchor_exempt（可选｜豁免理由 ≥30 字（须 anchor_review 复核带账））
  --note（可选）

### auto-intent
  --target（必填｜默认意图登记（常设授权 2026-09-22：开工提问机制减重）——档位恒 deep；线名有既有程序全景归属→program-cycle，无→single（D1 场景））
  --program（可选｜显式程序名（须有全景文档；缺省按线名自动归属））
  --show_d1（可选｜只打印 D1 标准提问文案（无全景新对象：A/B 两选项、无答复按 A；原文转述给用户，选项不可改写），不登记）
  --asked（可选｜本次已用宿主提问工具原文转述 D1 提问（question_asked=True 入链））
  --d1_choice（可选｜用户对 D1 的答复（A=单线深解剖；B=先建全景再研究；已提问而不给=无答复按 A 继续；伪造答复=硬禁令违规））
```

导出全文 543 行（当前引擎），其余为全部子命令的参数表与"账本载荷字段（AST 提取）"表；本卡不复制全文，生成方式为确定性机械导出（同引擎同版本两次运行字节一致），头部与所引节即为形态样张。

## ④ 溯源与设计功能
- status：`engine/status.py` 的 `collect`（聚合）/`render`（人读）/`gates_armed`（门的读侧投影）/`debts`/`triggers_due`/`next_actions`；CLI 入口在 `engine/tree.py` 的 `status` 分支。设计功能：status 是账本的纯函数——只把各门"此刻会不会拦"聚合给模型看，不新增判断、不写任何账；模型被任何门拒绝后来跑一次 status 就能看到全部拦路的门和每门的出路命令，不用逐门试错。
- "下一步"行：`engine/status.py` 的 `emit_line`，由 `engine/tree.py` 在变更类命令成功后调用（命令清单=源码 `MUTATING_CMDS` 集合）。设计功能：每次成功变更立即给模型下一个合法动作，形成"做完一步就知道下一步"的推进链；fail-open——读侧出异常时返回空串，不毁成功响应。
- 方向词黑名单：`engine/status.py` 的 `DIRECTION_WORDS` 与 `_guard`——生成器文案含"建议/优先/值得/应该/不妨/最好/研究方向"任一词即抛错。设计功能：三权分离的引擎侧保障——引擎只报合法动作与机械事实，绝不产生研究方向的措辞；这是收工纪律"『下一步』提示就是待办队列"能安全成立的前提。
- --interface：`engine/status.py` 的 `interface_text`+`ledger_fields_table`（argparse 内省+各引擎模块 `ledger.append` 调用点的 AST 字段提取），入口在 `engine/tree.py` 的 `--interface` 分支。设计功能：模型不读源码即可知道每条命令吃什么参数、每类账本事件带哪些字段；交叉约束（字段间依赖、条件必填）故意不导出，以各门拒绝文案为准（拒绝即教学）。
- 跨通道同源注明：T-ZC-02 记录同一 CLI 在 zcode 通道的输出实例——文本出自同一 Python 命令行程序，两通道字节同源。

## ⑤ 条件与现状备注
- 生产转录中 status 的三类真实用法齐备：开题后查五线状态（L67）、交付被拒后查门（L963 附近）、收工前终查（L1416/L1478，附录 H 三连块）。
- 负情境：引擎无守护进程、无推送——模型不执行命令就看不到任何 status/下一步文本；会话停转期间引擎文本不会自己出现。
- `--json` 机器读形态生产实例：L1280 把 `status --target growth-naval-target --json` 转存临时文件后用 python 摘录字段（文件已清理，转录留有调用记录）；本卡以人读形态为准立卡，json 形态同源。

---

# T-DSH-04 评审裁决回执（成功回执与拒绝文案形态）

## ① 编号与名称
T-DSH-04：评审裁决回执——模型提交评审/裁决/交付类命令时引擎返回的文本，重点是拒绝文案的形态（引擎门禁的 verdict/reject 文本）。

## ② 触发时机
- 成功回执：评审或裁决命令通过全部检查入账时（tree-review/frame-review/cf-batch/cf-verdict/model-review/tension-adjudicate/audit resolve/deliver 等）。
- 拒绝回执：任一门检查不过时。统一形态为 `拒绝: {原因文本}`，进程退出码 1（生产转录中以 `[exit code: 1]` 标注）。
- 频次：每条变更类命令零或一次；交付（deliver）在材料不全时可连续多次被拒，是拒绝文案最密集的命令。

## ③ 完整上下文
**成功回执——源码生成器模板**（逐字，占位符 `{}`）：
- `反事实批次已发起 #{seq}（generator={generator}）`（cf-batch）
- `树级隔离评审已入账本：verdict={verdict}`（tree-review）
- `主干元反思已入账 #{seq}（disposition={disposition}, generator={generator}）`（frame-review）
- `cf 裁决已入账 #{seq}（hyp={hyp_seq}, {verdict}）`（cf-verdict；verdict 取值 killed/low_potential/pending_test/candidate）
- `建模评审已入账 #{seq}（spec #{spec_seq} → {verdict}）`（model-review）
- `张力 #{seq} 已裁决：{outcome}（#{e_seq} 入账）`（tension-adjudicate）
- 交付成功回执（JSON+尾部注入）归 T-DSH-06 专卡，此处不重复。

**成功回执——生产实例**（生产转录 L614，逐字）：`反事实批次已发起 #27（generator=isolated）`

**拒绝统一形态**：`拒绝: {门名/原因文本}`，退出码 1。pwsh 通道下模型常以 `2>&1` 把 stderr 拼进结果（生产转录多处实证）。

**拒绝生产实例五则**（生产转录逐字）：

1. 隔离门（L609，判定/反思类角色必须隔离执行）：
```
拒绝: 隔离门（cf-generation）：判定/反思类角色必须由隔离上下文执行——经 dispatch 派发或 subagent 派生（--model 记 '…(via dispatch)' 或 '…(via subagent)'，两者同权），或登记豁免：py -3.13 engine/ledger.py append --target growth-naval-target --type isolation_exempted --field duty=cf-generation --field reason=…（inline 自评不再默认放行，2026-09-19）
```
（同轮 growth-mil-strategy 线收到同款拒绝，仅 target 不同。文中"…"为引擎原文占位符。）

2. 交付门·深度不足（L908）：
```
拒绝: 交付门禁：deep 档树深度不足（三级及以上节点仅 0 个，需 ≥2）——一级分支须长出二级展开（子节点），除非逐条声明并经评审确认无值得单开的问题
```

3. 交付门·完整性硬检查（L963）：
```
拒绝: 交付门禁：完整性硬检查 3 处缺场——白话化是语言降门槛不是内容删减（缺项前 8 条）：
  格结论缺场：growth程序|拐点|方法面（结论总表须含每格最新结论）
  格结论缺场：growth程序|簇归属|方法面（结论总表须含每格最新结论）
  格结论缺场：growth程序|趋势|方法面（结论总表须含每格最新结论）
补齐路径：tree.py narrative --reader-proxy --target growth-meta3（读者代理从账本结构化重生成，含结论总表/假设生死表机械层）
```

4. 交付门·可读性（L1175，渲染稿违禁记号）：
```
拒绝: 可读性门禁拦截——渲染稿含违禁记号，拒绝落盘（修登记簿白话名/单位、或模板记号后再渲染）：
  [活研报] 结论既无登记单位也无修正单位——在登记簿 set_render_meta 配单位：…上**，「舰船链的拐点」给出了定案读数：当前值 2（单位待补），出自 [naval-target](2026-…
```
（同款拒绝同轮共 6 条单位缺失行，上录第一条完整行与最长行截断处均为转录原样；转录本身对该行回显到此长度。）

5. 交付门·管线完备/活研报过期（L1195）：
```
拒绝: 管线完备性门禁（2026-09-16 用户指令：所有机制强制性经历，跳步唯一出口=pipeline-exempt 带理由入账）——活研报过期：图已重建而活研报未同步——重跑 claim_graph build
```

**交付门四路授权拒绝文案**（源码生成器，本会话未触发，源码取证）：
```
交付门禁：意图登记未经授权（四路授权均未命中）——①用户终端 tree.py intent（tty 确认码）；②会话 intent-chat 登记用户原话逐字（伪造=硬禁令违规）；③intent-chat --via-ask-tool（ask-user 作答，原话在宿主会话日志）；④auto-intent 默认登记（常设授权 2026-09-22：开工提问机制减重）。伪造用户原话/意图状态文件/默认授权标记属硬禁禁违规
```
（末句"硬禁禁违规"为源码字面，逐字照录。）

**Lakatos 记分门拒绝**（audit resolve 维持被拒场景，源码取证，生产未触发）：
```
Lakatos 记分门：连续两个审计窗口变量定义级修补 ≥2 且新颖预测兑现 0——『维持』非法，须 修订 或 纲领质疑（强制换纲领的引擎落点；如认为窗口计数有误，先补 outcome_resolution 结算再 resolve）
```

## ④ 溯源与设计功能
- 统一拒绝出口：`engine/tree.py` main 的异常处理（`print(f"拒绝: {ex}")` + 退出码 1）。设计功能：所有门共用一个输出形态，模型见到"拒绝:"即知这是引擎裁决而非工具故障；文案内嵌出路（该跑什么命令、豁免怎么登记），配合 status 形成"拒绝即教学"的闭环（AGENTS.md 与 engine-status 壳均明文"交叉约束以拒绝文案为准"）。
- 各门文案来源：交付门栈在 `engine/gates.py`（深度门/完整性检查调用 completeness/可读性调用 compliance+readability/管线完备调用 unbias/意图授权门）；隔离门在 `engine/node.py`（`_isolation_gate`）；建模评审与构造量门在 `engine/gates.py` 与 `engine/node.py`；Lakatos 记分门在 `engine/audit.py`（resolve）。
- 设计功能逐条：隔离门防模型自评自过（判定类产出必须在会话外做出）；深度门防浅层交付；完整性门防"白话化=内容删减"；可读性门防过程记号进人读产物；活研报过期门防全局认知载体（命题图渲染物）滞后；四路授权门防无授权开工；记分门防框架在反复打补丁下僵死。
- 跨通道同源注明：拒绝文案同样出现在 zcode 通道（T-ZC-02 同一 CLI）；audit 类拒绝归 T-ZC-03 一并覆盖。

## ⑤ 条件与现状备注
- 本会话生产实例覆盖拒绝文案的四种典型（隔离/深度/完整性/可读性/活研报），交付门其余分支（反事实批次不足/蒸馏欠账/机制地图未展开/随机双环审计未完成/合同缺失等）文案在源码 `engine/gates.py`，形态同上，未逐条在生产触发。
- 负情境：拒绝之外引擎无中间态提示——门通过时模型只看到成功回执，看不到"差一点没过"的警告（compliance 的"警告 N"行除外，它随 check 输出但不拦交付，生产实例见 L908 头部"警告 4"块）。
- pwsh 包装噪声：DSH 侧 stderr 输出会被 PowerShell 包装成 NativeCommandError 红字形态（生产转录 L1220 实证），模型需要在噪声里辨认引擎正文；zcode 侧（bash）无此包装，正文干净。

---

# T-DSH-05 工作单两拍（计划相送达与执行相回填）

## ① 编号与名称
T-DSH-05：工作单两拍——引擎需要模型意见的仪器（组合读数/摧毁重放/对照电池/盲读/法庭重喂/深挖）拆成两步交互：第一拍引擎导出提示词（PENDING 停住），第二拍会话以隔离子代理作答后回填续推。

## ② 触发时机
- 第一拍：模型执行 `run-pipeline`、`tension-read`、`method-audit`、`converged-dig` 等命令，引擎算到需要模型作答处——导出提示词到工作单文件、打印 PENDING 指引、退出码 2。
- 中间步：`model-work --prompts` 查看欠答提示词全文（随时可查）。
- 第二拍：隔离子代理逐条作答、写入回复 JSON 文件后，原命令带 `--replies-file`（或 `model-work-fill`）回填；缺答则再次 PENDING，循环至完成。
- 频次：每个仪器操作一轮两拍；一晚的交付流程可能循环数次。

## ③ 完整上下文
**第一拍 PENDING 指引——tension-read 版源码模板**（stderr，逐字；`{}` 为占位符）：
```
[子代理模式] 盲读工作单已导出：{key}（{k} 位读者）
  看提示词：py -3.13 research/engine/tree.py model-work --target {target} --prompts
  隔离子代理逐位作答（内容裸呈、元信息剥离、输出按读者格式）→ 写 JSON {"r0": 正文, "r1": 正文} →
  py -3.13 research/engine/tree.py tension-read --program {program} --target {target} --replies-file <回复JSON> --dims {dims 或 --entity {entity}}
```

**第一拍——生产实例**（生产转录 L1220，pwsh 包装下逐字，噪声行原样保留）：
```
[stderr]
py : [子代理模式] 盲读工作单已导出：read:growth:实体:舰船链（2 位读者）
所在位置 行:1 字符: 232
+ ... ONUTF8='1'; py -3.13 research/engine/tree.py tension-read --program g ... +
+ ... ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    + CategoryInfo          : NotSpecified: ([子代理模式] 盲读工作单已导...h:实体:舰船链):String) [], RemoteException
    + FullyQualifiedErrorId : NativeCommandError

  看提示词：py -3.13 research/engine/tree.py model-work --target growth-naval-target --prompts
  隔离子代理逐位作答（内容裸呈、元信息剥离、输出按读者格式）→ 写 JSON {"r0": 正文, "r1": 正文} →
  py -3.13 research/engine/tree.py tension-read --program growth --target growth-naval-target --replies-file <回复JSON> -entity 舰船链
[exit code: 1]
```
（退出码显示 1 是 pwsh 对退出码 2 的包装显示；同形态输出在 zcode/bash 通道为退出码 2。）

**第一拍 PENDING 指引——run-pipeline 版源码模板**（stderr，逐字）：
```
[子代理模式] 工作单欠读者回复——会话以隔离子代理逐条作答后：
  py -3.13 research/engine/tree.py model-work --target {target} --prompts  # 看欠答提示词
  py -3.13 research/engine/tree.py run-pipeline --target {target} --plan-file <计划> --replies-file <回复JSON>  # 回填续推
```

**中间步：model-work 台账——源码模板**（逐字；空工作单时为 `[model-work] {target} 工作单空（无待答操作）`）：
```
[model-work] {target} 工作单 {n} 操作：
  [{kind}] {key}——{need} 问，{已齐（可续推）|缺 N：{missing 前 8 个}}
```
`--prompts` 时对每个缺答操作追加 `===== {key} / {item_id} =====` 分隔的提示词全文。

**model-work --prompts——生产实例全文**（生产转录 L1395，growth-meta3，2334 字符逐字，含导出的读者提示词原文）：

```
[model-work] growth-meta3 工作单 14 操作：
  [combination] combination:growth程序|簇归属|方法面-C48656——5 问，已齐（可续推）
  [destruction] destruction:growth程序|簇归属|方法面-D1052——4 问，已齐（可续推）
  [combination] combination:growth程序|趋势|方法面-C33772——5 问，已齐（可续推）
  [destruction] destruction:growth程序|趋势|方法面-D1052——4 问，已齐（可续推）
  [combination] combination:growth程序|拐点|方法面-C78039——5 问，已齐（可续推）
  [destruction] destruction:growth程序|拐点|方法面-D1052——4 问，已齐（可续推）
  [battery] battery:auto——4 问，已齐（可续推）
  [court] court:growth程序|簇归属|方法面-C48656:EV-M3-SKIN-8——3 问，已齐（可续推）
  [court] court:growth程序|簇归属|方法面-C48656:EV-M3-GATE-8——3 问，已齐（可续推）
  [court] court:growth程序|趋势|方法面-C33772:EV-M3-P123-3——3 问，已齐（可续推）
  [court] court:growth程序|趋势|方法面-C33772:EV-M3-DAYCHK-3——3 问，已齐（可续推）
  [court] court:growth程序|拐点|方法面-C78039:EV-M3-CFKILL-8——3 问，已齐（可续推）
  [court] court:growth程序|拐点|方法面-C78039:EV-M3-FIX-1——3 问，已齐（可续推）
  [read] read:growth:实体:growth程序——2 问，缺 2：r0,r1

===== read:growth:实体:growth程序 / r0 =====
你是独立的投研断言审读员。只基于给定断言内容作答，不猜测来源。
你的回复最后必须是一个机器可读块（前面可以有分析，但该块必须逐字按以下骨架，不要代码围栏、不要加行外解释）：
张力：
- C1×C2：一句话理由——归路（归路∈口径分裂/实体分裂/证据对决/真矛盾）
-（可多条；一条都没有就写「- 无」）
无张力对：C3×C4, C5×C6（你考察过并判无张力的编号对，逗号分隔；没有写「无」）
该块是唯一的裁决依据，缺块或改行式=无效答卷。

以下是一个投研程序产出的 3 条断言（编号 C1..C3，顺序随机）：

C1：[growth程序·趋势|方法面] 值 3.0 带[3.0, 3.0]；依据 median of 1 survivors
C2：[growth程序·簇归属|方法面] 值 8.0 带[8.0, 8.0]；依据 median of 1 survivors
C3：[growth程序·拐点|方法面] 值 4.5 带[1, 8]；依据 median of 2 survivors

任务（高召回定位，不计代价地提案，精度由后续裁决负责）：
1. 找出断言之间的张力——矛盾、互相约束、联合不可信、口径/实体可能不同；
  特别注意：本簇跨多个研究侧面，从没被一起看过的断言对最可能藏着没被发现的矛盾或关联。
  每条张力给：涉及编号 + 一句话理由 + 你猜的归路（口径分裂/实体分裂/证据对决/真矛盾）。
2. 报告你考察过并判为无张力的编号对（负空间——不许静默跳过）。

===== read:growth:实体:growth程序 / r1 =====
你是独立的投研断言审读员。只基于给定断言内容作答，不猜测来源。
你的回复最后必须是一个机器可读块（前面可以有分析，但该块必须逐字按以下骨架，不要代码围栏、不要加行外解释）：
张力：
- C1×C2：一句话理由——归路（归路∈口径分裂/实体分裂/证据对决/真矛盾）
-（可多条；一条都没有就写「- 无」）
无张力对：C3×C4, C5×C6（你考察过并判无张力的编号对，逗号分隔；没有写「无」）
该块是唯一的裁决依据，缺块或改行式=无效答卷。

以下是一个投研程序产出的 3 条断言（编号 C1..C3，顺序随机）：

C1：[growth程序·拐点|方法面] 值 4.5 带[1, 8]；依据 median of 2 survivors
C2：[growth程序·趋势|方法面] 值 3.0 带[3.0, 3.0]；依据 median of 1 survivors
C3：[growth程序·簇归属|方法面] 值 8.0 带[8.0, 8.0]；依据 median of 1 survivors

任务（高召回定位，不计代价地提案，精度由后续裁决负责）：
1. 找出断言之间的张力——矛盾、互相约束、联合不可信、口径/实体可能不同；
  特别注意：本簇跨多个研究侧面，从没被一起看过的断言对最可能藏着没被发现的矛盾或关联。
  每条张力给：涉及编号 + 一句话理由 + 你猜的归路（口径分裂/实体分裂/证据对决/真矛盾）。
2. 报告你考察过并判为无张力的编号对（负空间——不许静默跳过）。
```
（r0 与 r1 同题但断言编号排布不同——双读者互不知晓、内容裸呈、编号引擎回解。）

**第二拍回填回执——源码模板**：
- tension-read 版：`盲读完成：{k} 读者，提案 {n} 条（双读者收敛 {conv}）——cluster_read 已登记，拦截解除`
- model-work-fill 版：`[model-work-fill] {key}：本回填 {filled}｜已齐 {have}/{need}`
- 数字溯源行（条件块，ntrace 启用的程序才有）：
  - 有来历不明数字：`[数字溯源] {n} 个数字来历不明已标记待复核（材料在场/计算痕迹均无）——逐个核：核对提示词原文或补 python 复算痕迹；台账在 {target}-MODELWORK.json 的 ntrace 节`
  - 全部有出处：`[数字溯源] 全部数字有出处（引用在场或附计算痕迹）`
- 下一回合材料面（条件块，程序有未解之题时）：`未解之题（下一回合的材料面）：[{kind}] {n}；[{kind}] {n}`（join 分隔符为全角分号）

**第二拍——生产实例**（生产转录 L1416，逐字）：
```
盲读完成：2 读者，提案 3 条（双读者收敛 3）——cluster_read 已登记，拦截解除
下一步（机械判定，非方向性内容）：监视态：触发器/预言到期看 tree.py status --target growth-meta3
```

**回合契约拒绝**（条件块：深挖类操作 dig/method_audit 的回复必须带"燃料节"，源码生成器逐字）：
```
回合契约：{key}/{item_id} 缺燃料节 meta.fuel——深挖类操作每回合 ≥1 条新问题带三要素（q≥12 字/why_now≥10 字/cost≥10 字）
```

**两拍协议的文档正文**（`research/MECHANISM_GUIDE.md` 子代理模式节原文）见**附录 I**。

## ④ 溯源与设计功能
- 工作单桥：`engine/modelwork.py`（`put_op` 计划相登记、`fill_replies` 回填校验、`status`/`prompts_of` 台账、`get_ready` 齐套判定、`_round_contract` 回合契约、`_run_ntrace` 数字溯源）。工作单文件是 `research/progress/<线名>-MODELWORK.json`，引擎渲染、回填只走命令。
- PENDING 打印与退出：`engine/tree.py` 的 run-pipeline/tension-read 分支（stderr 指引+退出码 2）。设计功能：2026-09-23 起引擎不自带任何模型调用（此前直打计费 API 十天消耗巨大），全部模型意见改为会话侧隔离子代理作答——两拍协议把"引擎算"与"模型答"分开，计划相冻结种子与分组，执行相只消费回复，会话隔多久回填都不影响结果。
- 指纹作废：每条提示词存 SHA 指纹，计划重推后指纹变化，旧回复自动作废（`fill_replies`/`get_ready`）——防拿旧答案配新问题。
- 数字溯源（ntrace）：回复入账前逐数核对——每个数字要么在提示词材料里可见、要么附计算痕迹，否则标记待复核（标记不拦截，按程序开关灰度）。设计功能：防模型在回复里编造数字混入账本。
- 盲读提示词本体：`engine/claim_graph.py` 的读者计划函数（断言内容裸呈、元信息剥离、随机编号、双读者异序）——模型读到的是纯内容，看不到"谁定的案"，这是盲读纪律（读图只见内容）的机械保障。
- 跨通道同源注明：zcode 会话跑同一命令得到同一工作单与同一指引（T-ZC-02 通道）；工作单文件是两侧共用的交接面。

## ⑤ 条件与现状备注
- 生产实例完整覆盖两拍：第一拍 L1220（舰船链实体簇）、中间步 L1395（14 操作台账+读者提示词全文）、第二拍 L1416（回填后盲读完成）。子代理答卷由 DSH 后台子代理产生（生产转录 L1269/L1412 的子代理回传消息实证），答卷临时文件会话末已清理。
- 负情境一：工作单欠答期间，相关门（模型工作单门/待读簇门）在 status 里持续 armed，模型不回填就永远过不去——引擎不会替答、不会超时自动作答。
- 负情境二：`model-work` 台账只列本线的操作；其它会话为同线导出的欠答操作对该会话可见（同一文件），但回复内容互不可见直到入账。
- pwsh 噪声同 T-DSH-04 备注。

---

# T-DSH-06 交付链触发（交付成功后的下一步与材料面注入）

## ① 编号与名称
T-DSH-06：交付链触发——`tree.py deliver` 交付成功后，引擎在同一输出里带回的后续指令与审计材料面（下一步动作、待盲读指引、张力/待读登记、综合到期、未解之题）。

## ② 触发时机
- deliver 通过全部交付门、账本写入 `dossier_delivered` 事件时，一次性输出：结果 JSON+若干条件注入行+尾部的"下一步"行。
- 频次：每条线每次交付一次；交付后引擎自动重建命题图与索引（其完成信息不回显，失败则整条 deliver 失败）。

## ③ 完整上下文
**交付成功输出——生产实例全文**（生产转录 L990，growth-meta3，逐字五行）：
```
提示（W2 叙事模版）：本线研报未从叙事骨架起步——建议先 tree.py narrative 从账本已裁决结论生成骨架再写正文（提示不拦截）
待读：簇 0 个 + 实体粗网 1 个——子代理模式：会话按指引以隔离子代理盲读（产出即读，工作单协议）
待盲读 1 簇（子代理模式）：请以隔离子代理作答并回填——py -3.13 research/engine/tree.py tension-read --program growth --target growth-meta3 导出读者提示词；待读保留，张力门仍拦
{"priced_share": 0.667, "audit_watch": true, "cf": {"killed": 8, "low_potential": 1, "pending_test": 0, "candidate": 1}, "note": "已定价占比>50%：研究增量不足信号，触发审计关注"}
下一步（机械判定，非方向性内容）：监视态：触发器/预言到期看 tree.py status --target growth-meta3
```

**条件块清单与触发条件**（源码生成器逐字）：
1. W2 叙事提示——条件：本线账本从未有 `narrative_skeleton` 事件。文本见上例第一行（提示不拦截）。
2. 综合到期——条件：本线属于某起点组（start-draw 抽的多线并行组）且组内其余线均已交付而母账本尚无综合完成事件：`综合到期：起点组 {mother} 全线交付——母账本已登记综合义务`
3. 张力扫描——条件：程序有维度登记簿且本次交付扫描出新张力：`张力扫描：新登记 {n} 条（{样例}）——未决张力拦后续开线，先四分归路裁决`
4. 待读与产出即盲读——条件：交付点亮了新待读簇/实体粗网：`待读：簇 {n} 个 + 实体粗网 {n} 个——子代理模式：会话按指引以隔离子代理盲读（产出即读，工作单协议）`；自动派发读毕时再附：`产出即盲读：本次读毕 {n} 簇——{维集样例}`；"待盲读 N 簇（子代理模式）"行（生产实例第三行）为自动派发后仍欠答的指引，含具体 tension-read 命令与"待读保留，张力门仍拦"的后果声明。
5. 结果 JSON——恒在：`{"priced_share": {占比}, "audit_watch": {true|false}, "cf": {killed/low_potential/pending_test/candidate 四档计数}, "note": {按 watch 两分支取一}}`；note 两分支：已定价占比>50% 时为"已定价占比>50%：研究增量不足信号，触发审计关注"，否则"纯度合格"。
6. 尾部"下一步"行——恒附（deliver 属变更类命令），见 T-DSH-03 emit_line；交付后通常进入监视态（生产实例末行）。
7. 未解之题注入（交付后回路的材料面）——条件：所属程序存在未了结事项：`未解之题（交付后回路的材料面）：[{kind}] {n}；[{kind}] {n}`（分隔为全角分号；kind 四类：未走之路积压/未决张力/到期未复盘预言/押注校准漂移）。本会话交付时刻该程序暂无未解之题故未出现；同文案在 status（"未解之题 N 类（主动回路的材料面）"，附录 H 第三块生产实例）与 model-work-fill（"未解之题（下一回合的材料面）"）两处同源复用。

**交付后的监视态 status**（附录 H 第二块，生产实例：armed 门 0、按相推进、监视态动作）。

## ④ 溯源与设计功能
- 交付门与尾部注入：`engine/gates.py` 的 `deliver`（W2 提示/综合到期/张力扫描/待读/产出即盲读/JSON）；`engine/tree.py` deliver 分支（未解之题注入、JSON 打印）。设计功能：交付不是终点而是回路起点——引擎把"接下来程序层面欠什么"在交付输出的同屏给足：该盲读的簇（带命令）、该裁决的张力、该综合的起点组、程序层面的未解之题，模型无需再自己找活。
- 未解之题汇总：`engine/pool.py` 的 `open_problems`（忽视积压/未决张力/到期预言/校准漂移四类，纯读侧）。
- 自动派发：`engine/claim_graph.py` 的待读登记与自动盲读派发（交付后引擎自己派读者，派发失败不阻断交付、待读保留）。
- 综合到期：`engine/gates.py` deliver 内检测起点组状态并写母账本研究义务——"发现即义务"在交付链上的落点，防全线交付后综合被遗忘。
- 尾部"下一步"行：与 T-DSH-03 同源（`status.emit_line`）。
- 跨通道同源注明：同一输出在 zcode 通道执行 deliver 时字节相同（T-ZC-02）。

## ⑤ 条件与现状备注
- 生产实例（L990）触发了条件块 1/4/5/6；条件块 2/3/7 本会话未触发，文案为源码取证（确定性打印，推定与生产一致）。
- `audit_watch: true` 的含义：交付纯度里"已定价"占比超过一半，引擎标记为研究增量不足信号供审计关注——不是拦截，是记入交付事件的标记位。
- 负情境：deliver 被拒时（T-DSH-04）以上注入一概不出现——门不过则连 JSON 都没有，交付链完全不通。
- 交付后再动研究（重开节点/新估计）会把相从"已交付"拉回"研究"（status 相判定规则），此时 armed 门重新开始计数。

---

# T-ZC-01 仓库纪律文件（投研根 AGENTS.md）

## ① 编号与名称
T-ZC-01：仓库纪律文件——投研根的 `AGENTS.md`，投研 harness 的硬规则总入口，zcode 会话在投研目录工作时的第一纪律文件。

## ② 触发时机
- zcode 会话（或任何 agent 会话）在投研根或其子目录工作时，按目录层级作为工作区纪律文件生效；zcode 宿主的目录纪律文件机制会自动纳入会话上下文（zcode 侧的注入形态本次未单独留档，见⑤）。
- DSH 侧同源：persona 冷启动指令要求先读它（T-DSH-01），生产转录 L24-L25 实证模型第一轮即读取全文。
- 频次：每会话冷启动一次读入；内容变更后重读才见新文。

## ③ 完整上下文
全文 50 行见**附录 F**（逐字收录）。主要块与触发条件：
- 开篇例外规则：任何动作前按序读完必读清单；唯一例外=用户开口要投研时先 auto-intent 再继续（触发条件：用户开口要投研）。
- 本仓库是什么+铁律：Agent 放大注意力/记忆/计算、不替代判断，verdict 归用户（恒定）。
- 冷启动必读清单五项（HARNESS.md/PROTOCOL.md/READABILITY.md/discipline.py 签名/quant-skill 手册）+ status-first 段（拿不准先跑 status、--interface 机械导出、交叉约束以拒绝文案为准）（恒定）。
- 硬规则速记八条（默认档开工/全仪器强制/发现即义务/几何忽视即义务/盲读纪律/三权分离/合同先行/引擎说了算/研报给人读/最终产出形态/图要多/一切面向人的产出同律/跑一轮只跑一轮/point-in-time/前瞻推演先行）——每条自带其触发场景（如"用户开口要投研""候选触发亮灯或审计发现研究性问题""探索测量判为双尺一致显著空白"）。
- 状态与产出在哪里（索引/书架/活研报/全景/命题图/账本路径）。
- 历史设计文档指路。

## ④ 溯源与设计功能
- 来源：投研根 `AGENTS.md`（仓库文件，用户与引擎会话共同维护的纪律入口）。设计功能：它是投研系统的"宪法速记"——把散在 PROTOCOL.md 的硬约束压成一屏速记，让任何新会话五分钟内知道：什么必须先做（auto-intent）、什么绝不做（代用户决策/静默关闭义务/等数据交卷）、产物给谁看（人）、去哪查状态（status）。每条速记末尾标注设计依据文件名，需要细节时可下钻。
- 与 T-DSH-01 的关系（跨通道同源）：DSH persona 的四条铁律与收工纪律是本文件硬规则的会话层副本——两个通道（预置系统提示/仓库纪律文件）承载同一套纪律，互为冗余。
- 冷启动必读的第 2/3/5 项（PROTOCOL.md 全文、READABILITY 七律、量法手册）是本文件指定的二级长文；PROTOCOL.md 全文 371 行已在本次盘点通读（其 S4 审计议程、S-1 意图路由节分别被 T-ZC-03、T-DSH-02 引用），按 E1 纪律不整篇复制，引用处给出路径。

## ⑤ 条件与现状备注
- 本盘点取证：文件本身（2026-09-30 版本）+ DSH 生产转录读取实例（L24-L25 回显全文开头，内容与盘上文件一致）。
- zcode 侧自动注入形态未单独留档（zcode 会话的工作区纪律加载属宿主机制；本盘点工作目录在插件 worktree，不触发投研根的自动加载）——zcode 会话在投研目录内工作时按 zcode 的 AGENTS 机制生效，此为宿主常规行为，标注"推定"，推断链：zcode 支持 AGENTS.md 目录纪律文件（本盘点会话自身即受 worktree 内 AGENTS 机制约束），文件位于投研根必被该机制覆盖。
- 负情境：会话不在投研目录内且不读该文件时，八条硬规则对模型不可见——只剩 T-DSH-01 的 persona 副本（DSH 侧）或完全不可见（无关会话）。

---

# T-ZC-02 引擎命令输出（zcode 通道 CLI）

## ① 编号与名称
T-ZC-02：引擎命令输出（CLI）——zcode 会话用 Bash 工具执行投研引擎命令得到的输出。
**跨通道同源注明**：与 T-DSH-03/T-DSH-04/T-DSH-05/T-DSH-06 的输出出自同一个 Python 命令行程序（engine/tree.py 等），文本字节同源；差异只在宿主通道（DSH 用 pwsh 工具，zcode 用 Bash 工具）带来的调用包装。

## ② 触发时机
- zcode 会话承担投研工作（或为投研做诊断、盘点）时，用 Bash 执行 `cd <投研根> && py -3.13 research/engine/tree.py <子命令>`。
- 频次：与 DSH 侧同为按需拉取；zcode 侧的投研直驱会话历史上即以此通道运转。

## ③ 完整上下文
**zcode 通道实测一**（本盘点会话于 2026-09-30 实跑，Bash 工具，逐字全文）：
```
== growth-mil-strategy 正向状态（status-first·账本纯函数） ==
相：审计｜档位 deep｜程序 growth
armed 门 1 扇（先解门）：
  - 覆盖清单门：未研究方面 ['十五五纲要装备表述文本比对', '军贸出口需求']
      → tree.py coverage（研究后改 status 或 excluded 带原因）
欠账：rounds_used=10
到期/待结算 3 项（前 5）：
  - [预言未结算] P1｜期限 2027-04-15
  - [预言未结算] P2｜期限 2026-10-31
  - [预言未结算] P3｜期限 2027-05-31
下一步（机械判定）：
  - 解门[覆盖清单门]：tree.py coverage（研究后改 status 或 excluded 带原因）
--json 出机器读数；--interface 出全命令接口（C4 机械导出）
exit=0
```
（与生产转录 L1416 同线同日早些时候的 DSH 输出对照：armed 门从 3 扇变为 1 扇——管线完备门与张力门已被该会话后续动作清掉，同源文本随账本状态变化。）

**zcode 通道实测二**（同日实跑 `--interface`，头部与 auto-intent 节逐字）：已在 T-DSH-03 ③ 引用（"--interface 导出——头部固定四行+auto-intent 节"块），两通道字节一致，不重复贴。

## ④ 溯源与设计功能
- 输出生成：同 T-DSH-03/T-DSH-04（`engine/status.py`、`engine/tree.py` 各分支）。设计功能与 DSH 侧完全一致——引擎只认命令行调用，不关心哪个宿主发起，这保证了双侧会话看到同一套事实与门禁（"引擎共用件，ZCode/DSH 双侧同律"）。
- 通道差异点：zcode 的 Bash 工具输出无 pwsh 的 NativeCommandError 包装，stderr 干净；Windows 下需注意 PYTHONUTF8 与路径反斜杠转义（DSH 生产命令里常见的 `$env:PYTHONUTF8='1'` 在 bash 等价于 `PYTHONUTF8=1 py -3.13 research/engine/tree.py <子命令>`）。
- 取证来源：本盘点会话自身即为 zcode 通道生产实例（实测记录，非转录）。

## ⑤ 条件与现状备注
- 负情境一：zcode 会话没有 engine-status/quant-discipline 技能壳与「全景投研」persona（见 T-DSH-01 备注）——zcode 直驱投研时，引擎用法知识完全依赖读仓库文件（AGENTS.md/PROTOCOL.md）或操作者带入。
- 负情境二：与因子挖掘系统的"CLI 直驱会话对驱动器不可见"同构——投研引擎没有注入器概念，zcode 会话停转即无人接管，一切推进靠会话自己或操作者（投研系统的收工纪律靠 persona/AGENTS 约束，zcode 侧无 persona 副本，约束力更弱）。
- 退出码语义在 bash 下如实显示（1=拒绝、2=工作单欠答），比 pwsh 包装更接近引擎本意。

---

# T-ZC-03 审计议程送达（双环审计的到达方式）

## ① 编号与名称
T-ZC-03：审计议程送达——引擎的随机双环审计如何把"该做审计了+审计做什么"带给会话（zcode 与 DSH 同机制；如实记录：不存在专用的推送通道，议程到达是拉取式的）。

## ② 触发时机
- 研究线签合同时 `audit.py schedule` 抽定随机审计点（quick 1 点、deep 2 点，进度占比截断在 20%-85% 区间）；此后会话每完成一个模块/阶段调 `check --progress <0..1>`，越过抽中点即返回 AUDIT。
- 证伪条件命中时会话主动 `flag`（确定性路径）。
- 交付门前 status 的"随机双环审计门"在未完成时 armed，出路即审计五步命令。
- 频次：每线 quick 1 次 / deep 2 次随机审计，加不定次证伪触发。

## ③ 完整上下文
**check 触发时的返回——生产实例**（生产转录 L1034，两线逐字）：
```
==================== audit growth-naval-target ====================
{"action": "AUDIT", "trigger": "random", "scheduled_at": 0.3819, "note": "随机审计点触发：进入 S4，按固定议程执行（合同核对/修补计数/隐藏假设/三选一）"}
==================== audit growth-mil-strategy ====================
{"action": "AUDIT", "trigger": "random", "scheduled_at": 0.4494, "note": "随机审计点触发：进入 S4，按固定议程执行（合同核对/修补计数/隐藏假设/三选一）"}
```

**check 未触发返回**（源码）：`{"action": "CONTINUE"}`

**确定性证伪触发的 note**（源码）：`确定性证伪触发：合同证伪条件命中，立即进入 S4`

**S4 固定议程全文**（`research/PROTOCOL.md` §1 S4 节原文，逐字；这是审计"做什么"的唯一完整文本，会话以读文件获得）：
```
### S4 双环审计（议程固定，四步缺一不可）
1. **合同核对**：逐条核心预言 vs 已累积证据 → 一致 / 被重新解释（引用证据时间戳）/ 已证伪。
2. **修补计数**：`engine/ledger.py stats --target X` → 自上次审计以来 single_loop_fix 总数，参数级 N₁ / 变量定义级 N₂；N₂>0 → 红旗如实写。
3. **隐藏假设扫描**：执行中默默依赖、框架构建时不存在的假设 → 列表。
4. **三选一**：维持 / 修订 / 纲领质疑（Lakatos 记分复核：连续 2 次审计变量定义级修补且新颖预测兑现 0 → 强制换纲领）。
- 修订 → `ledger.py append --type framework_revision --field dropped='[...]' --field why_least_entrenched=... --field diagnostic=...`（AGM 三问，缺一被拒）+ 新合同版本。
- 完成后 `py -3.13 engine/audit.py resolve --target X --verdict 维持|修订|纲领质疑`，回 S3。
```
（议程中的 `[...]` 与 `...` 为 PROTOCOL 原文的字面示例记号，非本盘点删节。）

**resolve 与 verify-complete——生产实例**（生产转录 L1044，逐字）：
```
==================== resolve growth-naval-target ====================
{"resolved": "random", "remaining": 0}
{"ok": false, "reason": "还有未触发的审计点（先 check --progress 1.0）：[0.5248]"}
==================== resolve growth-mil-strategy ====================
{"resolved": "random", "remaining": 0}
{"ok": false, "reason": "还有未触发的审计点（先 check --progress 1.0）：[0.85]"}
```
（随后 L1049 显示会话按 reason 提示循环 check+resolve 至 verify-complete 通过——`{"ok": true, "revealed": true}`，源码取证。）

**status 里的审计门**（armed 时，生产转录 L1044 后续 status 段逐字）：
```
  - 随机双环审计门：无调度或种子未揭示
      → audit.py schedule → check --progress 1.0 → S4 议程 → resolve → verify-complete
```

**其余 CLI 输出**（源码生成器）：`已调度: {'scheduled': n, 'mode': m}`（schedule）；拒绝形态如 `{target}: 审计未调度（先在 S3 入口调用 schedule）`、`{target}: 审计已调度（范围修订用 rescope，不要重复 schedule）`、Lakatos 记分门拒绝（全文见 T-DSH-04 ③）。

## ④ 溯源与设计功能
- 调度与保密：`engine/audit.py`（schedule/check/flag/resolve/verify_complete/rescope；随机数由本模块持有，种子与抽中值写 `research/audit_state/<线名>.json`——当前 135 个文件，字段为 target/mode/seed/points/flags）。设计功能：审计点是保密的——会话不知道抽在哪个进度，只知道"调度了、deep 两点"；防止会话挑安全进度段应付；`check` 是唯一合法查询接口，种子在 verify-complete 时才写入账本（事后可复现）。
- 议程送达方式（如实记录）：引擎不向 zcode（或 DSH）推送任何审计消息。议程以三种拉取形态到达任何一侧会话：①check 返回的一句 note（指针："进入 S4，按固定议程执行"+议程四步名）；②PROTOCOL.md S4 节全文（会话读文件）；③status 的随机双环审计门出路命令串（含"S4 议程"字样）。zcode 会话与 DSH 会话跑同一 CLI（T-ZC-02 通道），看到同样的三类文本。
- 交付门联动：`engine/gates.py` deliver 检查账本必须有 audit_scheduled 与 audit_schedule_revealed（种子揭示）事件，缺一拒绝（拒绝文案见源码 gates.deliver："随机双环审计未完成（无调度或种子未揭示）"路径）。
- Lakatos 记分门：`engine/audit.py` resolve 的维持分支——连续两个审计窗口变量定义级修补≥2 且新颖预测兑现 0 时拒绝"维持"。

## ⑤ 条件与现状备注
- **负情境（设计而非疏漏）**：`audit_state/` 下的种子与抽中值对模型禁读（PROTOCOL 硬禁令第 1 条；engine-status 技能壳坑清单重申"禁读，读了审计作废"）。模型在 verify-complete 前永远看不到自己会被抽在哪个进度——这是随机审计有效性的前提。
- **负情境**：会话不跑 check 就永远不会触发随机审计；audit 无守护进程、无定时器。深度靠交付门反逼（不完成审计不能交付）。
- zcode 通道无差别：审计命令与输出文本与 DSH 侧逐字同源（同一 audit.py）；不存在"审计只送达 DSH"的机制分叉。
- 生产实例完整覆盖 schedule 前置的 help 查询（L501）、check 触发（L1034）、resolve+verify 循环（L1044/L1049）。

---

# T-ZC-04 复盘触发（程序级复盘的触发与文本）

## ① 编号与名称
T-ZC-04：复盘触发——研究程序级 Lakatos 记分复盘（program-review）什么时候被要求做、要求文本长什么样、做完回什么。

## ② 触发时机
三个触发器任一命中且上次记分快照早于触发点（引擎函数 `program_review_due`，触发间隔常量为 5 条交付）：
1. 交付计数触发：上次快照后程序内新交付 ≥5 条（从未做过快照时：累计交付 ≥5 条即首次到期）；
2. 预言结算触发：上次快照后出现新的预言结算（outcome_resolution）；
3. 纲领质疑触发：上次快照后出现新的纲领质疑裁决。
到期呈现三种：status 的程序复盘门（armed）、新线开表被程序健康门拦（拒绝文案）、以及会话按 AGENTS/PROTOCOL 自觉执行。频次：按交付节奏，约每 5 条交付或每次结算后一次。

## ③ 完整上下文
**触发原因文本**（源码生成器，`{}` 占位）：
- `首次快照到期：已交付 {N} 条线`
- `交付计数触发：上次快照后新交付 {N} 条（≥5）`
- `预言结算触发：上次快照后有 {N} 条结算`
- `纲领质疑触发：上次快照后有 {N} 次质疑裁决`
- 未到期时：`未到期（上次快照后：交付 {N}｜结算 {N}｜质疑 {N}）`（此文本只进 status 详情，不拦路）

**status 程序复盘门**（armed 时，源码）：详情行 `复盘到期：{上述触发原因}`，出路行 `tree.py program-review（记分快照）`。

**拦新线的拒绝文案**（源码 `engine/claim_graph.py` 程序健康门，逐字；触发条件：复盘到期时程序内开新线/孵化）：
```
程序复盘门：{program} 的 Lakatos 记分快照到期——{why}。tree.py program-review --target <程序内任一线>（引擎算记分数字，会话写纲领判断：进步/退化/换纲领）
```

**提交命令的回执**（源码生成器，逐字模板）：
```
程序级记分快照已入账 #{seq}：
  预测 {predictions} 条（兑现 {resolved}）｜参数级修补 {param_fixes}
  ｜变量定义级修补 {vardef_fixes}
  ｜框架修订 {framework_revisions}｜纲领质疑 {program_challenges}
```
条件行：`  ⚠ 退化信号：变量定义级修补 ≥4 且零兑现——考虑换纲领（Lakatos）`（仅当记分命中该条件）。
（模板在源码中为连续三行排版，上示换行为源码字符串内的显式 `\n` 拼接原样。）

**材料面读数**：提交时引擎自动在快照里附加四组报告字段（trail_geometry 轨迹几何三读数/probe_yield 探针产量/active_loop 主动回路 8 读数），纯报告不拦复盘；`pool-status` 命令的人读读数行（源码）：`读数：领取率 {}｜押注校准 {}｜饥饿窗 {}｜不变式 {过|问题清单}`。

**生产实例**（复盘提交的账本留痕——`research/ledger/goldx-cycle.jsonl` 等四文件实查，逐字摘 judgement 字段开头）：
- goldx-cycle #7（2026-09-23T01:03）：`进步——12 线交付把机制层从单点读数进化为可复算资格表与触发器体系；两次方法论自我纠错（利率脱敏窗口化伪象、名义前瞻+0.33 实现事故）是量法纪律生效标志；wave-4 五项预注册判据全部机械可对表且无事后改规则；新颖预测兑现记分在途（`
- goldx-default #6（2026-09-23T01:02）：`进步：33条预言全在窗内0过期、变量定义级修补仅1次、两次框架修订均证据驱动且修订后读数更准、9/16加息对五项预注册判据对表=新颖预测首次事件级验证成功，保留项（结算0条到期/参数修补9次中等/旧键义务待用户裁决）不构成退化`

## ④ 溯源与设计功能
- 触发判定：`engine/claim_graph.py` 的 `program_review_due`（触发器与间隔常量 PROGRAM_REVIEW_INTERVAL=5）。设计功能：程序级复盘原先只有设计要求没有引擎落点，长期零实跑；机制化后"该复盘了"由引擎按账本事实判定，不靠记性。
- 两道呈现：`engine/status.py` gates_armed 的程序复盘门（拉取式提醒）+ `engine/claim_graph.py` program_health_gate 的拦新线（强制式——旧账不清不开新题家族的成员）。
- 提交与快照：`engine/claim_graph.py` 的 `submit_program_review`（引擎统计预言/修补/修订/质疑计数并自动附加几何/探针/主动回路读数；会话只写纲领判断）。回执打印在 `engine/tree.py` 的 program-review 分支。
- 分工设计：引擎算数字、会话写判断（进步/退化/换纲领）、用户握最终裁决权——三权分离在复盘环节的形态。
- 跨通道同源注明：zcode 与 DSH 跑同一命令得到同一回执（T-ZC-02 通道）；复盘没有宿主专属通道。

## ⑤ 条件与现状备注
- 推定标注：本取证会话（生产转录 fe9657b1）未实际执行 program-review 提交（会话内"复盘"仅以"复核义务随下一轮程序复盘执行"的表述出现在豁免理由里，L1421/L1426）；提交回执文本为源码确定性生成，推定与生产一致。触发器、门文案、快照字段均有源码+账本双重取证；goldx 程序的四条生产 program_review 账本条目（2026-09-23/24）证明该命令在生产实跑过。
- 源码函数 docstring 写"无快照 → 首次到期（程序 ≥2 条交付线时）"，而代码实现用间隔常量 5 判首次到期——以代码行为为准（≥5 条），docstring 为旧口径残留，如实登记。
- 负情境：复盘未到期时 status 不出现程序复盘门、开线不被拦——模型看不到任何复盘要求；复盘到期但会话只做本线推进时程序级门同样不拦本线（程序级门拦新线开表，不拦本线推进，见附录 D 坑清单第一条）。

---

# 附录 A：persona 配置原文（cordis.patch.yml，preset-comprehensive-investment）

来源：`~/.dsh/profiles/desktop/cordis.patch.yml`（`~/.dsh/profiles/desktop-diag/cordis.patch.yml` 存同款，两文件仅宿主 UI 版本号行不同）。预置 description 字段（注册信息，不进模型上下文，见 T-DSH-01 ③ 第 5 块）：

```yaml
- id: preset-comprehensive-investment
  config:
    id: comprehensive-investment
    name: 全景投研
    description: >-
      全景投研究会话（comprehensive-investment）：AI 投研 harness 专用——热点扫描到赛道深研到递归子线的完整工作流。
      引擎（Python CLI）是唯一裁判，账本只增不改，verdict 归用户。 会话可在 monorepo 根或投研根启动（2026-09-30
      放宽，DSH 会话常以 git 根=life-os 为 cwd）： 投研根=含 AGENTS.md 与 research/engine/
      的目录；引擎命令一律先 cd 到投研根再执行； 技能根已用 customSkillDirs 绝对补挂（DSH 项目根=git
      根，与投研根不同目录）。 不挂 web：研究数据一律本地管线，结构性背景走本地资料，确需网络由用户旁路会话完成。
    order: 10
    plugins:
```

persona 插件行及其 config.prefix（YAML 折叠标量原文，逐字；`{{model}}`/`{{cwd}}` 为渲染占位符）：

```yaml
      - id: persona
        name: '@deepseek-ai/dsh-persona'
        config:
          prefix: >-
            你是「全景投研」投研会话，由 {{model}} 驱动；当前会话目录 {{cwd}}。投研根目录=含 AGENTS.md 与
            research/engine/ 的目录（本机
            C:/Users/RTyyyy/Desktop/life-os/modules/investment/research/bull-case-anatomy；{{cwd}}
            即它则用当前目录）——bash 每次调用都是新 shell，引擎命令一律先 cd 到投研根目录再执行；冷启动先读该目录
            AGENTS.md 并按其指引行动。
            四条铁律：一、三权分离——买卖与赛道判定（verdict）永远归用户，你负责执行与提议，从不代用户决策；
            二、默认档开工——用户提出研究需求（新开研究线或旧线重启）时，你的第一条回复即是研究动作： 先跑 py -3.13
            research/engine/tree.py auto-intent --target <线名> 默认登记（档位恒深解剖；
            线名有既有程序全景归属→程序循环，无→单线），免前置问答轮；新对象无程序全景时，当轮用 ask_user_question
            原文转述引擎标准文案（auto-intent --show-d1 取文案，选项不可改写）问 (A) 单线深解剖 / (B)
            先建全景再研究，无答复按 (A) 继续不阻塞；疑问一律当轮内联问，
            不增加对话轮次；伪造用户答复=硬禁令违规；用户主动点名"快扫/标准/非共识专项"时走显式 intent-chat 通道尊重之；
            三、研报给人读——一切面向人的产出：30 秒总述开头、短句、来源段末、过程代称不出正文。
            四、前瞻推演先行——结论必须基于当下已知基本面完成对未来的完整推导（情形×各情形幅度×时点窗×辨别证据）；
            「等数据」只是给已注册推导打分的校准点：触发器亮灯=对答案，不许当结论、延期理由或替代推导；
            挂起（不可知/递延/等触发）前必须先落情形条件推演（各情形下该读数应是多少、什么读数杀死什么情形）； 「季度未结束」不构成不可知。
            收工纪律（与铁律同级）：研究工作一路推进到无事可做才收工——收工判据=引擎 status
            对相关线报『无下一步合法动作』或交付完成；每个变更类命令输出末尾的『下一步』提示
            就是待办队列，逐条清空；阶段战报发完就地继续，不等待指示、不问「要不要继续」。
            仅三种情况允许中途停：①三权分离事项等用户裁决；②不可逆或付费操作需确认； ③用户明确限定范围的即答任务（如单份研报整理）。
            引擎是唯一裁判：py -3.13 research/engine/tree.py 的门禁与审计说了算，账本只增不改； 冷启动先读仓库
            AGENTS.md 并按其指引行动。 拿不准下一步：py -3.13 research/engine/tree.py status
            --target <线名>（正向状态视图——相/armed 门带出路/ 欠账/到期读数/下一步合法动作；--json
            机器读）；全命令接口：py -3.13 research/engine/tree.py --interface。
```

技能根补挂行（同预置 plugins 内）：

```yaml
      - id: skill-filesystem
        name: '@deepseek-ai/dsh-skill-filesystem'
        config:
          customSkillDirs:
            - >-
              C:/Users/RTyyyy/Desktop/life-os/modules/investment/research/bull-case-anatomy/.agents/skills
```

# 附录 B：persona 生产渲染版（DSH 系统消息，生产转录逐字）

来源：生产转录 system/message 事件。占位符已由宿主替换：`{{model}}`→`glm-5.3-flash`；`{{cwd}}`→会话目录。

**第 1 轮渲染（L9，更新前——无收工纪律段）**：

```
You are an AI agent powered by DeepSeek Harness.

你是「全景投研」投研会话，由 glm-5.3-flash 驱动；当前会话目录 C:\Users\RTyyyy\Desktop\life-os。投研根目录=含 AGENTS.md 与 research/engine/ 的目录（本机 C:/Users/RTyyyy/Desktop/life-os/modules/investment/research/bull-case-anatomy；C:\Users\RTyyyy\Desktop\life-os 即它则用当前目录）——bash 每次调用都是新 shell，引擎命令一律先 cd 到投研根目录再执行；冷启动先读该目录 AGENTS.md 并按其指引行动。 四条铁律：一、三权分离——买卖与赛道判定（verdict）永远归用户，你负责执行与提议，从不代用户决策； 二、默认档开工——用户提出研究需求（新开研究线或旧线重启）时，你的第一条回复即是研究动作： 先跑 py -3.13 research/engine/tree.py auto-intent --target <线名> 默认登记（档位恒深解剖； 线名有既有程序全景归属→程序循环，无→单线），免前置问答轮；新对象无程序全景时，当轮用 ask_user_question 原文转述引擎标准文案（auto-intent --show-d1 取文案，选项不可改写）问 (A) 单线深解剖 / (B) 先建全景再研究，无答复按 (A) 继续不阻塞；疑问一律当轮内联问， 不增加对话轮次；伪造用户答复=硬禁令违规；用户主动点名"快扫/标准/非共识专项"时走显式 intent-chat 通道尊重之； 三、研报给人读——一切面向人的产出：30 秒总述开头、短句、来源段末、过程代称不出正文。 四、前瞻推演先行——结论必须基于当下已知基本面完成对未来的完整推导（情形×各情形幅度×时点窗×辨别证据）； 「等数据」只是给已注册推导打分的校准点：触发器亮灯=对答案，不许当结论、延期理由或替代推导； 挂起（不可知/递延/等触发）前必须先落情形条件推演（各情形下该读数应是多少、什么读数杀死什么情形）； 「季度未结束」不构成不可知。 引擎是唯一裁判：py -3.13 research/engine/tree.py 的门禁与审计说了算，账本只增不改； 冷启动先读仓库 AGENTS.md 并按其指引行动。 拿不准下一步：py -3.13 research/engine/tree.py status --target <线名>（正向状态视图——相/armed 门带出路/ 欠账/到期读数/下一步合法动作；--json 机器读）；全命令接口：py -3.13 research/engine/tree.py --interface。
```

（其后接宿主通用段：@路径规则、退出码规则、read/glob/grep 指引、后台任务规则等，属宿主文本，不在本附录复制。）

**第 6 轮渲染（L581，更新后——新增收工纪律段，位于"「季度未结束」不构成不可知。"与"引擎是唯一裁判"之间）**：

```
收工纪律（与铁律同级）：研究工作一路推进到无事可做才收工——收工判据=引擎 status 对相关线报『无下一步合法动作』或交付完成；每个变更类命令输出末尾的『下一步』提示 就是待办队列，逐条清空；阶段战报发完就地继续，不等待指示、不问「要不要继续」。 仅三种情况允许中途停：①三权分离事项等用户裁决；②不可逆或付费操作需确认； ③用户明确限定范围的即答任务（如单份研报整理）。
```

（第 6 轮渲染的其余部分与第 1 轮逐字一致。）

# 附录 C：monorepo 根 AGENTS.md（投研会话重定向桩）全文

来源：`C:/Users/RTyyyy/Desktop/life-os/AGENTS.md`（DSH 会话目录在 life-os 根时由宿主注入，生产转录 L11 实证）。

```markdown
# AGENTS.md — life-os monorepo 根（投研会话重定向桩）

本目录是 monorepo 的 git 根；DSH / ZCode 会话常以本目录为 cwd。本文件只为投研会话指路，不承载模块自身规则。

## 投研会话（「全景投研」preset / bull-case-anatomy 相关工作）

投研 harness 根目录在 **`modules/investment/research/bull-case-anatomy/`**（含 `AGENTS.md` 与 `research/engine/`）。投研会话必须：

1. 先读 `modules/investment/research/bull-case-anatomy/AGENTS.md`（硬规则总入口：默认档开工、合同先行、门禁、status-first 等），一切以其为准；
2. 引擎命令一律先 `cd modules/investment/research/bull-case-anatomy` 再执行（如 `py -3.13 research/engine/tree.py status --target <线名>`）——shell 每次调用都是新进程，`cd` 不跨调用保持。

## 其余模块

wiki / core / projects（含 dsh-factor-mining-plugin）等按各自目录内的说明文件工作，与本桩无涉。
```

# 附录 D：engine-status 技能壳全文

来源：`bull-case-anatomy/.agents/skills/engine-status/SKILL.md`（DSH 侧经 customSkillDirs 挂载；生产转录 L60 调用回显一致）。

```markdown
---
name: engine-status
description: 引擎正向状态视图（status-first）。当用户或会话需要知道某条研究线「现在处于什么状态、下一步能做什么合法动作」、被引擎拒绝后不知道出路、或冷启动不想读引擎源码找接口时使用。命令：tree.py status / --interface。
---

# engine-status —— 拿不准先跑 status

## 何时用

- 开工/接手一条线，不知道当前该做什么 → `status`
- 被引擎拒绝，想看全部 armed 门与各自出路（不是逐门试错）→ `status`
- 不知道某命令吃什么参数 → `--interface`（机械导出，不读源码）
- 脚本/程序消费状态 → `status --json`

## 快规格（两条命令）

```bash
cd <仓库根>/bull-case-anatomy
py -3.13 research/engine/tree.py status --target <线名>          # 人读摘要
py -3.13 research/engine/tree.py status --target <线名> --json   # 机器读
py -3.13 research/engine/tree.py --interface                     # 全命令接口导出
```

status 输出五件：**相**（未开表｜研究｜审计｜已交付）／**armed 门**（此刻会拦的门＋每门出路命令）／**欠账**（义务/蒸馏/工作单/张力/簇计数）／**到期读数**（未结算预言/递延触发器/停放问题）／**下一步**（机械判定的合法动作，≤5 条）。

--interface 交叉约束（字段间依赖、条件必填）不导出——**以各门拒绝文案为准**（拒绝即教学）。

## 慢协议（status-first 纪律）

1. **status 是账本的纯函数**：只聚合既有门的输入函数，不新增判断、不写任何账。无守护进程，每次调用从账本重建——并行会话安全。
2. **nextStep 只机械判定**：armed 门→先解门（前 3 扇）；无门→按相报该相的合法动作集。**引擎不产生任何研究方向性内容**（三权分离：建议/优先/值得/应该类词禁入生成器文案——出现即引擎 bug，报工程会话）。
3. **成功响应也说话**：变更类命令成功后输出末尾自动附一行下一步（轻量档：线级门 only；完整视图用 status）。
4. **先 status 后动作**：撞门才知道的年代结束了——查询即引导，但不代替判断：下一步的「研究什么」永远归会话与用户。

## 生命周期 × 原语地图（按相）

| 相 | 机械判定 | 常用原语 |
|---|---|---|
| 未开表 | 无 intent_declared | `auto-intent`（默认档登记；D1 无全景对象 A/B 两选项）→ `frame-argument` |
| 研究 | 有意图、主干未全定案 | `register` / `estimate` / `crosscheck` / `probe` / `model-spec` / `frame-review`（主节点定案后强制） |
| 审计 | 主干全定案、未交付 | `audit.py schedule→check→resolve→verify-complete` / `tree-review --verdict close` / `run-pipeline` / `deliver` |
| 已交付 | 有交付事件（后再开=研究） | `status`（监视态：triggers_due）→ 触发器亮灯按预注册推导对答案 / `reopen` |

armed 门全集（status 聚合）：意图门/意图授权门/主干定案门/元反思门/覆盖清单门/未决问题门/反事实批次门/随机双环审计门/研究合同门/建模下限门/建模评审门/构造量门/管线完备门/形态不变性门/蒸馏门/机制地图门/树级评审门/终态完备门/量纲门/义务门（本线+程序）/张力门/待读簇门/反共识老化门/过期预言门/程序复盘门/几何忽视门/模型工作单门。

## 坑清单精华（会话记忆实证）

- 程序级门（张力/簇/义务/几何）拦的是**新线开表**，不拦本线推进——status 里看到程序级 armed 门而你在做本线工作时不阻塞。
- `run-pipeline`/`tension-read` 停在 PENDING 不是错误：按 `model-work --prompts` 看提示词，隔离子代理作答后 `--replies-file` 回填续推（引擎不自带模型调用）。
- 工作单回填后看 `[数字溯源]` 行：来历不明数字已标待复核——逐个核对材料原文或补 python 复算痕迹（台账在 `<线>-MODELWORK.json` 的 ntrace 节）。
- `reopen-check` 已正名 `random-reopen`（行为=防御性随机重开执行器，会真的重开节点入账——不是查看器）。
- 双环审计种子在 `audit_state/` **禁读**（读了审计作废）；一切状态以账本为准。
```

# 附录 E：quant-discipline 技能壳（薄壳）全文

来源：`bull-case-anatomy/.agents/skills/quant-discipline/SKILL.md`。

```markdown
---
name: quant-discipline
description: 研究线量化建模的量法纪律手册（三书 245 条带页码锚+纪律库+失败案例库）。当用户要求在研究线内做回归/估计/预测评估/regime 分析/协整/事件研究/弹性与因果识别/校准/波动建模等量化建模工作，或引擎拒绝消息指名"quant-discipline §S*"时使用。因子挖掘用 factor-mining、策略构造用 strategy-lab，三者分界见入口层第一问。
disable-model-invocation: false
user-invocable: true
---

# 量法纪律（薄壳·发现入口）

本文件是装载薄壳（B 通道）。**权威全文在本仓库**：
`research/papers/quant-skill-2026-09/SKILL.md`——含入口层三问路由（角色/层判断→
问题形态→12 槽位→特质义务门）、参考层（六律/纪律库八函数手册/失败案例库 CASES.md/
槽位手册 slots/S0-S11）、接线层（引擎拒绝指名章节）。冲突一律以权威文件为准。

建模前三步（快照）：
1. **层判断**：产出是结论/预言→研究线建模（本技能）；截面排序因子→factor-mining；
   组合策略→strategy-lab。
2. **落槽**：问题形态对应 S0-S11 至少一槽（详见权威文件第二问的映射表）。
3. **对门**：特质→义务（预测性→前瞻检验；相对性→对照组；弹性凸性→解析恒等式先行；
   时变敏感→分 regime；新构造量→范围声明）；回归必带子样本表（ols_nw）；
   水平值回归先过协整门（eg_coint）。

会话在投研仓库根启动时，直接读权威文件（一层相对路径，无网络依赖）。
```

# 附录 F：投研根 AGENTS.md 全文（T-ZC-01 / T-DSH-01 冷启动目标）

来源：`C:/Users/RTyyyy/Desktop/life-os/modules/investment/research/bull-case-anatomy/AGENTS.md`（2026-09-30 盘上版本；生产转录 L25 读取回显与之一致）。

```markdown
# AGENTS.md — bull-case-anatomy 投研 harness（新会话自动入口）

> 本文件会被 agent harness 自动加载。你在本项目做任何事之前，按下面的顺序读完再动手——唯一例外：用户开口要投研时，先 `auto-intent` 默认登记再继续（免前置问答轮，见硬规则速记第一条）。

## 本仓库是什么

AI 投研 harness：从热点扫描到赛道深研到递归子线的完整投研工作流。
**铁律：Agent 放大注意力、记忆、计算——不替代判断。所有 verdict（买卖决策、赛道判定）归用户。**

## 冷启动必读（按序，缺一不可）

1. `HARNESS.md` —— 项目入口与总览
2. `research/PROTOCOL.md` —— **硬约束全文**（合同先行 S1.5、树引擎门禁、审计、隔离评审、可读性交付门、全景维护边界）
3. `research/engine/READABILITY.md` —— 研报写作七律（deliver 门禁强制：过程代称/账本行号/脚本名入正文=拦截）
4. `research/quant/discipline.py` —— 量法纪律库签名（ols_nw 子样本表必填、forward_information_test 基准必填）
5. `research/papers/quant-skill-2026-09/SKILL.md` —— **量法纪律手册（quant-discipline 技能·A 通道必读引用）**：任何研究线量化建模（回归/估计/预测评估/regime/协整/事件研究/弹性因果/校准/波动）先过其入口层三问路由（层判断→落槽 S0-S11→特质义务门）；引擎拒绝消息指名 §S* 时即查此手册；因子层用 factor-mining、策略层用 strategy-lab，分界在手册第一问。B 通道发现壳在 `.agents/skills/quant-discipline/`

**拿不准先跑 status（2026-09-28 任务书 N1·status-first）**：对任何线不知道下一步合法动作时，先 `py -3.13 research/engine/tree.py status --target <线名>`（正向状态视图：相/armed 门/欠账/到期读数/下一步合法动作，`--json` 机器读）——不再读源码找接口；全命令接口机械导出 `py -3.13 research/engine/tree.py --interface`（argparse 内省＋账本字段，交叉约束以拒绝文案为准）。变更类命令成功输出末尾自动附下一步（同源机械判定，非研究方向）。技能壳：`.agents/skills/engine-status/`。

## 硬规则速记（详情以 PROTOCOL 为准）

- **开工免问，默认档开工**（2026-09-22 用户指令：开工提问机制减重，任务书 INTENT_LIGHTENING_MANDATE.md）：用户开口要投研（新开线或旧线重启），你的**第一条回复即是研究动作**——先跑 `py -3.13 research/engine/tree.py auto-intent --target <线名>`（仓库根目录 bull-case-anatomy 下）默认登记：档位恒 **deep（全景投研·深解剖）**；线名有既有程序全景归属→program-cycle，无→single；常设授权注记入链（auto_default），免前置问答轮、不增加对话轮次。**D1 无程序全景的新对象**：当轮用宿主自带提问工具原文转述引擎标准文案（`auto-intent --target <线名> --show-d1` 取文案，选项措辞不得改写）给两选项 **(A) 单线深解剖 / (B) 先建全景再研究**；无答复不阻塞：按 (A) 继续；答 (B) 则先走全景建档流程再研究（其后程序线重登 program-cycle）；已提问用 `--asked`、答复用 `--d1-choice A|B` 入链。**伪造提问工具的用户答复=硬禁令违规**；auto_default 字段只能由 auto-intent 原语写入（直写账本伪造=审计违规）。**显式通道保留**：用户主动说"快扫/标准"时经 `intent-chat --tier quick|standard` 尊重显式档位、门禁下限随档位（D2）；cf-only 永不默认、永不提问，仅任务书/用户明确指定（D3）；旧提问通道（intent-chat 逐字原话 / 终端 intent 确认码 / --via-ask-tool 轻通道）保留为兼容径（D4），交付门四路授权都认（tty / chat 原话 / via_ask_tool / auto_default）。已登记过意图的线 auto-intent 不覆盖；任务书自主开线（mandate_ref）与子线继承（inherited_from）语义不变。
- **全仪器强制**（2026-09-16 用户指令："我要求所有机制都是强制性要经历的"）：无偏管线全部仪器（组合读数/注意力诊断/冷落法庭重喂/对照电池/承重分析与承重格摧毁/处置清零/继承声明）是交付门禁强制项——交付前必跑 `py -3.13 research/engine/tree.py unbias-audit --target <线名>`（欠账非零即拒）；全量跑批走 `run-pipeline`；跳步唯一合法出口=`pipeline-exempt` 带理由入账（注意力诊断零成本不可豁免）。**子代理模式（2026-09-23 用户指令）**：引擎不自带模型调用——run-pipeline/tension-read 停在 PENDING 时按 `model-work --prompts` 指引，逐条以隔离子代理作答、`--replies-file` 回填续推至 DONE；不许为省事改走 API 派发。
- **发现即义务**（2026-09-16 用户指令）：候选触发亮灯或审计发现研究性问题时，引擎登记 research_obligation——出路仅三条：开线解决（spawn-sub 带 obligation_seq）/带触发器递延/被既有线吸收；**回扫来源义务另有一条**：补审清偿（`settle-obligation` 带证据锚 `<线>#<seq>`，引擎验证锚在账/晚于义务/面貌与文书吻合后闭链——2026-09-28 引擎问题清单第 8 项修复，agent 不自证清偿）；程序内有未决义务时新线开表与孵化被门拒绝，义务所属线的交付被拦。不许静默关闭。
- **几何忽视即义务**（2026-09-22 几何阶段C，GEOMETRY_PHASE_C_MANDATE.md）：探索测量判为**双尺一致显著空白**的对象由 `tree.py probe-gen` 生成强制研究探针（三出口：翻出东西开线/不可知+触发器/不值得≥20字理由——后两者同样是完成；强制的是到场与处置不是产出，探针结论与自发研究同门禁）；程序 z(均匀)>2 且最大空白对象未处置时几何忽视门拦新线；每周期≤3、占程序未决义务≤30%、命中率进复盘第四读数；`tree.py r2-build` 维护语义坐标缓存（双尺一致性前提）；`probe-status` 看台账。
- **盲读纪律：频率解除，真读不降**（2026-09-23 用户指令：时间闸撤除+prompt 加强）：频率门已整体撤除——手动盲读不限时不限量，可连续批量清积压；**解除的是节奏不是义务**——每簇仍须逐条真读：张力提案高召回（宁可多提、由后续裁决淘汰，不许图省事少提或不提），无张力的对也要报备考察过（负空间不许静默跳过）；「读了没提案」的空转会在审计里显形（cluster_read.proposals 对照张力提案量）=违规。**节点定案门**：该有的审计与盲读是每个节点的义务——节点定案前，本线维度相交的待读簇未清、或已落结论的格子欠组合读数/摧毁检验，定案即被拒（解锁=做掉欠账，不是等待也不是攒批）。
- **三权分离**：decisions=用户 / execution=引擎 / proposals=AI。`verdict.py` 的录入权在用户。
- **合同先行**：预注册预言签署后才许取证（S1.5）；「显著」必须带 deflated p。
- **引擎说了算**：`tree.py deliver` 自动跑合规+可读性门禁，错误即拒绝交付；`registry.py build` 渲染 INDEX/QUEUE，勿手改。
- **研报给人读**：30 秒总述、短句、来源段末、过程代称出正文、强度标签集中。
- **最终产出形态**：每条线交付后，registry 自动生成渲染版 HTML（研报/轨迹/总览）与程序思维导图——这是所有输出的最终阅读形态；md 源文件是创作层。
- **图要多**：研报结论优先用图直观承载——图数量下限 quick≥2/standard≥3/deep≥4（compliance 错误级），引用图必须真实存在；程序全景含引擎渲染的树状图/思维导图（registry build 自动生成，节点可点击跳转）。**生图六要求（2026-09-17 用户指令，一切面向人的图同律）**：①节点+边网络结构（不做列式排版）②节点不遮挡（碰撞约束+缩放兜底）③白话标签（产品经理式，行话进悬停）④数字必须带单位且来源可辨（计算锚 vs 模型判断，不许裸示）⑤点击跳渲染版 HTML 非 raw text ⑥滚轮缩放/拖拽平移；交付前真实浏览器渲染检查。
- **一切面向人的产出同律**（2026-09-07 用户指令）：研报之外，trail、程序全景/章程、规则卡、verdict note、commit message、对用户的汇报——全部执行 READABILITY 原则（自足可读、机制语言、无过程记号）。机器格式（账本 JSONL/渲染物）豁免。registry build 自查 INDEX_NOTES 的过程记号并警告。
- **"跑一轮"= 只跑一轮**：验收汇报即止。全景总览与新树判断仅用户要求时执行（唯一例外：猪周期程序自动循环，见 `research/dossiers/pig/pig-program-charter.md` 第四阶段）。
- **point-in-time 纪律**：`methodology.md`——t 时点不可得的数据不得用于 t 时点判断。
- **前瞻推演先行，禁止等数据交卷**（2026-09-22 用户指令："agent 在等数据确认反转已经发生；投研要求基于已知基本面推导未来"）：结论必须基于当下已知基本面完成对未来的完整推导——情形×各情形幅度（数值+单位+带宽）×时点窗×辨别证据，交付时推演须已在账；「等数据」唯一合法角色=给已注册推导打分的校准点（触发器亮灯=对答案，不是从零开始想）；一切挂起类出口（不可知/递延/等触发）前必须先落情形条件推演（各情形下该读数应是多少、什么读数杀死什么情形）；「季度未结束」不构成不可知。point-in-time 禁前视、不禁前瞻——用 t 时点可得信息推导 t+1 正是研究本体。

## 状态与产出在哪里

- 深研线索引（引擎渲染）：`research/INDEX.md`（按程序分组：猪周期/黄金/赛道/其它）
- 研报书架（人类导航+阅读顺序）：`research/dossiers/README.md`
- **活研报（与全景图一同维护的最终研报·引擎渲染勿手改）**：`research/dossiers/<程序>/<程序>-living-report.md.html`——每次图重建自动同步再生（2026-09-17 用户指令）；含结论总表（白话+单位+带+来源线）/触发器/未走之路/过时陈述警告；**自配叙事+产出更新栏目（2026-09-23 用户指令）**：无模板程序由引擎按最新数据自动成文（30 秒总述+分维度叙事），每版报告专设「相比上一版的产出更新」——成文叙述（非干列表，产品经理式逐条说）+来源线链接到渲染版研报（<线>.md.html，绝不链未渲染 raw）；要求=量化先行+公式推导+白话+单位+来源+图（用户全部研报要求）
- 程序全景与循环状态：`research/dossiers/pig/pig-program-panorama.md` + `pig-program-charter.md`
- 程序命题图（全局记忆·引擎渲染）：`research/graph/<程序>claims.json` + `<程序>CLAIMS.md`——各线结论/开放项/队列的单一真相源，交付时自动重建（2026-09-16 写侧入链），勿手改；开线/选题/对照前先投影，不从单线视角出发；**读侧 v1（2026-09-17）**：模型读图一律盲读（只见内容，元信息不可见）；开线站位/张力裁决走 tree.py（grid positions、tension-adjudicate 四分归路）；人类视图=<程序>-claims-map.html
- 赛道判定状态：`supply-chain/state/track_verdicts.json`、`candidates.json`
- 研究账本（append-only+哈希链）：`research/ledger/*.jsonl`（勿改写历史）

## 历史设计文档

根目录各 `*_DESIGN.md`/`*_SURVEY.md` 是机制的设计依据（改动机制前先读对应文档）；过程日志为根目录 `log.md`。候选判定工具在 `supply-chain/verdict.py`。
```

# 附录 G：意图路由全套标准文案（engine/intent.py 源码取证）

**常设授权注记**（auto-intent 登记事件的 user_quote，生产账本逐字）：
```
常设授权（2026-09-22 用户指令：开工提问机制减重——已有的提问与选择、强制用户原话机制太重，去掉选项，默认全景投研·深解剖，免前置问答轮；任务书 INTENT_LIGHTENING_MANDATE.md）
```

**D1 标准提问文案**（`d1_question_text`）：
```
=== 研究线 {target} 开工确认（引擎标准文案，原文转述、选项不可改写）===
本对象暂无程序全景图，请选择开工方式：
(A) 单线深解剖——基准+单次深解剖直接研究本对象；
(B) 先建全景再研究——先走程序全景建档流程，其后本程序线按程序循环展开。
无答复不阻塞：按 (A) 继续。
```

**旧通道开工提问文案**（`intent_question`，会话路径/`intent --show` 取用）：
```
=== 研究线 {target} 开工提问（引擎标准文案，原文转述、选项不可改写）===
意图三选一：1=基准+单次（缺省方向）  2=program-cycle（递归投研）  3=cf-only（非共识专项）
档位：q=快扫  s=标准  d=深解剖
说明：选 2 须指明程序名（须已有全景图，如 muyuan）并附 ≥20 字一句话确认；选 3 须指明已交付基线名并附 ≥10 字确认；选 1 确认可附可不附。你的回答将逐字入链作为授权原文。
```

**intent-chat 登记回执**（源码生成器）：
```
已登记 #{seq}: {intent}（档位 {tier}，{会话路径|ask-user 轻通道（原话见宿主会话日志）}）——回执须原文转述给用户；逐字通道的 user_quote 已入链，伪造用户原话=硬禁令违规
```

**生产账本开题两行**（`ledger/growth-naval-target.jsonl` 第 1、2 条，逐字；哈希链字段保留）：
```
{"seq": 1, "ts": "2026-09-30T01:16:29+08:00", "type": "intent_declared", "intent": "program-cycle", "user_quote": "常设授权（2026-09-22 用户指令：开工提问机制减重——已有的提问与选择、强制用户原话机制太重，去掉选项，默认全景投研·深解剖，免前置问答轮；任务书 INTENT_LIGHTENING_MANDATE.md）", "program": "growth", "auto_default": true, "hash": "e45face2b957ac1b11e4f03468bf1a3e0bd9f3ac25c2d1fc566d9a57967f192e", "prev": "GENESIS"}
{"seq": 2, "ts": "2026-09-30T01:16:29+08:00", "type": "research_started", "mode": "deep", "hash": "9cab9cdef3b64e39ac2356908d5bde3eb0c2bc061dde550ab25599467a2637b7", "prev": "e45face2b957ac1b11e4f03468bf1a3e0bd9f3ac25c2d1fc566d9a57967f192e"}
```

# 附录 H：status 生产实例全集（生产转录逐字）

**实例一**（L67，研究相，6 门 armed）：全文已录于 T-DSH-03 ③。

**实例二**（L1416，交付后监视态+程序级门，growth-meta3 段逐字）：
```
== growth-meta3 正向状态（status-first·账本纯函数） ==
相：已交付｜档位 deep｜程序 growth
armed 门 1 扇（先解门）：
  - 张力门：3 条未决张力拦新线
      → tree.py tension-adjudicate（四分归路裁决）
欠账：program_tensions=3，rounds_used=12
未解之题 1 类（主动回路的材料面）：
  - [未决张力] 3｜如 #196 [双读者收敛] 8.0 恰为 [1,8] 上端点且中位 4.5 暗示存者近 1 
下一步（机械判定）：
  - 解门[张力门]：tree.py tension-adjudicate（四分归路裁决）
--json 出机器读数；--interface 出全命令接口（C4 机械导出）
```
（"存者近 1 "后的空格为转录原样。）

**实例三**（L1416 续，growth-naval-target 段逐字，含到期读数块）：
```
== growth-naval-target 正向状态（status-first·账本纯函数） ==
相：已交付｜档位 deep｜程序 growth
armed 门 2 扇（先解门）：
  - 管线完备门：1 项仪器未跑（如 锚豁免未复核：裁决 [87, 88, 89] 使用 anchor_exempt ）
      → tree.py run-pipeline --target growth-naval-target
      → tree.py pipeline-exempt <仪器> --reason <理由>（注意力诊断不可豁免）
  - 张力门：3 条未决张力拦新线
      → tree.py tension-adjudicate（四分归路裁决）
欠账：program_tensions=3，rounds_used=10
到期/待结算 3 项（前 5）：
  - [预言未结算] P1｜期限 2026-10-31
  - [预言未结算] P2｜期限 2026-10-31
  - [预言未结算] P3｜期限 2026-12-31
未解之题 1 类（主动回路的材料面）：
```
（转录该块在"未解之题"行后被下一段输出覆盖，如上原样；同命令下一线 growth-mil-strategy 段见 T-ZC-02 实测对照。）

# 附录 I：工作单两拍协议文档（research/MECHANISM_GUIDE.md 子代理模式节原文）

```
### 子代理模式（2026-09-23 用户指令：全部走子代理）

引擎不再自带任何模型调用——此前组合读数/摧毁重放/对照电池/盲读经派发器直打
开放平台 API（按量计费=用户 API 额度），现全部改走会话隔离子代理（coding plan 额度）。
两拍工作单协议：

```
第一拍  py -3.13 engine/tree.py run-pipeline --target X --plan-file p.json
        （或 tension-read）→ 引擎算到需要模型意见处导出提示词 → PENDING
会话    model-work --target X --prompts 看欠答提示词
      → 每条提示词派一个隔离子代理作答 → 写回复 JSON
第二拍  run-pipeline ... --replies-file 回复.json → 引擎消费回复续算
      → 还有欠答再停，循环至 DONE（计划冻结，幂等可重入）
```

- 语义嵌入无子代理通道（宿主模型产不出向量）——`r2-build` 默认拒绝，`--via-api` 显式授权才走 API（embedding-3 单价低）
- 派发器（dispatch.py）仅存人工显式路径：CLI 手动 run/probe
- 成本审计照旧：usage.jsonl 只会再记人工显式调用；子代理消耗在 coding plan 侧不可见于此账
```

# 附录 J：审计 CLI 输出文案总表（engine/audit.py 源码取证+生产实例）

| 命令 | 成功输出 | 拒绝输出（示例） |
|---|---|---|
| schedule | `已调度: {'scheduled': N, 'mode': 'deep'}` | `{target}: 审计已调度（范围修订用 rescope，不要重复 schedule）` |
| check | `{"action": "CONTINUE"}` 或 `{"action": "AUDIT", "trigger": "random", "scheduled_at": 0.3819, "note": "随机审计点触发：进入 S4，按固定议程执行（合同核对/修补计数/隐藏假设/三选一）"}`（生产 L1034 逐字） | `{target}: 审计未调度（先在 S3 入口调用 schedule）` |
| flag | `{"action": "AUDIT", "trigger": "deterministic", "condition": "{条件}", "note": "确定性证伪触发：合同证伪条件命中，立即进入 S4"}` | 同上 |
| resolve | `{"resolved": "random", "remaining": 0}`（生产 L1044 逐字） | Lakatos 记分门拒绝（全文见 T-DSH-04 ③） |
| verify-complete | `{"ok": true, "revealed": true}` | `{"ok": false, "reason": "还有未触发的审计点（先 check --progress 1.0）：[0.5248]"}`（生产 L1044 逐字）/ `{"ok": false, "reason": "已触发未决议：N 个随机点 / N 个证伪"}` |
| rescope | `{"redrew": N}` | — |

# 附录 K：复盘触发与提交文案总表（源码取证）

- 触发原因文本五则：见 T-ZC-04 ③。
- status 程序复盘门：`复盘到期：{why}` / 出路 `tree.py program-review（记分快照）`。
- 程序健康门拒绝：见 T-ZC-04 ③。
- 提交回执两段+条件警告行：见 T-ZC-04 ③。
- pool-status 读数行：`读数：领取率 {}｜押注校准 {}｜饥饿窗 {}｜不变式 {过|problems}`。
- 生产 program_review 账本条目四例：goldx-cycle #7（2026-09-23T01:03）、goldx-cycle #1002（2026-09-24T13:29）、goldx-default #6（2026-09-23T01:02）、goldx-long #6（2026-09-24T15:16）——judgment 字段均以"进步"开头的纲领判断全文入账（两例头段逐字见 T-ZC-04 ③）。

---

## 盘点自验记录（2026-09-30）

1. **引语逐字抽查三处**（对照生产转录解压文本/源码/账本原件）：
   - 抽查一：T-DSH-02 生产回执"已登记 #1: program-cycle（growth）（默认档 deep，auto_default 常设授权）——第一条回复即研究动作，不阻塞" ↔ 生产转录 L332 tool/result —— 零出入。
   - 抽查二：T-DSH-04 隔离门拒绝 ↔ 生产转录 L609 —— 零出入（含引擎原文的"…"占位符）。
   - 抽查三：T-DSH-05 model-work 台账与读者提示词全文（2334 字符）↔ 生产转录 L1395 —— 零出入。
2. **无删节检查**：全文无本盘点自造的省略号与"略"；引文内出现的"…"均为引擎/协议原文的字面内容并已在正文注明。
3. **骨架勾稽**：E2 投研节 10 卡齐（T-DSH-01..06、T-ZC-01..04），负情境在各卡⑤立注。
