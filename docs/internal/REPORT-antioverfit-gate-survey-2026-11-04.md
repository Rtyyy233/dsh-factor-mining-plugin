# 防过拟合门禁设计调研：学术/金融/业界三路（2026-11-04）

> 缘起：2026-11-04 ETF 线三难困境（雕刻旗+双轨门对"持续+均匀"型真溢价定义性全杀，67 发零入册）。
> 问题：放过拟合的门禁到底应该怎么设置？
> 前作：`docs/internal/REPORT-multiple-testing-survey-and-wall-growth-2026-10-07.md`（多重检验校正六防线）——
> 本篇不重复校正数学，专注**门禁架构**：什么该筛查、什么该确证、证据不足时怎么办。

---

## 一、三路调研的核心发现

### 学术线（自适应数据分析 / 序贯检验 / 模型选择过拟合）

1. **可重用 holdout（Dwork, Feldman, Hardt, Pitassi, Reingold, Roth — Science 2015, "The reusable holdout"）**：自适应分析（分析者根据前面的结果选下一个问题）下，holdout 每被查询一次就劣化一点——这就是"selection 区烧穿"的理论形式。解法 Thresholdout：**噪声化回答+过/不过式披露**，可支撑多项式级查询而不失效。([arXiv:1506.02629](https://arxiv.org/abs/1506.02629) / [Google Research 解读](https://research.google/blog/the-reusable-holdout-preserving-validity-in-adaptive-data-analysis))
2. **梯子机制（Blum & Hardt — ICML 2015, "The Ladder"）**：竞赛榜只回答"是否超过此前最好"（1 比特），从机制上让榜单探测无利可图——Kaggle 公榜过拟合的正面解。([PMLR](https://proceedings.mlr.press/v37/blum15.html) / [Ladder.jl](https://github.com/mrtzh/Ladder.jl))
3. **序贯检验 SPRT（Wald）**：证据逐次积累，双阈交叉才判定（接受/拒绝），**中间地带=继续采样**——不存在"证据不足就处决"的一锤子门。现代工业实现：Statsig A/B、Patronus AI 上线门。([Statsig SPRT](https://docs.statsig.com/experiments/advanced-setup/sprt) / [Patronus AI](https://www.patronus.ai/blog/sequential-probability-ratio-test-for-ai-products) / [Stefan & Schönbrodt 2022](https://pmc.ncbi.nlm.nih.gov/articles/PMC9729330))
4. **模型选择过拟合（Cawley & Talbot 2010 经典）**：在同一个数据上反复选模型/调参，最终所选模型的样本内成绩系统性虚高——过拟合不只发生在训练，也发生在**选择过程本身**。这是"雕刻效应"的通用形式。

### 金融线（因子复制危机 / 多重检验 / 样本外衰减）

5. **Harvey, Liu & Zhu（RFS 2016）**：316 个已试因子的历史上，传统 t>2 不再够——新因子须 **t>3**（对已试次数的多重检验校正）。([NBER w20592](https://www.nber.org/system/files/working_documents/w20592/w20592.pdf) / [RFS](https://academic.oup.com/rfs/article/29/1/5/1843824))
6. **Hou, Xue & Zhang（RFS 2020, "Replicating Anomalies"）**：452 个异象按统一协议+校正后重检，**多数失败**（交易摩擦类 96% 失败）——确证性重检是大屠杀。([NBER w23394](https://www.nber.org/system/files/working_papers/w23394/w23394.pdf))
7. **Chen & Zimmermann（RFS 2022, 开源截面资产定价）**：~200 因子温和校正后**多数成立**、发表偏倚不大——与 HXZ 结论对立。**争论本身就是结论**：门槛高度有争议，但两派都不主张用样本内属性做终审；判定一律靠独立数据重跑。([开源项目](https://www.openassetpricing.com) / [GitHub](https://github.com/OpenSourceAP/CrossSection))
8. **Jensen, Kelly & Pedersen（JF 2023, "Is There a Replication Crisis in Finance?"）**：400+ 因子的**贝叶斯层级模型**——单因子证据弱时从全池借力收缩，而非硬阈值处决；结论：多数因子复制后仍有正溢价。**对属性硬门的替代方案**。([JF 全文](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13249) / [NBER w28432](https://www.nber.org/system/files/working_papers/w28432/w28432.pdf))
9. **McLean & Pontiff（JF 2016）**：异象收益入样本→发表后**衰减 58%**（26% 来自统计偏差=纯过拟合部分，32% 来自套利）。**入册线应按"衰减后净值为正"定价；册内跟踪应对照预期衰减曲线**。([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623))

### 业界线（竞赛榜 / 药物审批 / ML 预注册 / 开源框架）

10. **Kaggle 双榜**：公榜（可反复探、必然过拟合）只做过程反馈；私榜只开一次、限选 2 个提交——**筛查与确证物理分离 + 确证次数配额**。([LB probing](https://medium.com/data-science/how-to-lb-probe-on-kaggle-c0aa21458bfe) / [论坛警示](https://www.kaggle.com/general/54610))
11. **FDA 药物审批**：I/II 期筛查（高假阳容忍）→ III 期确证（独立、预注册终点）→ "substantial evidence" 传统要求**两次独立确证试验**；2026 年新规放宽为"一次确证+补充证据"——确证证据的地位反而更强化。**筛查属性永远不构成批准依据**。([FDA 1998](https://www.fda.gov/files/drugs/published/Providing-Clinical-Evidence-of-Effectiveness-for-Human-Drug-and-Biological-Products..pdf) / [新规](https://www.fda.gov/media/133660/download))
12. **ML 预注册/注册报告**（NeurIPS 2021- 工作坊）：假设与评估计划先行落盘、时间戳不可改——我们 journal 预注册纪律的同源实践。([preregister.science](https://preregister.science) / [PMLR v188](https://proceedings.mlr.press))
13. **开源量化框架**：Qlib 系（AlphaBench/FactorBench 2025-26）评估协议=walk-forward+IC/ICIR，**无属性型硬门**——开源生态普遍停留在"正确切分+滚动"，防过拟合靠数据纪律而非信号属性。([Qlib](https://github.com/microsoft/qlib) / [AlphaBench](https://www.cs.cityu.edu.hk/~cliu644/HomePage/doc/AlphaBench/AlphaBench_PDF.pdf) / [walk-forward 框架](https://arxiv.org/html/2512.12924v1))

---

## 二、对三难的回答：全世界没有一门学科用"筛查型属性"做终审

我们的雕刻旗（7/7 一致即旗）和 M13（双轨过度一致即拒）犯的错误，在三个世界各有名字：

| 世界 | 对应错误 | 正确做法 |
|---|---|---|
| 临床 | 用生物标记物（筛查试验）直接拒绝药物 | 标记物异常→**转确证试验**（Phase III 独立数据）；批准只依据确证 |
| 竞赛 | 用公榜分数终审 | 公榜只导航；**私榜一次性确证** |
| 统计 | 用 LR≈1 的属性终审（似然比不足以区分时处决） | SPRT 中间地带=**继续采样积累证据**；JKP=层级收缩而非处决 |

统计根因一句话：**选择雕刻解释了"过拟合因子趋于一致"（成因），但一致性本身不是过拟合的证据（属性）**——对"真·持续+均匀"子类型，两个属性的似然比≈1（null 与真值都机械命中），此时任何以该属性为终审的门都是**类型过滤器**而非真伪判定器。法证数据早有伏笔：高强度 null 的逐年一致性"饱和于全同号"。

---

## 三、综合方案：五层门架构（映射到本系统）

```
层1 筛查墙（固定强度线 z2.65）      ← 流量控制，非真伪判定（FDA I/II 期）
层2 属性旗（雕刻/M13/M8/M3）        ← 全部降级为路由信号：命中→强制层3；不命中→快速通道（SPRT 的 C 态）
层3 独立确证（selection/前向凭据）  ← 唯一终审；alpha-spending 预算+过/不过式披露（Dwork/Ladder/Kaggle 私榜）
层4 层级收缩跟踪（册内）            ← JKP 式层级模型+McLean-Pontiff 衰减曲线；衰减超预期→观察而非除名
层5 滚动重检（季度审计）            ← 前向带延长=SPRT 继续采样（已有）
```

与现状的三个差异点：

1. **层2 降级**（三难的解）：旗命中者不死，转入层3 强制确证——凭据过则旗降级为警告入册（留痕），不过则拒。预算够：路由只作用于墙过者（今日 18 只），selection 预算 60/期仅烧 1。
2. **层3 披露收紧**：selection 检查对模型的反馈从连续 IR 读数改为过/不过+余量档位（Thresholdout/Ladder 教训：连续披露加速烧穿 holdout）。研究需要的数字走人读报表，不进对话回路。
3. **层4 新增**：入册后按全池层级模型跟踪（弱证据者收缩权重而非除名），衰减对照 26-58% 预测带。

## 四、不建议采纳的

- **t>3 式高墙**（HLZ 路线）：我们的法证已实证增长墙 80-85% 误杀前向稳健——HXZ/CZ 之争说明门槛高度本身有争议，JKP/CZ 的"温和校正+复制优先"支持低墙+强确证（我们现行 z2.65 方向正确）。
- **完全 1 比特披露**：研究会失去必要的分辨力；折中=档位披露（过/边际/不过三档）。
- **按属性调阈值**：治标不治本——似然比≈1 的属性调到哪都是类型过滤器。

## 五、结论

三难不是参数问题，是**把筛查当确证用**的架构问题。业界共识清晰：属性门只配路由，独立数据才配终审；holdout 按预算消耗、按比特披露；册内用收缩而非处决。落地方案=上文五层架构的前三条改造，工作量集中在层2 降级与层3 披露形态，均不动"固定不涨"语义。
