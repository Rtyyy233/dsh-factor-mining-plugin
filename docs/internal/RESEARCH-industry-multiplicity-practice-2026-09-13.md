# 业界多重检验处理调研 — 实务视角(非学界)

- 日期:2026-09-13
- 背景:用户质询 deflation 墙(现 ≈IC_IR 1.17/1.2)是否造成严重假阴性;上一轮给出了统计诊断(零功率带/pool_std 漂移/统计量丢信息)与修复阶梯;本轮任务:**看业界(真金白银承担后果的机构)怎么处理**,不看学界。
- 方法:web 检索 WorldQuant BRAIN/多经理对冲基金/AQR/国内券商金工/实务派(Rob Carver)/工程平台(米筐/同花顺/DolphinDB);机制描述以检索摘要与官方材料为准。

---

## 0. 核心发现(一句话)

**没有任何一家实务机构在准入层做 per-admission 统计校正(FWER/DSR/deflation)。业界把多重性问题从"推断层"搬走了:M 在设计层被先验压小,边际价值在组合层被计价,证明确实交给样本外与持续监控,假阳性靠成本不对称吸收——不相关的假阳性进池便宜(被组合稀释),相关的才贵(风险集中),所以门开在相关性上,不开在显著性上。**

---

## 1. 五类主体

### 1.1 WorldQuant BRAIN(最大公开 alpha 工厂,与我们同 M 量级)

[准入标准](https://www.scribd.com/document/728780335/World-Quant-Brain-Alpha-Documentation):Sharpe ≥1.25-1.625(按区域/delay)、换手 1-70%、fitness(保密加权复合)、weight/流动性测试。**多重性的主力防线不是这些数字门槛,是相关性双门**:self-correlation <0.7(对自己已交池)、prod-correlation <0.7(对生产池)——[从业者直说"the correlation gate is the real filter"](https://www.reddit.com/r/quant/comments/1v6n30n/worldquant_brain/)。

三个关键设计:

1. **条件门**(官方 [Quantcepts](https://www.facebook.com/WorldQuantCareers/videos/how-do-you-assess-whether-an-alpha-idea-is-strong-this-quantcepts-episode-walks-/1250274476637752/)):"与池内 alpha 的相关应当低,**除非性能显著高于被相关的那些**"——相关者面对更高的墙,不相关者只面对基础墙。多重性被**定价在边际价值上**,不是均匀罚金;
2. **多样性是池的设计目标**:[101 Formulaic Alphas](https://arxiv.org/pdf/1601.00991) 全池平均两两相关仅 15.9%——池的价值来自正交性,不来自单因子显著性;
3. **假阳性哲学=成本不对称**:不相关的假阳性进池后权重被稀释、代价小;冗余相关的假阳性才集中风险。**相关性门限的是假阳性的代价,不是概率**。配套:人工顾问代码评审(过拟合签名检查)+ IQC 保留事后取消资格权([IQC guidelines](https://www.worldquant.com/brain/iqc-guidelines/))——**账本不是终身制**。

注:DSR/B-LP 文献正出自 WorldQuant 轨道(Bailey/López de Prado),但**生产准入门是相关性,不是 DSR**——统计防御在研究叙事里,不在生产闸门上。

### 1.2 多经理对冲基金(pod shops:Millennium/Citadel 系)

新信号的价值 = **对现有账本的增量 PnL**(净掉相关后的边际 Sharpe),不是独立显著性([How Quant Hedge Funds Actually Build and Vet Trading Signals](https://youngandcalculated.substack.com/p/how-quant-hedge-funds-actually-build))。信号间的冗余度(coverage/overlap)与容量是比任何 p 值更硬的约束。**准入单元是组合,不是因子。**

### 1.3 AQR / 因子投资机构

三支柱([AQR 官方](https://www.aqr.com/Insights/Research/Tax-Aware-Investing/AQR-Factor-Research-Papers-Win-Prestigious-Academic-Awards?aqrPDF=1)):**经济直觉 + 强样本外证据 + 跨资产证据**。因子是"论证"出来的不是"筛"出来的——研究员只测自己相信的少数假设,**M 在设计层被先验压小**(每研究员每年几十个假设,不是几千个试验)。[Robeco 压缩因子动物园](https://www.robeco.com/zh-cn/insights/2023/11/honey-we-shrunk-the-factor-zoo):统计+经济双重筛选后**持续监控**,失效即出。

### 1.4 国内券商金工(华泰/东方/海通/国盛)——与我们最近的生态

标准流水线(多源交叉验证):**构造(须有经济逻辑)→ 中性化 → 单因子检验(IC/ICIR、分层单调性、多空、回归)→ 样本外/滚动窗验证 → 入库前相关性去重(只收低相关,如海通[正交化](https://bigquant.com/wiki/doc/XROfU06Eui)、通联[因子库精简](http://qiniu-images.datayes.com/uqer/dongfang10.pdf))→ 合成(最大化 ICIR,[华泰实证](https://crm.htsc.com.cn/doc/2019/10750101/ae933a8c-5a94-4d92-8f33-d92b70c36119.pdf))→ 持续监控(IC 衰减→降权/剔除)**。经验门槛量级:|IC|>0.05、ICIR 考核、分层单调——**没有 deflation,没有 FWER,多重性四件套=先验+样本外+去重+事后淘汰**。工程平台已产品化:米筐([审核管理/相关性矩阵/发布产品库](https://www.ricequant.com/doc/quant/factor-system))、同花顺([因子有效性实时监控](https://quant.10jqka.com.cn/view/help/8))、DolphinDB([批量入库+评价框架](https://docs.dolphindb.cn/zh/tutorials/factor_evaluation_framework.html))。

### 1.5 实务派(Rob Carver,ex-Man AHL)

[Designed not fitted](https://www.slideshare.net/slideshow/trading-strategies-that-are-designed-not-fitted-by-robert-carver-independent-systematic-futures-trader-writer-and-research-consultant/75640564):规则从第一性原理设计,不信回测显著性;稳健性检查(参数扰动、跨品种一致)替代显著性检验([podcast](https://bettersystemtrader.com/026-robert-carver/))。

---

## 2. 业界共识(七条)

1. **M 在设计层控制,不在推断层校正**——先验/经济逻辑压小假设空间,而非事后付显著性罚金;
2. **准入单元是组合**——边际价值(相关性/增量 PnL)才是门,独立显著性不是;
3. **门是条件的**——相关者高墙、不相关者基础墙(BRAIN 原则的通用形态);
4. **小 IC 不是罪**——IR=TC·IC·√BR([Grinold 基本定律](https://www.robeco.com/en-int/insights/2018/04/fundamental-law-of-active-management-shows-way-to-higher-information-ratio)):IC 0.02-0.05 × 大宽度是好生意,**单因子高 IR 墙在业界没有存在理由**;
5. **证明确实交给时间**——样本外+持续监控,失效降权/剔除;**账本是活的,不是一次判死的论文集**;
6. **假阳性靠成本不对称吸收**——不相关的便宜(稀释),相关的贵(集中);所以门开在相关性;
7. **人工/机械评审过拟合签名兜底**(BRAIN 顾问看代码)——我们的对应物:平坦性门/烟测门/同构门已有。

---

## 3. 对照我们的系统

| 维度 | 业界 | 我们 |
|---|---|---|
| 单因子门槛 | Sharpe 1.25 量级(in-sample 区域档)或无硬显著门 | deflated p≤0.05,墙=IC_IR ~1.2 |
| 多重性主力 | 相关性条件门 + 设计层先验 + 样本外 + 事后淘汰 | per-admission E[max\|X\|] deflation |
| 准入单元 | 因子对组合的**边际** | 因子**个体** |
| 墙的时间行为 | **稳态**(相关性门不随历史 M 上涨) | 单调上涨(√(2ln M)+pool_std 漂移) |
| 假阴性处理 | 孵化/监控/降权;错过一个边际因子成本低(组合已有宽度) | 拒绝即关闭(near-miss 靠手工 deepdig) |
| 入册后 | 持续监控,可降权/剔除/取消资格 | 一次性(falsified 池只进不出的语义不同) |

判词:**我们运行着比任何实务机构都严的多重检验制度——严过以真金白银承担假阳性后果的机构。**业界的可辩护立场是决策论的:它们不声称"每只入池因子 95% 真",它们声称"组合的假阳性**成本**有界、且时间会淘汰错货"。这个立场的成立依赖下游存在(真实组合/持续监控)——**我们有**(策略层+test 区),所以业界模型对 我们适用性不低。

反方向也要说:deflation 墙的统计合法性没有错——一只正交的 0.7 也可能是两千次 null 里最幸运的;业界不是在统计上更聪明,而是在**决策上换了计价物**(显著性罚金 → 组合稀释成本+时间检验)。我们的问题不是"墙错了",是"墙是唯一的门,且它随生成器组成漂移上涨,且入册是终身制"。

---

## 4. 业界证据对我们修复阶梯的裁决

| 我方提案(上轮) | 业界裁决 |
|---|---|
| 修复① 统计量增强(年度 Stouffer) | ✅ 对应物=分层单调/稳健性检查——业界当**辅助证据**,不当门;降为证据字段 |
| 修复② 组合准入(正交 near-miss 联合检验) | ✅✅ **业界主流本体**(BRAIN 条件门/pod 增量 PnL 的变体)——升为首选 |
| 修复③ FDR/local fdr | ❌ 业界无人用——学界答案,搁置 |
| 修复④ 残差目标(边际检验) | ✅ pod 标准实践 |
| 调 α / 封顶 M | ❌ 业界不调 α,换判据;封顶 M 毁记账 |
| (新增,业界启发)持续监控+降权+剔除 | ✅ 全体一致做法,我们缺位 |
| (新增,业界启发)条件门:正交者基础墙/相关者高墙 | ✅✅ BRAIN 明文原则;我们的正交门恰好是反着用的(均匀墙+事后相关拦截) |

---

## 5. 落地建议草案(供拍板,未实施)

1. **墙改条件门**(业界原则直译):与在册账本正交(|ρ̄|<0.6)的候选走**基础墙**(deflation 保留但按正交子族的 M 计价,而非全量 M);相关候选走高墙。统计上可辩护:选择运气的计价域应是"该候选从中被选出的那个试验族",不是全部历史;
2. **组合准入第二道门**:互相正交的 near-miss 组(K≥3)联合检验(组合 t≈√Σt²),过门入册为"组合条目";
3. **入册后持续监控**:registry 信号通道挂降权标记,IC 衰减监控→降权→证伪池(**入册不是终身制**——test 锁语义不动,动的只是活账本的权重);
4. **统计量增强降级为证据字段**:年度 Stouffer p 进 diagnosis,不进门;
5. **诊断照跑**:拆墙(bar_sigma vs pool_std 时间序列)+near-miss 结构对照——业界视角强化了它:基础墙应该**稳态**,被生成器组成拖着涨本身就是口径病。

---

*检索于 2026-09-13;主体机制均有来源可核;BRAIN 阈值随区域/delay 变动,取公开文档口径;券商流程为多源交叉的通行情形,个别机构或有出入。*
