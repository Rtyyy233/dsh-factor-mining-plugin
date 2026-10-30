# 调研:业界绝对收益策略的 ML 特征输入(2026-09-01)

> 任务:用户指令「业界如果想做绝对收益策略,会让模型接受什么来作为
> 特征」。范围 = 系统化/量化绝对收益(非主观宏观对冲),重点映射到
> 我们的 ETF 面板语境。检索来源见 §5。

---

## 0. 结论摘要(TL;DR)

1. 业界绝对收益的主流架构是三段式:**Beta 择时 × 截面轮动 × 波动率/仓位
   控制**——与我们此前「绝对收益 = 横截面 alpha + Beta 择时」的设计
   完全同构,架构判断被业界印证。特征问题因此拆成两个:轮动模型吃什么、
   择时模型吃什么。
2. 特征维度上,业界比我们多用了三大类:**跨资产/宏观状态量、ETF 特有
   微观结构(溢价 × 份额申赎)、期权隐含信息**。截面价量特征(我们的
   主战场)业界同样用,但只是其中一类。
3. **对我们最重要的两条**:
   - ETF 溢价/折价 × 份额申赎的**交互**在文献中有强次日预测效应
     (Liao-Yin-Zhao:年化 14.02%、Sharpe 0.88、t=3.40)——与昨天评估
     发现的 cvv_div_dd60(隔日级快信号、全场最正交)同型同频,**文献
     与我们的实证互相印证**:ETF 的隔日信息住在价格对净值/均价的偏离
     和份额变动里,不在多日截面动量里。
   - 跨资产特征(债券/黄金/商品 ETF 的动量与波动)**我们的面板本来就
     有**(1642 只含赛道/商品/海外/QDII),业界当大类配置信号用,我们
     从未把它们作为特征列——**零新数据成本的新维度**。

## 1. 类型学:绝对收益在业界的实现路径

| 路径 | 代表 | 模型目标 | 主力特征 |
|---|---|---|---|
| 择时+轮动叠加 | 广发金工神经网络 ETF、我们的 V20/三档门 | 仓位门控 × 多头轮动 | 时序动量/价量状态 + 截面排序 |
| 多空市场中性 | Gu-Kelly-Xiu 式截面 ML、中性因子组合 | 净暴露≈0 的多空价差 | 94 类截面特征 |
| CTA/趋势 | Man AHL、Graham、Quantica | 跨市场趋势跟随 | 时序动量、波动率目标、突破 |
| 大类配置/风险预算 | 华泰基本面轮动、风险平价系 | 资产间权重 | 时序动量、目标波动率、相关性状态 |
| 套利/微观结构 | ETF 申赎套利(授权参与者视角) | 次日短持有 | 溢价/折价、份额流、净值偏离 |

共同点:绝对收益 ≠ 找更强的截面 alpha,而是**把截面 alpha 与独立的信息
源(Beta 状态、跨资产状态、微观结构)组合**。Man AHL 的表述:ML 的价值
是把「大量弱信息源」连贯地组合起来——单源都不显著,组合后可预测。

## 2. 业界特征清单(按类别,附证据)

### A. 截面价量衍生(轮动/选股模型的经典输入)
Gu-Kelly-Xiu(2020, RFS,引用 4500+)的 94 特征分类是权威骨架:多
horizon 动量/反转、流动性(Amihud/换手)、波动(beta/特异波动)、
价值、投资/盈利 + 74 行业哑变量 + **8 个宏观变量**。我们 ETF 面板上
已系统穷尽此类别(两条线互证的「饱和」结论),**此维度业界不比我们多**。

### B. 时序/Beta 择时特征(绝对收益的独有输入)
- 时序动量(Quantpedia:绝对动量主要贡献是**降波动降回撤**,相对动量
  才贡献收益)——对应我们的三档仓位门;
- 波动率目标/条件波动率目标(Alpha Architect:波动管理产生正偏;
  业界共识「连续波动率目标 ≈ 滚动 Kelly 比例」);
- 趋势质量、回撤深度、市场宽度/离散度、相关性状态。
- 我们的教训与之吻合:择时信息方向存在但**决策次数少导致量级不可
  分辨**——业界解法是让择时特征**日频可观测**(状态量而非事件量)。

### C. 跨资产/宏观状态量
- GKX 的 8 宏观变量(通胀、工业产出、失业等经典序列);
- 利率与期限结构、信用利差、汇率、商品动量;
- 银河金工(2026-04):宏观变量直接驱动宽基 ETF 风格轮动;
- 华泰:大类层用时序动量+风险预算+目标波动率,权益层才轮动。
- **对我们的映射**:不需要外部宏观数据——面板内的债券 ETF、黄金 ETF、
  商品 ETF、QDII 本身就是跨资产状态的日频可观测代理,可直接构造
  「债券 20/60 日动量、黄金动量、内外价差(QDII vs A 股)」等特征列。
  业界用整条收益率曲线,我们用 ETF 代理,粒度粗但方向同源。

### D. ETF 特有微观结构(文献效应最强的一类)
Liao-Xu-Yin-Zhao(2021,美股 2012-2016):
- 每日资金流分解为需求成分与套利成分,**回归残差 =「意外资金流」**;
- 意外资金流与次日收益显著正相关;**条件性关键**:溢价+份额创设 →
  预测次日正收益;折价+赎回 → 预测次日负;方向不匹配时不可靠;
- 市值加权多空年化 14.02%、t=3.40、Sharpe 0.88(扣成本);
- 所需数据 = ETF 参考数据:份额存量、NAV、AUM、跟踪指数、价差。
- 国内对应:交易所每日公布 ETF 份额,基金公司每日公布单位净值——
  **历史可回溯全长**,工程量在管道不在可得性。
- 这正是 QUANT_MASTER 里挂了半年的 P3(「ETF 申购=真金白银,机制
  不同于个股放量」)的文献版,也是 dsh 线「净值折溢价」新信息维度的
  具体化。**与我们 cvv_div_dd60(价格/日内均价乖离÷回撤,隔日信号,
  置换 z=−12.97)同频互证**——cvv 是它的价格侧影子,份额/净值数据
  会补上流量侧。

### E. 期权隐含信息
- VIX 及其期限结构、SPX 偏斜(学术:VIX 期权 IV slope 预测 VIX 期货
  收益;下行偏斜陡 = 尾部保险贵 = 悲观状态);
- A 股对应物:上证 50ETF 期权(2015 起)、沪深 300ETF 期权(2019 起)、
  500/1000/创业板 ETF 期权——**IV、PCR(认沽认购比)、偏斜都有多年
  历史**,可作全市场择时特征列。数据源待查(免费通道:新浪/东财期权
  页,深度待验证)。

### F. 端到端深度学习(特征工程自动化)
广发金工 2023-2024 系列:CNN 直接吃**标准化价量图表**识别走势分类,
用于 ETF 轮动(2020 年来约 30.7% 年化,样本外持续跟踪);2026-03
新发的 GRU 技术形态专家模型同思路。这是「让模型自己从原始价量学特征」
路线——与我们 GBDT 手工特征的失败不冲突(它们把表达力花在形态识别,
样本量是日频多标的,远大于我们择时侧的 ~110 有效观测),但对我们
的可审计纪律是大改写,列为远期选项。

## 3. 对照我们的资产:已有/缺失

| 业界特征类别 | 我们的状态 | 缺口性质 |
|---|---|---|
| A 截面价量 | ✅ 饱和(23 入册因子+56 试验) | 无 |
| B 时序择时特征 | ⚠️ 试过(16 状态量族;量级不可分辨=样本瓶颈) | 需日频可观测的新状态量 |
| C 跨资产/宏观 | ❌ **从未用**——但面板自带债券/黄金/商品/QDII | 零数据成本,纯构造 |
| D ETF 微观结构 | ❌ 缺份额/净值数据;价格侧影子(cvv_div_dd60)已证强 | 需建数据管道(可得性好) |
| E 期权隐含 | ❌ 缺 | 数据源待验证 |
| F 端到端 DL | ❌ 未做 | 远期 |

## 4. 可执行建议(信息增量 × 获取成本排序)

1. **面板内跨资产特征(先做,零新数据)**:债券/黄金/商品/QDII ETF 的
   20/60 日动量与已实现波动、A 股-海外价差,作为全市场状态列广播进
   特征卡——业界大类配置标配信号,我们的面板一直躺着自己没吃。
2. **ETF 份额+净值管道(第二优先,文献效应最强)**:每日份额存量 ×
   单位净值 → 溢价率、意外申赎流;与 cvv_div_dd60 组成隔日信号族。
   自然口径 = 短 horizon(与 D5 决策点合并拍板)。
3. **期权隐含列(第三)**:50/300ETF 期权的 IV level/期限/PCR 作择时
   状态量——先做数据源可行性验证。
4. 端到端 DL(广发式 CNN)仅在上面增量证伪后再议。

## 5. 来源


- [Gu, Kelly, Xiu — Empirical Asset Pricing via Machine Learning (RFS 2020)](https://academic.oup.com/rfs/article/33/5/2223/5758276)([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3159577)、[AQR 摘要](https://www.aqr.com/Insights/Research/Journal-Article/Empirical-Asset-Pricing-via-Machine-Learning))
- [Quantpedia — How to Use ETF Flows to Predict Subsequent Daily ETF Performance](https://quantpedia.com/how-to-use-etf-flows-to-predict-subsequent-daily-etf-performance/)(底层论文 Liao, Xu, Yin, Zhao 2021)
- [Man Group — The Rise of Machine Learning](https://www.man.com/insights/the-rise-of-machine-learning)、[Trend Following: Optimal Market Mix](https://www.man.com/insights/trend-following-optimal-market-mix)
- [Alpha Architect — Conditional Volatility Targeting](https://alphaarchitect.com/conditional-volatility-targeting/)
- [Quantpedia — Asset Class Trend-Following](https://quantpedia.com/strategies/asset-class-trend-following)
- [广发金工神经网络 ETF 绝对收益策略(2026-07 月报)](https://finance.sina.com.cn/wm/2026-07-08/doc-inihaxzt1463986.shtml)、[基于卷积神经网络的 ETF 轮动策略](https://www.vzkoo.com/read/4636590714631753728.html)
- [华泰 — 基本面轮动之绝对收益型 ETF 轮动](https://zhuanlan.zhihu.com/p/474115532)、[海通 — 通过 ETF 轮动的绝对收益策略](https://bigquant.com/wiki/doc/qhayFoZG2j)、[中泰 — 机器学习挖掘隐式因子 ETF 轮动](https://www.scribd.com/document/720043183/)
- [GRU 技术形态专家模型(2026-03)](https://finance.sina.com.cn/wm/2026-03-24/doc-inhsavws1674245.shtml)
- [Harbour Front Quant — VIX vs SPX Options: Skewness, Term Structure](https://harbourfrontquant.substack.com/p/vix-vs-spx-options-skewness-term)、[VIX option-implied volatility slope](https://www.researchgate.net/publication/359398123_VIX_option-implied_volatility_slope_and_VIX_futures_returns)
- [Quantica — Are Trend-Following CTAs Truly Long Vol](https://quantica-capital.com/en/publication/are-tf-ctas-truly-long-vol)

## 6. 附录:时序择时特征的构造与检验范式(2026-09-01 追问补充)

### 6.1 构造

截面因子每日 N 个观测可互比;时序特征每日 1 个观测、高自相关、有效
样本=独立 regime 数——构造三原则:**降方差、多尺度集成、状态化**。

1. **趋势族**:多回看期时序动量(1/3/6/12 月集成投票/平均,TSMOM
   Moskowitz-Ooi-Pedersen 2012);Quantpedia 实证:对价格路径 OLS 取
   **斜率**优于简单区间收益(样本外最优+换手最小——用足路径、末端
   不敏感);MA 乖离/通道突破为简化版。
2. **平滑与确认层(防 whipsaw,时序特有)**:EWMA 平滑、N 日同号确认、
   **滞后带**(开/平仓阈值分离,带内维持——与 top-k 缓冲簿同思想)、
   切换成本内建。
3. **波动率族**:多窗已实现波动、波动率目标(≈滚动 Kelly)、波动率
   期限结构(短/长比)、EWMA/GARCH 预测。
4. **市场内部状态**:above-MA200 比例、新高新低差、截面离散度、
   **平均两两相关**(危机→1,最快的拥挤温度计)。
5. **跨资产状态**:债券动量(风险偏好)、黄金、VIX/期权隐含、期限利差
   ——§4 第一优先的面板内代理即此类。
6. **状态化**:长窗分位/z → 2-3 档(三档门范式);regime 变量做条件门
   而非直接信号。

### 6.2 检验(坑多于构造侧)

1. **统计功效第一约束**:有效样本=决策事件数而非日数(三档门全程 4 次
   切换=量级原理上不可分辨);检验单位=持有期事件聚合。
2. **干净对照**:同一策略开关择时(vs 恒定满仓轮动),非 vs 买持。
3. **成本与 whipsaw 进报告**:切换次数、双向成本、whipsaw 比例、
   2× 成本压力。
4. **Null = 同频率随机切换**(保频率打方向)+ 时间块 bootstrap。
5. **OOS 衰减实证**:McLean-Pontiff 发表后 −26~58%;CXO 888 策略 IS
   仅解释 OOS 方差 1-2%——walk-forward+逐年分解是底线。
6. **多重检验**:几百个候选指标总有偶然显著(Damodaran);正式工具
   White's Reality Check / Hansen SPA,实务等价 deflation 计账。
7. **训练态=应用态分布**(修正案 #7 教训):门控类策略训练态须历史
   状态机重放,前置门标准件。
8. **逐年符号一致性**(修正案 #11 churn4 教训):两向 AUC 取大在
   regime 翻转年是陷阱。
9. **Regime 分段计量权利金**:熊市赚逃逸/延续段付踏空分别计量
   (G8−G1 证据形状:翻转年 +7pp、延续年 −4.4pp),只报全期 Sharpe
   掩盖结构。

### 6.3 映射到 ETF 线

面板内跨资产代理列(债券/黄金/商品/QDII 动量+平均两两相关+
above-MA200 宽度)按多尺度+状态化+滞后带构造;检验四件套(同频率
随机 null、切换计数与成本压力、逐年符号、regime 分段)+ 两个私有
前置门(训练态重放、决策事件数下限)。

- [Moskowitz, Ooi, Pedersen — Time Series Momentum (JFE 2012)](https://www.sciencedirect.com/science/article/pii/S0304405X11002613)([PDF](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)、[AQR TSM 数据集](https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Factors-Monthly))
- [Quantpedia — Time Series Momentum Effect(线性趋势斜率构造)](https://quantpedia.com/strategies/time-series-momentum-effect)
- [Alpha Architect — Pitfalls When Assessing Market-Timing Strategies](https://alphaarchitect.com/pitfalls-when-assessing-market-timing-strategies/)
- [Quantpedia — In-Sample vs OOS(McLean-Pontiff 衰减)](https://quantpedia.com/in-sample-vs-out-of-sample-analysis-of-trading-strategies/)、[CXO 888 策略 IS/OOS](https://www.cxoadvisory.com/big-ideas/in-sample-vs-out-of-sample-performance-of-888-trading-strategies/)
- [Damodaran — Market Timing Approaches](https://pages.stern.nyu.edu/adamodar/New_Home_Page/invfables/mkttimingapproaches.htm)
