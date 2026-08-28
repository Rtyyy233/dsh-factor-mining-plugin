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
2. 写 `factor(env)` 源码；只用过去数据（`shift(正数)`、`rolling`、`expanding`、`cumsum`），输出 `(T,N)` 对齐数组。写法契约与效率规范见 docs/FACTOR_GUIDE.md（向量化优先，引擎会扫描低效模式并警告）。**函数名必须是 `factor`**——`factor_random_generate` 返回的 source 已符合契约，**原样使用不要改名**；手写因子也命名为 factor。批量评估传 `factor_evaluate_batch` 的 `sources`（JSON 对象 `{"名字": "def factor(env): ..."}`，value 是完整源码）。
3. `factor_check_causality` 是**引擎强制的**：前视因子直接被拒绝评估（无需自觉）。**A 类确定性反模式同样硬拒**（2026-08-27 起）：iterrows/itertuples/applymap/apply(lambda)/循环内 np|pd 前缀 concat-concatenate-append——拒绝发生在评估之前（零计算、不计 trial），错误内嵌可直接抄的替换模板；嵌套 for 等仍只是警告。返回附 CPU 计时（慢=疑似非向量化，CPU 口径不受并行会话挤占影响）与 `inefficiency_warning`。
4. `factor_evaluate(stage: development)`——结果顶层有 **verdict**（pass/fail/needs_review）与 **red_flags**（|IC_IR|>5、净年化>200%、test 显著优于 train 等自动标红）：**有 red_flags 必须先解释再下任何结论**；`deflated_train` 是按已试假设数折减后的 p（试得越多门槛越高）——**p>0.05 是入册硬门**（进 red_flags，选择运气不可排除，不显著不入册；N>1 而引擎报「缺池分布基线」时先跑 null-calibration）；`train_sensitivity` 是分界敏感性（只扰动 dev_end，test 绝不扰动）；`duplicate_suspect`/`method_suspect` 是引擎对记忆池的自动对表（数值重复/方法重复）——命中必须先查 factor_query_paths 或论证本质差异。
5. 写失败归因 + 下一步假设，并 `factor_record_trail`（**schema 强制**：round/signal/attribution/next_hypothesis/new_information 五要素——new_information 是第一优先级纪律：这一轮引入了什么新信息源）；证伪具体探索用 `factor_record_explored`（**证伪三条件强制**：exploration 精确定义/evidence 引用具体数字/root_cause 根因；带 source 的证伪自动入证伪记忆池防挖坟），试过的变体用 `factor_record_search_path`。
6. 入册 `factor_registry_submit` 时**带上完整 evaluate 诊断**（引擎校验 receipt：编造数字会被标 verified=false；同一因子换名重复登记、**同一名字重复提交**都被铁律拒绝；red_flags 未清自动拒绝）。描述写错了用 `factor_registry_update` 修正（只许改 signal/note——**不要换名重登、也不要重复 submit，两者都会被拒**）。分享结果用 `factor_export_report`（markdown 结果卡片，含指纹/verdict/null 地形对比）。

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
- **`loop.state == "may_stop"` → 才允许收尾汇报**。停点**只由引擎机械判据触发**：轮次上限（round ≥ max_rounds）/ **当前簇试验上限**（cluster_trials ≥ max_cluster_trials——簇=尾部同族链（|ρ|≥0.6），**换方向即断链重置**；终身 n_trials 只用于 deflation 计价，不做停点，trail 增长永不锁死）/ IC_IR 改善收敛（**滑窗对滑窗**：最近 ic_conv_window 条的最佳 |IC_IR| 未超前一个窗口最佳达 ic_conv_delta——对照前窗而非全历史，无棘轮）/ finalize 已执行。**候选池满、已有 accepted 因子都不是停点**（2026-08-24 session.jsonl 实证：导入的 stateRoot 自带 8 入册因子，旧「池满即停」让 agent 33 个 turn 全停，中位 11 分钟一次，用户手动 push 31 次）。
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

## 并行与多会话纪律（2026-08-27 起，机制已落地）

多会话/多进程并行已受支持（stateRoot 写锁 + test 区原子消费 + CPU 归因超时），纪律三条：

1. **研究家族 = env identity**（数据文件内容哈希 + 口径哈希）：同一 identity 拆多个 stateRoot 稀释 N_eff = 假门，等同 `state.reset` 洗 trail。不同数据面板/不同口径才允许分开 root。
2. **共享账本诚实记账**：同 root 并行会话的试验全部进同一账本，deflation 门槛自动升高——这是设计内行为（多重检验记账本就是为大量试验设计的），不是惩罚，不要试图绕开。
3. **并行会话分核**：各会话设 `DSH_FACTOR_JOBS`（默认 cpu−1），多会话加总 ≤ 核数。worker 超时报「机器超订 / CPU 饿死」= infra_failure——直接重试同一因子（未计账），反复出现就降并行；报「CPU 密集 / 修实现不换假设」才是实现慢。`evaluate_batch` 已进程池化（jobs 并行、部分失败隔离：坏成员标 error 不烧整批）。

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
- 所有产出写在用户 stateRoot，插件仓库不包含任何用户数据、因子库或挖掘结果。
