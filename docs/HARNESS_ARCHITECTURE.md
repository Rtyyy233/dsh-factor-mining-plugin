# 因子挖掘 Harness 架构全量文档 · 总览卷（2026-09-30；2026-10-03 修订：永续化＋loop 三原语＋死线生命周期）

> 规格=HARNESS_ARCH_SAMPLE v5（判定细节到"查哪些数据、怎么算、哪些边界"，
> 语言全白话、不引代码位置）。**分篇制**：每个模块一篇（docs\arch\，每篇
> 五段：它是什么／位置／上下游表／核心机制逐道（防什么·怎么查·阈值·
> 边界·不过怎么办）／成功与失败样例），本卷是总览——阅读路径、分篇目录、
> 流程层、门禁总表、分权总表、附录事故博物馆。已成分篇 12 篇（评估与
> 检验 4＋调度与台账 2＋治理与送达 6），其余篇目规划中待补（目录内标注）。

## 〇、2026-10-03 修订摘要（永续化＋三原语＋死线生命周期）

- **永续不变量**：除死线与用户指令外**无任何终态**。loop.state 值域钉死
  {running, must_rotate}（may_stop 退役）；finalize/fail_streak 停点删除；
  test 一次性锁保留为工具级硬锁，烧毁后走状态页告警（建议数据集轮换）
  而非停推进；注入器「连续简单注入达上限让位真人」分支删除（连注不限，
  防复读护栏移交引擎侧）。
- **loop 三原语（影子态）**：指令选择新增判定层——continue（四类欠账
  任一存在即推：结算/派发/轮询/记录，最旧者优先）、expand（派生图前沿×
  统一排序的坐标载荷；前沿空回退三源轮转供数）、rescue（再定价/组合/
  孵化判负三通道条件成熟即插队）；外加穷尽谓词（全空+井干→告警不停机）
  与双向漂移报警（绕圈/草率收弧/家族产出率，只红字不停顿）。默认影子
  记账（指令仍由现行阶梯产出，新旧对照日志可对账）；exec 开关接管后，
  超时换向活跃时三原语让位（卡死安全网保序）。
- **死线生命周期（会话级+跨重启持久）**：死线落盘插件侧状态文件；
  「这个会话挖到八点」语义（并行会话不连坐）；重启时已过期死线自动
  取消并静默交还用户（不补收官注）；收官/取消后用户一条真实消息=
  重新接管授权（过期死线一并解除，防再收官循环）。
- **Prompt 全层自足性**：工具描述/注入模板/错误文案/技能正文/预置
  persona 全面改写——零历史引用、术语首用即定义、无比喻；黑名单
  grep 机械验收锁死。

## 阅读路径（新读者三步走）

- **第零步·外壳接线（两套系统怎么装进宿主）**：[DSH-EMBEDDING](DSH-EMBEDDING.md)
  （装了什么/在哪层/模型看见什么/怎么送达/状态放哪/怎么升级/三宿主差异）
  ——本卷讲引擎**内部**，这一篇讲**外壳**。

- **第一步·评估与检验（挖的是什么、凭什么算数）**：[evaluation-pipeline]
  (arch/evaluation-pipeline.md)（一次评估怎么跑）→ [multiplicity-wall]
  (arch/multiplicity-wall.md)（为什么挖得越多门槛越高）→
  [novelty-defense](arch/novelty-defense.md)＋[orthogonality-gate]
  (arch/orthogonality-gate.md)（怎么防同一件事反复挖）。
- **第二步·治理与送达链（会话怎么被驾驶）**：[mining-never-stops]
  (arch/mining-never-stops.md)（停走权归属的总纪律）→
  loop 三原语（欠账/扩张/救援——修订已落地引擎，分篇成文待补）→
  [delivery-three-channels](arch/delivery-three-channels.md)（指令怎么送达）→
  [drive-injector](arch/drive-injector.md)（注入器的护栏）→
  [rotate-and-growth](arch/rotate-and-growth.md)（卡住的判定）→
  [deadline-mechanism](arch/deadline-mechanism.md)（死线）→
  [lane-isolation](arch/lane-isolation.md)（多会话身份）。
- **第三步·调度与台账（算力怎么定价）**：[scheduler](arch/scheduler.md)
  （影子先行的研究预算调度器）→ [derivation-ledger](arch/derivation-ledger.md)
  （每个表达式的出身记账）。
- **查某道门**：§四 门禁总表一页对照，再进对应分篇。**查"这件事谁
  决定"**：§五 分权总表。**查历史事故**：§六 附录事故博物馆。

## 一、分篇目录（已成文 12 篇＋规划中 15 篇，docs\arch\）

### 组一 · 评估与检验

| 篇 | 一句话 | 状态 |
|---|---|---|
| [evaluation-pipeline](arch/evaluation-pipeline.md) | 评估管线：异步槽位／在途去重／双端超时／烟测门／评估缓存 | 已成文 |
| [multiplicity-wall](arch/multiplicity-wall.md) | 多重检验墙与 deflation：现价重算／null 地形／单调包络／测试区一次性锁 | 已成文 |
| [orthogonality-gate](arch/orthogonality-gate.md) | 正交门：与在册账本的逐日秩相关（0.80/0.60 软门）＋探索期双池对表 | 已成文 |
| [novelty-defense](arch/novelty-defense.md) | novelty 双防线：选种去相关＋构造查重＋同构拦截＋宣言对账 | 已成文 |
| 烟测门（独立深化篇） | 小面板实测外推／缩放指数三档判定／自动重写等价换用 | 规划中待补（现行机制在评估管线篇第五节） |
| 随机空间与算子集 | 随机生成器的算子集／模板占比／指纹与 null 校准同源 | 规划中待补 |

### 组二 · 深化与分叉

| 篇 | 一句话 | 状态 |
|---|---|---|
| [derivation-ledger](arch/derivation-ledger.md) | 构造台账与派生图＋事件日志：出身记账／边来源四选一／五不变量 | 已成文 |
| [scheduler](arch/scheduler.md) | 调度器 v1.1：五项打分／影子／灰度／ε 底／消费记账键 | 已成文 |
| 近失队列（双线对称） | 0.05＜p≤0.30 的待深化名单：现价重算、按 p 升序、双线对称 | 规划中待补 |
| 层价回扫（人审→全自动，2026-10-06 晚升级） | 机制层价影子重放＋特质判据（A4 残差 null 分位/族代表/正交绝对值）自动放行：rescue_ops 四命令（scan 只读出卡／admit 人审／auto 全自动轮／evidence-refit 拟合冻结）；tick 节流 24h、每轮每账本 ≤10、时序过滤堵事后补票、层 M 按台账行数计防 reset 塌缩、急停开关回人审模式 | 已落地（自动轮首跑 2026-10-06） |
| 深化战役 | 对点名的目标出结构型变体：一颗一颗推进、穷尽由引擎机械判定 | 规划中待补 |
| deepdig 关闭门 | 深挖通道的阀门：何时开、何时关、关闭条件机械判定 | 规划中待补 |
| 结构变体与家族地图 | 加腿/拆杠杆/条件门等结构轴操作与家族谱系 | 规划中待补 |

### 组三 · 供数与灵感

| 篇 | 一句话 | 状态 |
|---|---|---|
| farm 与选种 | 随机农场供数：三列表（top/top_tail/top_net）＋反同质化选种 | 规划中待补 |
| 审阅钟 | 周期审阅：调度器 tick 节流遥测、影子打分与选点行 | 规划中待补 |
| 灵感三源轮转 | 文献→随机→机制的确定性轮转与配额 | 规划中待补 |
| 概念碰撞卡 | 内容盲抽样的概念刺激卡：连接/变形/弃权免费 | 规划中待补 |
| arxiv 通道 | 论文检索：类目过滤／去重台账／exhausted 禁引／种子正交 | 规划中待补 |
| 孵化队列与候补池 | 候补视图（standby）与三条复活通道（重定价/组合/前向孵化） | 规划中待补 |

### 组四 · 治理与送达

| 篇 | 一句话 | 状态 |
|---|---|---|
| [lane-isolation](arch/lane-isolation.md) | lane 隔离与首调门：意图按线／计价全局／绑定落盘／双层契约 | 已成文 |
| [delivery-three-channels](arch/delivery-three-channels.md) | 三通道送达：工具响应指令区／回合注入／引擎指令文件，同源纪律 | 已成文 |
| [drive-injector](arch/drive-injector.md) | drive 注入器：武装条件／静默窗／键去重／楔死恢复／停摆复活扫描／连注不限（永续） | 已成文 |
| [rotate-and-growth](arch/rotate-and-growth.md) | 超时换向与增长双轴：判窗 10 分钟／无终态／每窗恰一注 | 已成文 |
| [deadline-mechanism](arch/deadline-mechanism.md) | 死线机制：最低期限非收摊点／会话级跨重启持久／到点一次收官／重启过期自动取消／迟到必告警 | 已成文（2026-10-03 生命周期修订已同步分篇） |
| [three-primitives](arch/three-primitives.md) | loop 三原语：欠账扫描（结算/派发/轮询/记录）／前沿×排序扩张／三通道救援／穷尽谓词／漂移报警／防复读护栏；影子→exec 切换纪律 | 已成文（2026-10-04 补） |
| [mining-never-stops](arch/mining-never-stops.md) | 挖掘不停止纪律：四把锁／三轮演进／收摊否决／永续不变量／退役全史（终态仅死线与用户指令） | 已成文（永续化修订已同步分篇） |
| 证据零暴露纪律 | 绿侧证据遥测：evidence_ledger 旁路账本／A4 残差拟合冻结（evidence_fit 版本只增）／null 地形 a4_dd·famsize 分位段——全部只进引擎侧账本与人审卡片，不进任何模型面响应（暴露=激励适配，校准被侵蚀） | 已落地（2026-10-06） |

### 组五 · 外围

| 篇 | 一句话 | 状态 |
|---|---|---|
| 数据管线（两库语义） | 两套数据面板的语义差异与口径管理 | 规划中待补 |
| 策略层 harness | 消费已入册因子做组合策略的姊妹 harness | 规划中待补 |
| test 锁与方案 B | 测试区一次性锁的深层机制与分层计价影子模式 | 规划中待补 |
| 效率编译层与 mmap 缓存 | 自动编译加速、强模型重写、内存映射数据缓存 | 规划中待补 |
| 桥与 worker 进程模型 | 单线程桥／子进程隔离／进程纪律（严禁杀进程） | 规划中待补 |

## 二、流程层：一个因子的一生（16 步）

1. **灵感出生**——三源任选：自提机制假设／文献迁移／随机探索（可抽
   概念碰撞卡刺激）；动笔前查重（选种已去相关、构造指纹先检先例）
   （[novelty-defense](arch/novelty-defense.md)）。
2. **评估前注册**——把预期（方向/半衰期/换手）与源码写进推理日志：
   没有注册预期的评估是白测，评估后没有东西可对照。
3. **写源码**——factor(env) 契约，只用过去数据；第一版就向量化
   （重方法要把因子跑 8-10 遍，逐个循环的单遍即分钟级）
   （[evaluation-pipeline](arch/evaluation-pipeline.md)）。
4. **免费拒绝两道**——烟测门（小面板实测外推超预算＝零试验拒绝＋
   自动重写）与因果检验（前视构造、确定性反模式在评估之前硬拒）
   （[evaluation-pipeline](arch/evaluation-pipeline.md)）。
5. **评估派发**——异步作业秒回作业号；同参在途自动并入；缓存命中
   即终局；未命中缓存的派发计入本线活动轴
   （[evaluation-pipeline](arch/evaluation-pipeline.md)＋
   [rotate-and-growth](arch/rotate-and-growth.md)）。
6. **落账**——每次评估自动落 expand 事件：快照自带当时池参数、现价
   p、墙单位增益、近失旗、族名；首次出现才计新候选（消耗统计资本）
   （[derivation-ledger](arch/derivation-ledger.md)）。
7. **现价折减**——多重检验墙按全部试验现价重算 p：选择运气线、池
   分布尺度、批内族口径、跨轨分账
   （[multiplicity-wall](arch/multiplicity-wall.md)）。
8. **镜像对账与归因**——预期 vs 实测机械并置；轨迹五要素（含新信息
   源第一优先级：这轮引入了什么库里没有的新信息）。
9. **分流三路**——入册候选→提交门链；近失（0.05＜p≤0.30）→近失
   队列待深化；证伪→证伪记忆池防挖坟
   （[multiplicity-wall](arch/multiplicity-wall.md)＋
   [novelty-defense](arch/novelty-defense.md)）。
10. **提交门链**——噪声世界（|z|≥3 拒）、展平检验（cliff 签名拒）、
    价差噪声门（尾部轨，不可判定即中止）、日排列零（报告）、回执
    校验（编造数字拒）、铁律（同源换名/同名重投拒）
    （[multiplicity-wall](arch/multiplicity-wall.md)）。
11. **正交门对表**——与在册账本的开发区逐日秩相关：软门标注＋提示，
    冗余争议由测试区终裁（[orthogonality-gate](arch/orthogonality-gate.md)）。
12. **入册**——registry 落账、正交基线追加；入册数跨三的倍数触发
    一次即消费的里程碑审计（[orthogonality-gate]
    (arch/orthogonality-gate.md)＋[mining-never-stops]
    (arch/mining-never-stops.md)）。
13. **深化**——结构变体（加腿/拆杠杆/条件门）、近失队列按现价 p
    升序逐个消化；调度器灰度点名（选点行只含事实，消费记账键）
    （[scheduler](arch/scheduler.md)）。
14. **候补与孵化**——开发段优异未过线的存量进候补视图；复活通道
    （分层重定价/规则化组合/前向孵化）由引擎机械触发，无申诉权。
15. **终局 test 消费（永续语义）**——声明制消费 test 区（一次性锁，
    护栏兜底）；幸存者入册终裁。test 烧毁**不是停机点**：状态页告警
    「建议数据集轮换」，开发/选择区照常推进——永续不变量：除死线与
    用户指令外无任何终态（[multiplicity-wall](arch/multiplicity-wall.md)＋
    [mining-never-stops](arch/mining-never-stops.md)）。
16. **全程被驾驶**——停走指令随五个工具响应必达、回合边界主动注入、
    直驱会话读指令文件；增长双轴计时、卡住超时换向、死线到点收官
    （[delivery-three-channels](arch/delivery-three-channels.md)＋
    [rotate-and-growth](arch/rotate-and-growth.md)＋
    [deadline-mechanism](arch/deadline-mechanism.md)）。

## 三、门禁总表（一页对照：门｜查什么｜阈值｜不过怎么办｜所在篇）

| 门 | 查什么 | 阈值 | 不过怎么办 | 所在篇 |
|---|---|---|---|---|
| 因果检验 | 噪声扰动法跑因子 8 遍比对＋确定性反模式静态扫描 | 前视/逐行循环类=违规 | 评估之前零计算零试验拒绝，文案带替换模板 | [evaluation-pipeline](arch/evaluation-pipeline.md) |
| 烟测门 | 两块小面板实测耗时→拟合缩放指数→外推全量×方法遍数 | 投影超方法预算；α>1.4=超线性 | 零试验拒绝；自动编译/重写后原样重交 | [evaluation-pipeline](arch/evaluation-pipeline.md) |
| 双端超时 | 客户端 900 秒；worker 墙钟基数 600 秒按核份额伸缩 | 超时 | 零入账可重试；同参重提自动并入在途作业 | [evaluation-pipeline](arch/evaluation-pipeline.md) |
| null 地形指纹门 | 地形绑定数据+口径+引擎版本+生效算子集指纹 | 任一变更=失配 | 拒绝给出 p，先重校准（不给不可信数字） | [multiplicity-wall](arch/multiplicity-wall.md) |
| deflated p 硬门 | 四充分统计量+门参数重算 p，扣选择运气线 | p≤0.05；p 不可算=拒 | 拒收；单发豁免不设（符号选择也是选择） | [multiplicity-wall](arch/multiplicity-wall.md) |
| 批内族口径/跨轨 Šidák | 批内相关矩阵→选择运气计价；双轨分账 | 轨门槛 α≈0.0253 | 族内选择未过轨门槛拒 | [multiplicity-wall](arch/multiplicity-wall.md) |
| 单调包络 | 写盘时刻对全量试验算有效数并盖章 | 只升不降 | 灌水只能遇到封顶墙 | [multiplicity-wall](arch/multiplicity-wall.md) |
| 测试区一次性锁 | 预约→计算→落锤三步原子 | 已消费/他人在途=拒 | 数据性失败不算消费；崩溃预约按进程存活回收 | [multiplicity-wall](arch/multiplicity-wall.md) |
| 噪声世界门 | M 个波动率匹配随机世界重跑因子 | 跨世界 \|z\|≥3 | 无条件拒收（拟合评价管道 artifact），先查因果构造 | [multiplicity-wall](arch/multiplicity-wall.md) |
| 日排列零 | 真实边际+随机重配时间线，p_two | 方向待校准 | 报告不拦（report-only） | [multiplicity-wall](arch/multiplicity-wall.md) |
| 展平检验 | 申报参数最小步长邻域逐一评估 | cliff 签名（符号翻转/塌缩/退化） | 事务中止；申报值不在源码=中止 | [multiplicity-wall](arch/multiplicity-wall.md) |
| 价差噪声门 | 尾部轨的合成世界检验 | \|z\|≥3 拒；不可判定=fail-closed | 尾部轨事务中止，不做静默放行 | [multiplicity-wall](arch/multiplicity-wall.md) |
| 尾部安慰剂 | top-N 净超额 vs 随机选股零假设 | placebo_z≥3（单侧） | 未过拒（尾部轨准入链首门） | [multiplicity-wall](arch/multiplicity-wall.md) |
| 正交门 | 候选与在册因子的开发区逐日秩相关（公共截面≥300） | ≥0.80/0.60 | 软门：标注+提示不拦，测试区终裁（可切硬拒） | [orthogonality-gate](arch/orthogonality-gate.md) |
| 探索期双池对表 | 活跃池/证伪池的指纹比对 | 数值≥0.90、结构≥0.70 | 提醒不否决（先查相似检索、论证差异） | [orthogonality-gate](arch/orthogonality-gate.md) |
| 同构重交拦截 | 对全部先例的最大构造指纹相似 | ≥0.80 且先例已过 | 评估之前零试验拒绝；常量变/先例未过/源码缺失三放行 | [novelty-defense](arch/novelty-defense.md) |
| 宣言分型对账 | "新"字宣称 vs 数据列增量/指纹相似 | 相似<0.6 或有数据列增量 | 证据不足放行（防寒蝉优先）；确证则要求如实记"变体" | [novelty-defense](arch/novelty-defense.md) |
| 选种 novelty | 名次+λ×(1−max\|ρ\|) 贪心选种 | λ=0.3（可关） | 同族亚军让位独立候选 | [novelty-defense](arch/novelty-defense.md) |
| 首调门 | 会话身份+绑定表 | 未绑定账本 | 工具层 Proxy 拒绝，二选一声明后放行（重启持久） | [lane-isolation](arch/lane-isolation.md) |
| lane 双层契约 | 工具清单声明×处理器转发对拍 | 缺一=静默丢参 | 全量对拍审计；引擎拒收"空源码无作业号"请求 | [lane-isolation](arch/lane-isolation.md) |
| 穷尽宣告拒收 | 下一步假设的词族扫描 | 穷尽/天花板类措辞 | 拒收并要求重写（实测 12/12 全假） | [mining-never-stops](arch/mining-never-stops.md) |
| 停走判定 | loop.state（running/must_rotate 两值；值域外=版本错配保险丝） | 永续：无静默终态 | 停笔违规；test 烧毁=告警建议数据集轮换，不停推进 | [mining-never-stops](arch/mining-never-stops.md) |
| 超时换向 | 增长双轴（本线试验数/未命中评估派发）零增长超窗 | 判窗 10 分钟（评估在途豁免限时 45 分钟） | 换向指令每窗恰一注、无上限直到有评估落账 | [rotate-and-growth](arch/rotate-and-growth.md) |
| 供数限发 | 三个机械库存同时为空 | 3 发/间隔 10 分钟 | 预算耗尽落入超时换向循环（无终态） | [rotate-and-growth](arch/rotate-and-growth.md) |
| 死线收官 | 会话级死线墙钟（跨重启持久） | 到点一次收官 | 收官注入后该会话解除推进；重启过期=自动取消静默交还（不补收官）；用户真实消息重新接管；迟到 30 分钟节流必告警 | [deadline-mechanism](arch/deadline-mechanism.md) |
| 策略键去重 | 上次投放键 vs 新策略键 | 复杂类型同键不重发；可重推类连注不限（永续） | 静默（日志可查）；防复读护栏在引擎侧：同欠账三次重申未消账→升级告警并继续推 | [drive-injector](arch/drive-injector.md) |
| 楔死恢复 | 运行中且零会话事件 | >30 分钟 | 取消死回合保收件箱，收敛后重推复活 | [drive-injector](arch/drive-injector.md) |
| 静默窗/真人优先 | 回合结束后等待真人 | 60 秒 | 窗内真人消息取消待发注入并清零计数 | [drive-injector](arch/drive-injector.md) |
| C 类欠账扫描 | 推理日志结算欠账／声明后无派发／在途作业未轮询／引擎账未被叙事追上（四类，最旧者优先） | 判窗 15/5/10 分钟（影子期标定） | 欠账指令可重推；消账即停；三次未消账升级告警不停推 | loop 三原语（分篇待补） |
| E1 前沿枚举 | 派生图前沿节点×合法变体轴−该节点已试轴；家族预算余量 | 节点取最新 10 个（预算纪律）；预算耗尽=前沿空 | 前沿空→E4 三源轮转供数（文献/随机/自提机制，回退链无限） | loop 三原语（分篇待补） |
| rescue 成熟检查 | 再定价（现价 p 下移）／组合（正交组 dry-run）／孵化判负（窗口满）三通道 | 成熟即插队 | 组合点名边沿消费（同组合不重挂）；引擎选成员，模型只触发 | loop 三原语（分篇待补） |
| 穷尽谓词 | 四欠账空∧前沿空∧三救援空∧井干（近失/候补/孵化待判/未消化假设全空） | 全空才告警；子查询失败按未知不算干 | 告警建议数据集轮换（用户操作），不停机不静默 | loop 三原语（分篇待补） |
| 漂移报警 | 绕圈（同构簇零产出）／草率收弧（弧试验数骤降）／家族产出率（滑窗零产出+预算过半） | 只红字不停顿 | 状态页+对账日志警示；不改变推进 | loop 三原语（分篇待补） |
| exec 让位治理 | 三原语接管期检查治理总闸动作 | 超时换向（rotate）活跃=让位 | 卡死安全网保序；供数指令（drain）不让——exec 期统一走 E4 | loop 三原语（分篇待补） |
| 影子红线 | 调度器/三原语打分、tick、日志零计入活动数与试验数 | 零计入 | 违反会被回归测试抓出（打分不是挖掘活动） | [scheduler](arch/scheduler.md) |
| 逐比特回退 | 执行份额=0 时系统行为与现状一致 | 等价性回归 | 权重改动必须走函数版本号+回归，禁在线调权 | [scheduler](arch/scheduler.md) |
| 消费记账键 | 选点行 pick 键：派发命中才消费 | pending 同哈希不重挂 | 会变键的重复挂出=重注机，被键纪律拦截 | [scheduler](arch/scheduler.md) |
| 停止规则 | 视界内至少一次成功的后验概率 | <0.05 | 退场进孵化（可重返），终态只有入册/判死/证伪 | [scheduler](arch/scheduler.md) |
| 图五不变量 | 无环/子代可复现/单写者/打分确定性/关闭等价 | 检查器随时可跑 | 被改坏在回归测试里炸，不在生产静默腐化 | [derivation-ledger](arch/derivation-ledger.md) |
| 挂边纪律 | 边来源四选一按序判定（点名/农场/声明/诚实根） | 推断不出 | 记无链根——宁缺毋假 | [derivation-ledger](arch/derivation-ledger.md) |
| score 事件版本号 | 打分事件必须带函数版本号 | 缺版本号 | 拒绝落账（重放可复现的钥匙） | [derivation-ledger](arch/derivation-ledger.md) |

## 四、分权总表（每件事谁决定）

| 事项 | 引擎 | 模型 | 调度器 | 用户 |
|---|---|---|---|---|
| 假设与因子构造 | — | **决定＋执行** | — | — |
| 验证与判定（因果/评估/门链） | **执行且说了算** | — | — | — |
| 停走 | 机械判据说了算（永续：无静默终态） | 禁自停（穷尽宣告拒收） | — | 死线来源；收官/取消后接管（真实消息=重新接管授权） |
| 挖什么方向 | 指令只定何时与合规判据，不引导去哪 | **决定**（09-18 定调） | 灰度点名（只含事实） | — |
| 算力投放 | — | — | 影子打分＋灰度选点＋ε 硬底 | 开关与权重（禁在线调权） |
| 三区划分 | 校验并落盘 | 建议分界 | — | **拍板**（研究诚信的分界线） |
| 数据集/池/频率轮换 | 接收上报 | 随里程碑上报，继续挖 | — | **操作**（不是停笔理由） |
| test 区消费 | 一次性锁兜底 | 声明制执行 | — | — |
| 死线时刻 | 接管停止条件 | 按口述设置/清除/查询 | — | **口述**（唯一合法来源） |
| 账本选边 | 首调门＋绑定表 | 显式声明 | — | 部署几个账本 |
| 重复与否 | 只报事实（查重不查新） | 论证本质差异或如实记变体 | — | — |
| 灵感源切换 | 轮转与配额记账 | 声明（配额内自主） | — | — |
| 假阴性救助（层价回扫，已全自动） | **算价＋留证＋机械放行**（机制层价重放／evidence_ledger 原始值／null 分位——模型面零暴露；auto 判据=层价 0.0253+时序+正交绝对值+A4 门+族代表+配额） | 禁知（回扫判据、A4 门槛、配额对模型不可见） | — | **开关与急停**（rescue_auto.enabled；关=回人审拍板模式；尾轨/边界候选仍缓议留人） |
| 产出归属 | 全部写入用户账本 | — | — | **全部产出归用户** |

## 五、附录：事故博物馆（12 起，全部真实在案）

1. **双通道假死（2026-09-18）**——一晚深挖批次全灭：错误响应被双重
   编码致客户端配对失败，更深一层是 worker 子进程继承了桥的输入管道、
   在管道上永久冻结（手动跑 1-2 秒的作业在桥内 4/4 冻结）；烟测判决
   按指纹缓存让同文本重试永远秒拒，形成"惯性阵亡"。教训：worker 必须
   断开继承管道；"全灭＋同源秒拒"先重启桥再查代码
   （[evaluation-pipeline](arch/evaluation-pipeline.md)）。
2. **楔死与流死（2026-09-19 深夜）**——宿主重启后一个会话的生成流死于
   中途，回合状态永卡"运行中"：事件路径等回合结束（等不到）、扫描路径
   尊重忙碌会话（永久跳过），两会话双哑到天亮。楔死恢复（30 分钟零事件
   →取消死回合→收敛后重推）由此入档
   （[drive-injector](arch/drive-injector.md)）。
3. **空转三修（2026-09-19）**——死线武装的会话凌晨 03:00-10:00 分钟级
   空转约 450 回合。三轮修复对应三层根因：电平触发无消费→消费制；
   叙事回合被算成增长＋键编回合数→只认试验落账＋稳定键；收摊态违反
   死线语义→终稿超时换向制（[mining-never-stops]
   (arch/mining-never-stops.md)＋[rotate-and-growth]
   (arch/rotate-and-growth.md)）。
4. **裸重启丢代理（2026-09-20）**——直驱夜跑的 runner 重启时没带代理
   环境变量：本地网络直连论文接口不通，当夜文献检索全部失败（灵感
   一源整条断掉）。教训：直驱链路的运行时依赖（代理）在重启脚本里、
   不在引擎里——裸重启等于静默砍掉一条通道
   （[delivery-three-channels](arch/delivery-three-channels.md)；arxiv
   通道分篇规划中待补）。
5. **全灭三层根因（2026-09-20）**——同夜批次全灭，复盘出三层叠加：
   活动记账的线号参数没随请求转发（活动轴读不到真实派发）、输出流
   读取无超时（一处卡死永久挂起）、宿主进程静默死亡（无任何日志）。
   三层分别修：参数对拍审计、超时与楔死类恢复、可观测三件套
   （[lane-isolation](arch/lane-isolation.md)＋
   [drive-injector](arch/drive-injector.md)）。
6. **compose 一夜 13 连注（2026-09-20 前）**——合成指令每回合重推：
   触发是电平的（库存常驻即条件恒真），键又编了回合数（每轮都是
   新键），注入器同键去重永不咬合。修法确立"边沿触发＋一次即消费"
   规律：同一事件只催一次，条件再满足才再发
   （[mining-never-stops](arch/mining-never-stops.md)）。
7. **正交门缝（2026-09-13）**——正交门的入口包装只落在主代码树，一台
   从工作树直驱的桥加载不到它，导入失败后门静默跳过：一晚 11 笔提交
   全部漏检，10 条冗余因子回流账本。用户终裁保留回流条目并补标注；
   教训："失败即放行"的门最怕部署不一致——它的失败方式恰好是沉默
   （[orthogonality-gate](arch/orthogonality-gate.md)）。
8. **近失深化执行率 0/30（2026-09 前后）**——近失待深化名单落盘 30
   条，会话执行 0 条：产物写进了文件，但没有任何送达通道把它带回
   会话眼前——落盘无人读等于没写。深化点名此后并入指令通道（队列
   点名随指令区/指令文件送达），算力落点由调度器收编
   （[delivery-three-channels](arch/delivery-three-channels.md)＋
   [scheduler](arch/scheduler.md)；近失队列分篇规划中待补）。
9. **死线迟到 4h19m（2026-09-29）**——死线 09:00，两个会话的收官注
   13:19 才到。取证：收官注时间戳与"用户消息回合结束后 60 秒整"吻合
   （真人中午回来恰好走通事件路径）；引擎换向记账冻结在第一注；根因
   是空闲会话只剩 5 分钟扫描一条腿且凌晨起不再送达。修复：代理缺失
   告警＋死线未送达告警＋诊断面——"按时到或必告警，不允许第三种
   沉默"（[deadline-mechanism](arch/deadline-mechanism.md)）。
10. **桌面迁移断链与 CLI 不可见（2026-09-30，已修复）**——桌面前端
    部署后注入全停。双根因：桌面会话一律加预置（工具面=预置的封闭
    清单），工具包不在任何预置清单里→全部桌面会话看不到因子工具；
    版本兼容门又把服务行禁用。同夜一个纯命令行直驱的挖掘会话真挖
    一小时后自停等口令——它不走工具面，对注入器永远不可见（设计
    边界，挂账待拍板）。修复在配置层：预置清单补行＋注入器只由预置
    行持有＋精确版本豁免；修复后首个会话 65 分钟自主挖掘两只入册
    （[delivery-three-channels](arch/delivery-three-channels.md)＋
    [drive-injector](arch/drive-injector.md)＋
    [lane-isolation](arch/lane-isolation.md)；修复报告见
    analysis/glm_mining/REPORT-desktop-factor-tools-fix-2026-09-30.md）。
11. **连注让位咬死无死线日间挖掘（2026-10-02 定罪，随永续化移除）**——
    注入器的「连续简单注入达上限（5）→暂停等待真人」保险丝在无死线的
    日间挖掘里变成永久静默：第 6 注起注入器永远沉默，会话停摆等一个
    不在场的真人。教训：给永续系统设「让位」分支=给死锁开门。修复：
    分支删除（连注不限），防复读护栏移交引擎侧（同欠账三次重申未消账
    →升级告警**并继续推**）（[drive-injector](arch/drive-injector.md)＋
    loop 三原语分篇待补）。
12. **过期死线再收官循环（2026-10-03 行为测试抓出，随死线生命周期
    修复）**——收官/重启取消后用户发真实消息重新接管时，已过期的死线
    仍武装在身：下一轮扫描立即再收官、再静默、再等接管——循环。教训：
    「重新接管」必须连带清退触发静默的条件本身。修复：接管时未到点
    死线保留、已过期死线一并解除（要新死线重新设置）
    （[deadline-mechanism](arch/deadline-mechanism.md)）。

---

*总览卷（分篇制）。各篇判定细节以各门拒绝文案为最终真相（拒绝即教学）；
停走与指令全文以模型可见上下文盘点（docs/model-context/f-dsh.md）的
卡与附录为最终真相。*

