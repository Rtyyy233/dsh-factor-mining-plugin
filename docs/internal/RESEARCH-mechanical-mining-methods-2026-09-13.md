# 机械因子挖掘方法调研(非人工挖掘)

- 日期:2026-09-13
- 范围:自动化/机械化因子搜索的方法版图——演化(GP)/强化学习/GFlowNet/LLM 智能体/全栈闭环,以及**机械系统如何处理海量 M 的准入、同质化与假阴性**(承接 RESEARCH-industry-multiplicity-practice-2026-09-13 的假阴性线)。
- 与前档分工:构造面调研(09-12)按"指导缺口"组织;本档按**搜索范式**组织,聚焦机械系统的选择偏差处理。

---

## 1. 范式谱系(五代,指导来源各不相同)

| 代 | 代表 | 搜索机制 | "指导"从哪来 |
|---|---|---|---|
| 手工(基线) | 101 Alphas | 人 | 经济直觉 |
| **GP 演化** | gplearn / [华泰系列](http://vip.stock.finance.sina.com.cn/q/go.php/vReport_Show/kind/search/rptid/4609942/index.phtml) / [DolphinDB Shark GPLearn](https://dolphindb.cn/solution/detail/mutualfund-quant-research)(GPU 版) | 表达式树交叉/变异+适应度选择 | 适应度(IC/ICIR/互信息/残差收益) |
| **RL** | [AlphaGen](https://arxiv.org/abs/2306.12964) / α² / [AlphaForge](https://ojs.aaai.org/index.php/AAAI/article/view/33365)(AAAI 2025) / AlphaQCM | 策网络逐 token 构造表达式 | 训练出的策略(奖励回灌) |
| **GFlowNet** | [Alpha-GFN](https://github.com/nshen7/alpha-gfn) / **[AlphaSAGE](https://arxiv.org/abs/2509.25055)(ICLR 2026)** | 按奖励**比例**采样分布(非 argmax) | 分布本身(多样模式全覆盖) |
| **LLM/Agent** | Alpha-GPT 系 / [MCTS+FSA](https://arxiv.org/abs/2505.11122) / FactorMAD / [RD-Agent(Q)](https://arxiv.org/html/2505.15155v2)(微软,NeurIPS 2025) / Chain-of-Alpha(已撤稿,慎引) | 生成-评估循环/树搜索/多智能体 | 上下文学习+反馈 |
| 混合 | RL 选 GP 变异算子([Springer 2025](https://link.springer.com/article/10.1007/s10515-025-00501-z));[东吴证券 LLM 增强 GP+函数式双路径](https://www.cs.com.cn/tzjj/jjdt/202503/) | 各取所长 | — |

## 2. 深读四个(与假阴性线最相关)

### 2.1 AlphaSAGE(ICLR 2026):分布采样对 argmax——治 mode collapse 的范式级方案

全文精读要点:

- **核心转向**:普通 RL(含 AlphaGen 的非平稳 MDP)收敛到单一最优模式;GFlowNet 学**覆盖所有高奖励模式的分布** $P(\alpha)\propto R(\alpha)$(轨迹平衡损失训练),天然多样;
- **奖励三项**:$R = R_{IC} + \lambda(T)R_{SA} + \eta(T)R_{NOV}$——主信号 |IC|、**结构-行为对齐项**(RGCN 编码 AST,语义等价的语法变体不再重复计)、**新颖性项 $R_{NOV}=1-\max|\mathrm{IC}(\alpha,\alpha')|$**(与已知优质池的 IC 相关直接进负奖励)——**正交门的奖励化**;
- **组合剥离出奖励**:与 AlphaGen(组合边际做奖励)相反,组合独立成段(AlphaForge 式动态重加权)——搜索层管多样,组合层管定价;
- **理论论证**(附录 E):因子间相关高 → 协方差 Σ 条件数恶化 → 组合方差膨胀——**多样性是组合稳定性的必要条件**(与我们的 deflation 多样性红利互证:实测 R 矩阵越稀疏,bar_sigma 越便宜);
- 消融:GNN 结构编码是最大单项提升(IC 0.046→0.070),novelty/熵正则各再抬一截。

**对我们的直接映射:我们的 random_generate(null 校准)就是一个零先验均匀分布的 GFlowNet;farm 的"top-k IC 排序选种"恰是 AlphaSAGE 批评的 argmax 模式——同质化的机械入口。**

### 2.2 AlphaGen:协同奖励——组合贡献定价的鼻祖

reward = 候选加入下游组合后的组合性能增益(非单因子 IC)——**准入单元=组合**,业界 BRAIN/pod 原则在学术机械挖掘里的对应物。缺陷(AlphaSAGE 指出):贪心构池单一组合,非平稳 MDP,后期趋同。

### 2.3 MCTS+FSA(AAAI 2026,前档已精读,此处只列多重性处理)

频繁子树禁忌(FSA,生成时负约束)/多维反馈含多样性维(与库最大相关)/**对库百分位排名替代绝对阈值**(阈值随库自适应——**墙的稳态化方案**,直接回应我们的 pool_std 漂移问题)/动态预算(突破才加)。

### 2.4 RD-Agent(Q)(微软,NeurIPS 2025):因子+模型全栈闭环

数据因子与模型(LightGBM/Transformer)联合优化,多智能体分工,**最终裁判=下游模型性能**而非单因子统计——把"准入"交给消费端。开源([qlib](https://github.com/microsoft/qlib) / [rdagent docs](https://rdagent.readthedocs.io/en/latest/scens/quant_agent_fin.html))。

## 3. 国内版图(实务侧)

- **私募三路线**(公开材料):幻方(萤火超算,端到端深度学习,因子=神经网络特征,无显式表达式)、九坤("因子挖掘类"代表,北溟超算+AI/Data/执行三实验室)、明汯(统计套利类,数千 GPU,全频率);共同点:**因子生产已完全机械化,人工在数据与基础设施层**([汇总报道](https://fund.10jqka.com.cn/c673796276.shtml));
- **蝶威量化**:宣称同等因子产出,手工投研 90-180 天 → AI Agent 大幅压缩;
- **券商**:东吴"LLM 增强遗传编程+函数式因子"双路径;华泰 GP 系(残差收益目标=正交化在 Y 侧,前档已覆盖);
- **平台**:DolphinDB Shark GPLearn(GPU 加速 gplearn,批量入库+评价框架)。

## 4. 机械系统的多重性处理共识(本档核心结论)

综合全部机械系统(含前档 GP/RL/LLM 各家):

1. **反同质化做进搜索目标,不做事后门**:novelty 奖励(AlphaSAGE)/分布采样(GFlowNet)/子树禁忌(FSA)/经验记忆(FactorMiner)/正则化探索(AlphaAgent)——选择偏差在**生成分布上**被抵消,不是在准入处被罚金;
2. **边际定价在组合层**:AlphaGen 奖励/AlphaForge 动态组合/RD-Agent 下游模型——消费端当裁判;
3. **池层相关性去重**:全员(配合 §2.1 的理论:相关→条件数→方差);
4. **阈值自适应**:MCTS 百分位排名(对库相对),无一家用随 M 单调上涨的绝对墙;
5. **零 per-factor deflation 墙**——与业界调研(机构侧)完全互证:机械挖掘界同样没有人在准入层做统计校正;
6. **假阴性的隐性处理**:机械系统的"拒绝"不是终点——留在池外候选区被组合层反复重选(AlphaForge 每期动态重选),等价新数据/新组合语境再入场。**没有"拒绝即关闭"**。

## 5. 对我们系统的映射(三条可落地的差距)

| 差距 | 证据 | 落地形态 |
|---|---|---|
| **farm 的 argmax 选种** | AlphaSAGE:argmax→mode collapse;我们的 farm.py top-k IC 排序正是此模式 | 选种加 novelty 项(与在册/近期已选的 max |IC| 惩罚)——机械线的反同质化 |
| **随机生成器是零先验分布** | AlphaSAGE:P(α)∝R(α);null landscape 已存 R(α) 样本 | random_generate 加"偏向"模式:按 landscape 分位加权采样算子组合(便宜的半步 GFlowNet) |
| **墙应稳态/自适应** | MCTS 百分位排名;我们墙随 pool_std 漂移 | 与业界调研建议⑤同向:拆墙诊断后,基础墙改对库百分位口径 |

另有一条架构级观察:我们的系统 = LLM 语义生成 + 随机机械生成,但**缺第三代(演化重组)**——trail 里的幸存者之间没有交叉算子(GP/AlphaEvolve 的核心)。F2 家族(精英回灌)任务书部分覆盖;若上,机械重组是零 LLM 成本的第四生产线。

---

## 附:文献清单

| 工作 | 出处 |
|---|---|
| AlphaSAGE(GFlowNet+结构感知+novelty) | [ICLR 2026, arXiv:2509.25055](https://arxiv.org/abs/2509.25055)(全文精读) |
| Alpha-GFN | [github.com/nshen7/alpha-gfn](https://github.com/nshen7/alpha-gfn) |
| AlphaGen / α² / AlphaForge / AlphaQCM | 前档已录(KDD23/arXiv/AAAI25) |
| MCTS+FSA | [AAAI 2026, arXiv:2505.11122](https://arxiv.org/abs/2505.11122)(前档全文精读) |
| RD-Agent(Q) | [arXiv:2505.15155](https://arxiv.org/html/2505.15155v2), NeurIPS 2025;[MSR 中文页](https://www.microsoft.com/en-us/research/articles/rd-agent-quant/?lang=zh-cn) |
| Chain-of-Alpha | arXiv:2508.06312——**已撤稿**,仅记双链架构一说,不采信 |
| GP+RL 混合 | [Springer 2025](https://link.springer.com/article/10.1007/s10515-025-00501-z) |
| 日内风险因子层级演化 | arXiv:2501.07274 |
| 国内私募/平台 | 幻方/九坤/明汯公开材料;[DolphinDB Shark GPLearn](https://dolphindb.cn/solution/detail/mutualfund-quant-research);东吴双路径(中国证券报汇总) |

*检索于 2026-09-13;AlphaSAGE 机制描述取自全文精读,其余按检索摘要;Chain-of-Alpha 已撤稿标注。*
