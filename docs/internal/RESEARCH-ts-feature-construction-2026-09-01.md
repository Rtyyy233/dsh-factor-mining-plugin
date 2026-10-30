# 调研:业界时序特征的构造实践 — 与截面构造的差异(2026-09-01)

> 任务:用户指令「调研业界对时序特征一般怎么构造、和截面特征有什么
> 不同」。承接同日对话中的概念层讨论(相对 vs 绝对/噪声载体/功效),
> 本报告聚焦**构造实践**层面的证据。来源见 §4。

---

## 0. TL;DR

1. 业界时序特征构造分四大流派:**CTA/趋势规则系**(多回看集成、通道、
   确认共振)、**宏观/估值系**(Goyal-Welch 正统变量集,样本外记录
   惨淡)、**通用 ML 时序工程**(lag/滚动统计范式)、**自动化特征生成**
   (tsfresh 数百候选+假设检验筛选;catch22 从 7700 特征精选 22 个
   跨领域稳定特征)。国内券商金工 ≈ CTA 系的技术指标版(125 指标库
   + 多周期共振合成)。
2. 与截面构造的系统差异(§2 八条):最核心的三条——**标准化基准**
   (截面用当日横截面,时序只能用自身长窗历史)、**信号形态**(截面
   保持连续排序,时序大量符号化/事件化/档位化)、**参数节俭度**
   (时序构造极端少参数 + 强制多窗集成,因为单窗口=参数赌注)。
3. catch22 的选特征方法(跨领域稳定性优先,不拟合单一数据集标签)是
   我们 v2 漏斗 L0-L1 的业界同构印证;Goyal-Welch 两代检验(2008/2024)
   对宏观估值变量的样本外警示,与我们功效预算的结论一致。

## 1. 四大构造流派

### A. CTA / 趋势规则系(最成熟的时序构造实践)

- **基础构造三件套**([Graham Capital 择时入门](https://www.grahamcapital.com/blog/trend-following-primer/)):
  回看收益率(TSMOM)、均线交叉、**通道突破**(现价 vs 自身过去极值,
  Donchian 型)——注意通道的定义域是**标的自己的历史**,不是任何
  横截面。
- **多回看集成是行业共识**:单一窗口 = 对 regime 周期的参数赌注;
  AQR/Man 的实现普遍混合 1/3/6/12 月信号投票或加权([Moskowitz-Ooi-
  Pedersen](https://www.sciencedirect.com/science/article/pii/S0304405X11002613))。
- **统计理论派构造**([Quantpedia — Designing Robust Trend-Following
  System](https://quantpedia.com/designing-robust-trend-following-system/)):
  用回归斜率的 **t 统计量**(而非斜率本身)作信号——自动按噪声归一,
  「趋势显著吗」比「趋势多大」更稳健。
- **确认/共振层**:突破后需 N 日确认、ADX 型趋势强度过滤防假突破
  (业界与国内券商同款,见 D)。
- 构造参数极少(每信号 1-3 个窗口),且 [Alpha Architect](https://alphaarchitect.com/using-trend-following-rules-to-enhance-factor-performance/)
  实证:低频数据(年/月)比日频在数据完整性上更稳健。

### B. 宏观/估值系(Goyal-Welch 正统)

- [Goyal-Welch 2008](https://www.sciencedirect.com/science/article/abs/pii/S1057521918304745)
  检验的 17 个经典择时变量:股息价格比、股息率、盈利价格比、派息率、
  **方差(SVAR)**、账面市值比、**短期利率(TBL)、长期收益率(LTY)、
  长期回报(LTR)、期限利差(TMS)、违约利差(DFY)、违约回报
  (DFR)、通胀(INFL)、CAY 消费财富比**等——估值水平 + 利率结构 +
  宏观状态三组。
- [2024 修订版](https://academic.oup.com/rfs/article/37/11/3490/7749383)
  再检 26 篇后续论文的 29 个新变量:结论不变——绝大多数样本外跑不赢
  历史均值基准。**这组变量是「慢频状态量」的代表:机制清晰、频率低、
  与我们月频门的可观测性匹配,但预测力本身微弱。**

### C. 通用 ML 时序工程(lag/rolling 范式)

- [scikit-learn 时序滞后特征](https://scikit-learn.org/stable/auto_examples/applications/plot_time_series_lagged_features.html)、
  [Microsoft 时序特征工程指南](https://medium.com/data-science-at-microsoft/introduction-to-feature-engineering-for-time-series-forecasting-620aa55fcab0)
  的标准动作:滞后值、滚动统计(均值/std/min/max)、日历特征;
  核心纪律 = 防泄漏(滚动窗右对齐、lag 严格正)。
- 金融特化([CodeSignal 金融 ML 课程](https://codesignal.com/learn/courses/preparing-financial-data-for-machine-learning/lessons/creating-lag-features-for-time-series-prediction)):
  把时序预测转成监督学习的矩阵化——本质与我们「日频状态行 × 未来
  标签」的观测单位重定义相同。
- 局限:通用范式不管经济机制,特征过剩全靠正则化硬扛——在金融低
  信噪比下这正是过拟合温床。

### D. 自动化特征生成(生成-筛选两段式)

- [tsfresh](https://tsfresh.readthedocs.io/)(Neurocomputing 2018,
  引用 1900+):自动计算**数百个**时序特征(峰数、峰位、自相关、
  熵、非线性度量…),再用 **FRESH 算法(可扩展假设检验)**做特征
  选择——生成广、选择严的两段式。
- [catch22](https://github.com/DynamicsAndNeuralSystems/catch22)
  (DMKD 2019):从 ~7700 特征的 hctsa 库中,按**跨数据集稳定分类
  能力**精选 22 个「规范特征」——选择标准是跨领域通用性而非对单一
  数据集的拟合度。**这是我们 v2 漏斗 L0/L1(结构体检/跨载体一致性)
  的业界同构:不烧标签、以稳定性和跨场景一致性预筛。**

### 国内券商金工(CTA 系的技术指标变体)

- [申万宏源《125 个经典技术指标择时分析》](https://m.hibor.com.cn/wap_detail.aspx?id=558b49e63d44511a5b16fbb7d9f297a9):
  构造方法归纳为**均线法/通道法/位置法**三类;除价格反转外多空择时
  年化中位数 13.5-18.6%(样本内/参数内数字)。
- 多周期/多指标**共振合成**是标配([中银黄金多周期合成](https://finance.sina.com.cn/money/future/roll/2025-03-22/doc-ineqntrt8910629.shtml)、
  [华泰波动率换手率牛熊指标](https://bigquant.com/wiki/doc/pxKpzxGNS8))。
- [中金《技术择时的艺术》](https://web.hibor.com.cn/data/f5395ab48f30a3b466e6333f8f9c2c51.html)
  明确警告:技术择时参数多基于历史回测优化,过拟合风险高——与我们
  功效预算推演一致。

## 2. 构造差异:时序 vs 截面(八条系统对照)

| # | 维度 | 截面特征构造 | 时序特征构造 |
|---|---|---|---|
| 1 | **标准化基准** | 当日横截面 z/秩——市场冲击自动消去 | **自身长窗历史**(expanding/rolling z、历史分位)——「和自己的过去比」;基准错=前视(我们的 chip123 事故) |
| 2 | **信号形态** | 保持连续排序直到组合层 | 大量**符号化/事件化/档位化**:金叉死叉、突破、共振计数、多/空/平三态——离散化是降噪手段也是消费单位 |
| 3 | **窗口哲学** | 窗口=信号衰减特性(电池衰减网格可测) | 窗口=regime 周期;**单窗口=参数赌注,强制多窗集成**(1/3/6/12 混合是 CTA 共识) |
| 4 | **噪声处理** | 横截面平均天然分散 | 只能靠**平滑、确认、滞后带、共振**(whipsaw 是时序特有敌人,截面无此问题) |
| 5 | **维度方向** | 沿 N 变异(每天一个分布),特征定义绑定标的池 | 沿 T 变异,**同一构造天然跨载体复用**——跨资产/跨篮子一致性可当筛选器(截面的特征无法这样平移) |
| 6 | **参数节俭度** | 可承受数十算子复合(样本大,门链兜底) | **极端节俭**(每信号 1-3 参数)+ 低频数据偏好;参数邻域平坦性是生死线 |
| 7 | **变量来源** | 行为/流动性异象的横截面差异 | 估值水平、利率结构、宏观状态、自身路径形态——Goyal-Welch 集 + CTA 路径构造 |
| 8 | **检验单位** | 日 × 标的截面 | **事件/regime 段**(切换次数、持有期);共振/确认本身把样本单位从「日」改成「事件」 |

一条总括:**截面构造的全部技巧都在「横截面怎么比」上,时序构造的
全部技巧都在「怎么和自己的过去比且不被噪声甩出来」上**——后者把
大量自由度花在降噪(平滑/确认/多窗/离散化),所以有效参数远少于
表面参数;这也是为什么时序特征「看起来简单」:简单是约束的产物,
不是偷懒。**

## 3. 对我们设计的印证与修正

1. **v2 任务书与业界范式的一致性**:两级生成(基元×结构算子)对应
   tsfresh 的生成段;L0/L1 漏斗对应 catch22 的跨领域稳定性选择;
   L3 计价对应 FRESH 假设检验的选择代价。不需要改架构。
2. **需修正/强化的三点**:
   - v1 四族里所有单窗口特征应**全部多窗化**(20/60/126 三窗集成,
     CTA 共识:单窗=参数赌注);
   - 斜率类构造采用 **t 统计量口径**(Quantpedia 统计理论派)——
     v2 §1.2 已有,升为默认;
   - Goyal-Welch 警示:宏观估值类慢变量在月频门上的预测力微弱且
     样本外惯常失效——跨资产篮子动量(快、日频可观测)优先级维持
     最高,宏观估值列不进 P0。
3. **申万 125 指标的数字是参数内中位数**(年化 13-18%),中金的
   过拟合警告 + 我们的功率预算共同定性:这类结果的样本外折扣应按
   Goyal-Welch 经验(26-58% 衰减,见绝对收益调研)预估。

## 4. 来源

- [Graham Capital — Trend-Following Primer](https://www.grahamcapital.com/blog/trend-following-primer/)
- [Moskowitz, Ooi, Pedersen — Time Series Momentum (JFE 2012)](https://www.sciencedirect.com/science/article/pii/S0304405X11002613)
- [Quantpedia — Designing Robust Trend-Following System](https://quantpedia.com/designing-robust-trend-following-system/)
- [Alpha Architect — Trend-Following Rules to Enhance Factor Performance](https://alphaarchitect.com/using-trend-following-rules-to-enhance-factor-performance/)
- [Goyal & Welch (2008) — Comprehensive Look at Equity Premium Prediction](https://breesefine7110.tulane.edu/wp-content/uploads/sites/16/2015/10/Goyal-and-Welch-2008.pdf)、[Goyal & Welch (2024, RFS)](https://academic.oup.com/rfs/article/37/11/3490/7749383)
- [scikit-learn — Lagged Features for Time Series Forecasting](https://scikit-learn.org/stable/auto_examples/applications/plot_time_series_lagged_features.html)、[Microsoft — Feature Engineering for TS Forecasting](https://medium.com/data-science-at-microsoft/introduction-to-feature-engineering-for-time-series-forecasting-620aa55fcab0)、[CodeSignal — Lag Features for Financial ML](https://codesignal.com/learn/courses/preparing-financial-data-for-machine-learning/lessons/creating-lag-features-for-time-series-prediction)
- [tsfresh](https://tsfresh.readthedocs.io/) / [Christ et al., Neurocomputing 2018](https://www.sciencedirect.com/science/article/pii/S0925231218304843)、[catch22](https://github.com/DynamicsAndNeuralSystems/catch22) / [Lubba et al., DMKD 2019](https://link.springer.com/article/10.1007/s10618-019-00647-x)
- [申万宏源 — 125 个经典技术指标择时分析](https://m.hibor.com.cn/wap_detail.aspx?id=558b49e63d44511a5b16fbb7d9f297a9)、[中金 — 技术择时的艺术](https://web.hibor.com.cn/data/f5395ab48f30a3b466e6333f8f9c2c51.html)、[中银 — 黄金择时多周期合成](https://finance.sina.com.cn/money/future/roll/2025-03-22/doc-ineqntrt8910629.shtml)、[华泰 — 波动率换手率牛熊指标](https://bigquant.com/wiki/doc/pxKpzxGNS8)、[QuantsPlaybook 券商研报复现](https://github.com/hugo2046/QuantsPlaybook)
