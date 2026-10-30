# RESEARCH: 随机搜索空间扩充路线调研

- 日期：2026-09-18
- 触发：用户拟扩充随机因子生成器（farm/randgen）的算子集或以其它方法扩充随机搜索空间，先调研怎么做。
- 前档衔接：`RESEARCH-mechanical-mining-methods-2026-09-13.md`（五代谱系；已列三条差距：novelty 选种〔已落 P0a λ=0.3〕/ landscape 加权 / 第三代演化重组缺失〔本档 R6〕）。本档是该线的直接续篇。
- 结论先行：**技术钩子全套现成（算子注册表单一真值源），但有一条致命缝必须先修——null 地形指纹不含算子集，扩了算子旧墙口径会静默漂移。路线推荐：类型文法打底 → 条件结构+算子类别主攻 → 覆盖档案做计量 → 字段腿按数据工程节奏 → 交叉重组暂缓 → 论文/大模型提议作为词表长期生长通道。**

---

## 1. 现状底座（生成器实况）

核心三件：`python/src/dsh_factor_mining/factor/random_gen.py`（611 行，生成逻辑）、`python/src/dsh_factor_mining/factor/ops.py`（225 行，算子真值源）、`analysis/glm_mining/farm.py`（95 行，外层循环）。bridge 入口 `_factor_random_generate`（bridge.py:5275-5506），双模式：`explore`（挖矿）与 `null-calibration`（墙校准）——**同一个生成器两用，扩算子必然同时改两者**。

| 维度 | 现状 |
|---|---|
| 算子池 | 36 个：时序 17（含双输入 corr/cov/vol_ratio）+ 截面 4（rank/zscore/winsorize/scale）+ 二元 7（含保号 div）+ 单目 8（ops.py:190-219） |
| 叶子字段 | 6 个：o/h/l/c/v/amount（ops.py:224） |
| 结构约束 | 深度 3..5（边界可到 6）；**强制 ≥1 时序 + ≥1 截面**；binary 两子树不全叶（random_gen.py:120-229） |
| 类型系统 | **无**。只有元数+参数类别检查，cs/ts 可任意嵌套（产物常见 `cs_winsorize(cs_winsorize(...))`，FARM_REVIEW_PENDING.md:14） |
| 采样 | 全均匀（`rng.choice`）；binary 双权是唯一加权；**无按算子权重机制**（operator_set.json 只支持 disable/windows/delays，不支持加权，更不支持新增——random_gen.py:53-80） |
| 选种 | 生成后经 `_novelty_greedy` 反同质化贪心 λ=0.3（选择层，bridge.py:5363-5446；历史持久化 explore_seeds.json） |
| 防伪 | explore 与 null 校准同 seed 拒绝（防前缀重放假发现，bridge.py:5319-5326） |
| 诚实边界 | 模块自述"curated 算子集是人类先验……不是无偏搜索"（random_gen.py:19-20） |

farm 运营轨迹：09-13 拍板单进程退役 → 09-16/17 夜巡期作为配额供数+新域探测通道复跑（424 成员 2 热树）；ETF 线 60+ 周期零带内（23 标的低维宇宙，已建议降配额或改结构化生成）。**null-calibration 模式永久承重**：pool_std（墙的地形读数）取自该模式的产出（bridge.py:3639-3651）。

战役侧的经验事实（扩充方向的实证依据）：收官战役六入册全部来自加腿/拆杠杆；七元腿（ICIR 1.822）与量腿加权三度兑现均为手写构造——**当前深度 3-5 的语法树根本长不出这些结构**。随机空间与高产出结构之间的缺口就是本调研要量的东西。

---

## 2. 四条硬缝（扩充前必须处理）

### 缝 1（致命）：null 指纹不含算子集
null 地形指纹 = `full_fingerprint(数据文件, calibration) + v{ENGINE_VERSION}`（discipline.py:68-70），而 `ENGINE_VERSION = "0.1.0"` 是静态串（`__init__.py`）。**OPERATOR_REGISTRY 变更与 operator_set.json 覆盖都不进指纹**——扩充算子后旧 null_landscape 仍判 match，pool_std 不重算，墙的口径静默漂移；且 null 校准用的是同一个生成器，扩算子后 null 分布本身就该变。修法两档：
- 便宜修：每次动 ops.py 手动 bump ENGINE_VERSION（纪律依赖人记）。
- 正修：把 operator_set 的内容 hash 并进 calibration 指纹（discipline.py:40-60 处），一次工程永久生效。

### 缝 2：病态防护三缺口
- `ts_momentum = x/x.shift(w)-1` **无零保护**（ops.py:93-94）——除法族里唯一不设防的；
- 生成树**无输出健康检查**：inf/NaN 树不报错，进 IC 计算被 mask 静默吃掉（random_gen.py:461-464 except continue）；
- explore 模式不按 env.amount 可用性过滤叶子（bridge.py:5349 未传 leaves）——无 amount 的环境上含 amount 的树整棵报 error。
扩算子会放大这三条（新算子=新病态面）。修法：ts_momentum 补保号除法；生成后加 isfinite 比例+方差健康门（glm_mining 侧已有非强制工具 ast_check_night13.py，可内置化）；explore 传 leaves 过滤。

### 缝 3：无类型系统 → 垃圾密度随算子数超线性上升
现在 36 算子已有废树（cs 套 cs）。扩到 60+ 而无类型约束，有效组合占比会塌。类型文法（R4）是 R1/R2 的前置工程，不是可选项。

### 缝 4：墙自动升高（经济学，非缺陷）
deflation 的 bar_sigma = E[max|X|] 是**单调不减定理**（evaluate.py:426-429）：新算子产物进 trail → R 矩阵加列 → 墙升高。扩空间从来不是免费的——短期入册率必然再降。诚实的定价：当前空间的随机边际本就≈零（312 随机 0 入册、664+ 成员零热树），扩空间损失的不是产量是算力+校准成本；**随机通道的考核指标应按重定位后的角色设为热树/新域/供数质量，不是入册数**。

---

## 3. 六条扩充路线

### R1 算子池扩充（用户点名的主线）
文献参照：WorldQuant BRAIN 的 Fast Expression 算子目录（社区整理版在 Scribd/Studocu，[101 Alphas](https://arxiv.org/abs/1601.00991) 是经典公式库）；层级遗传算法论文附录有逐算子数学定义（[SSRN](https://papers.ssrn.com)）。对照现状的类别缺口：

| 缺口类别 | 候选算子 | 依据 |
|---|---|---|
| 回归残差族 | `ts_regression(y,x,w,rettype=resid/coef/r2)` | 现 ts_corr/ts_cov 是其特例；七元腿的"负相关对冲腿"本质是残差结构的手工实现 |
| 衰减族 | `ts_decay_exp_window` | 现只有线性衰减 |
| 换手/变化族 | `hump`（小变动抑制）、`days_from_last_change` | 换手率定价线（已调研待拍）的机械化入口 |
| 高阶共矩族 | `ts_co_skewness`、`ts_triple_corr` | 尾部相依战役手工构造的机械化 |
| 分组族 | `group_rank/group_neutralize/group_zscore` | **依赖分组字段**（行业/市值桶），与 R3 绑定 |
| 条件/逻辑族 | `if(cond,a,b)`、比较算子 | 与 R2 重叠，见下 |

注意：**operator_set.json 不能新增算子**，扩充=ops.py 代码级新增函数+注册表条目，随后必须触发缝 1 的指纹变更与 null 重校准。

### R2 结构扩充（战役证据最直接）
- **条件三分支**：当前语法树完全没有分支结构。六入册全是"条件门+腿"形态（三档仓位择时门、winsor 单杠杆、量腿加权）——随机树连表达都不能表达，这是比算子数量更大的缺口。
- **腿数/深度扩容**：深度 3-5 长不出七腿。深树=垃圾密度上升，必须 R4+缝 2 健康门配套。
- **骨架模板（带槽模板）**：把已兑现结构做成槽位模板（量腿加权骨架、对冲腿骨架），槽内随机。**定位须诚实**：这是结构先验，不是零先验——它是介于随机与手写之间的第三通道，建议在台账里与纯随机产物分 origin 记账（现有 origin=random/hypothesis 二分可扩 random-template）。
- 文献呼应：[Shin et al. 2024](https://arxiv.org/abs/2401.02710)（Synergistic Formulaic Alpha Generation）正是"扩充搜索空间+用已有表达式做种子"两招；[AlphaGen（KDD 2023）](https://github.com/ICT-FinD-Lab/alphagen)的 token-by-token 构建本质就是文法生成。

### R3 字段腿扩充（无偏性最优的通道）
新叶子：分钟频数据（`~/.a_share_minute` 已开工）、基金持仓、行业/市值分组字段、换手率。这是 09-18 无偏性审计结论的直接落地——**字段层不带世界观，是数据供应商的枚举**；盲区覆盖最大。成本在数据工程与 null 重算，不在生成器。分组字段落地后解锁 R1 分组族。

### R4 类型文法（R1/R2 的前置工程）
谱系：Grammar-Guided GP（G3P）、[RBG2-SR（CFG 作 RL 动作空间）](https://arxiv.org/abs/2202.04367)、PGE（确定性文法枚举）、2025 年新出的[语义约束 SR](https://link.springer.com)（值域/单位约束——与本系统"尺度卫兵"待办天然合流）。本系统落地：给算子加输出类型标注（时序量/截面量/原始量），生成时按类型约束组合，消灭 cs 套 cs 类废树。工程量集中在 random_gen.py 的采样表改成类型驱动。

### R5 覆盖档案（质量多样性机制的零先验改造）
谱系：[MAP-Elites（Mouret & Clune 2015）](https://quality-diversity.github.io)、[VQ-Elites 2025](https://arxiv.org/abs/2510.05022)、[MAP-Elites+GP 用于符号回归集成](https://www.researchgate.net/publication/369857270)。**本系统的改造点**：不取 MAP-Elites 的精英繁衍（那是追分，破坏零先验），只取**档案+分箱**——描述子 =（数据腿集合，算子族直方图），箱内均匀采样+每箱最低配额。效果：均匀但不重采样；与选择层的 novelty 贪心（λ=0.3）互补成生成层 counterpart；描述子恰好就是无偏性审计里"字段覆盖图"的生成器版。与"测量不引导"红线完全一致——这是给机器配额，不是给模型喂方向。

### R6 交叉重组（前档已列缺口，建议暂缓）
GP 经典交叉 + [LLM 做交叉算子](https://arxiv.org/abs/2401.07151)（LLM_GP、LEAR、[ALFA 组教程](https://github.com/alfape/LLM_GP)）都成熟。但幸存者交叉=argmax 压缩机的机械化——正是无偏性审计定的同质化入口。若做：必须与 R5 绑定（交叉产物按描述子入箱计量，同质化可见）+ 与 novelty 选种同池。**建议等 R5 计量上线后再评估**。

### R7 词表外部注入（词表长期生长通道）
论文/大模型提议新算子候选 → 机器验证（数值面板测试+烟测缩放+病态检查）→ 入册 OPERATOR_REGISTRY → 触发 null 重校准。与"注入=遥测非指令"红线相容：**给生成器喂词表，不是给模型喂方向；提议者在外，验证是机械的**。arxiv 通道已修（curl 优先传输）；IVOL 是该通道已验证的概念注入先例，算子注入是同一通道的加深。可设独立配额（如每周期 1 个候选）防词表膨胀。

---

## 4. 推荐组合与顺序

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P0 前置必修 | 缝 1（指纹）+ 缝 2（病态三件套） | 无——不扩空间也该修 |
| P1 主攻 | R4 类型文法 + R2 条件结构（if 三分支优先，骨架模板次之） | P0；战役证据最直接 |
| P2 第一批算子+计量 | R1 无新字段依赖的类别（回归残差/衰减/换手族）+ R5 覆盖档案 | P1（类型系统） |
| P3 字段腿 | R3（分钟/持仓/分组字段）+ R1 分组族 | 数据工程节奏 |
| P4 观察 | R6 交叉重组（等 R5 数据）、R7 常态化 | R5 上线一周期 |

**每次扩充的标准动作**（写成检查单）：ops.py 入册 → 指纹变更生效 → null 重校准（n 棵×全 horizon）→ 墙读数刷新 → 台账记录 bar_sigma 变化（会升，这是诚实代价，不是事故）→ 病态率对比（error/inf 树比例不得高于扩充前）。

## 5. 验收指标
- 箱覆盖率：描述子空间的访问比例（R5 上线后天然可得）
- 新域命中：域级候选出现数（IVOL 式判定：单独使用年度一致的未开垦域）
- null 重算幅度：pool_std 变化 <30% 为温和扩，>50% 说明扩进了高地（墙大涨，需评估是否分线记账）
- 病态率不升；explore 每周期 error 树占比下降（缝 2 修复的验证）
- ETF 线单独评估：23 标的低维宇宙，R2/R5 优先于 R1（分组族无分组字段可用）

## 6. 决策点（待拍板）
- D1：缝 1 修法选档——ENGINE_VERSION 手动 bump（便宜）vs operator_set hash 进指纹（正修，推荐）
- D2：P1 先行确认（类型文法+条件结构）
- D3：骨架模板的定位——farm 内新模式 vs 独立第三通道（涉 origin 记账三分）
- D4：R6 暂缓确认
- D5：null 重校准节奏——扩一次跑一次 vs 攒批（涉及校准成本与墙稳定性的权衡；前档"墙稳态化"缺口的正反面）

## 参考来源
- [AlphaGen: Generating Synergistic Formulaic Alpha Collections via RL (KDD 2023)](https://github.com/ICT-FinD-Lab/alphagen)
- [Shin et al. 2024: Synergistic Formulaic Alpha Generation（扩搜索空间+预训练种子）](https://arxiv.org/abs/2401.02710)
- [RiskMiner 2024](https://dl.acm.org/doi/10.1145/3627673.3679209)、AlphaQCM（ICML）——RL 谱系后续
- [RBG2-SR: 文法引导的 RL 符号回归](https://arxiv.org/abs/2202.04367)
- [G3P 综述（EmergentMind）](https://www.emergentmind.com/topics/evolutionary-computation/grammar-guided-genetic-programming)、[Reissmann et al. 2025 语义约束 SR](https://link.springer.com/article/10.1007/s10710-025-09561-2)
- [MAP-Elites / Quality-Diversity 论文集](https://quality-diversity.github.io)、[Mouret & Clune 主页](https://members.loria.fr/JBMouret/)、[MAP-Elites+GP 符号回归集成](https://www.researchgate.net/publication/369857270)
- [Wang et al. 2025: When LLMs Meet Evolutionary Algorithms（综述）](https://pmc.ncbi.nlm.nih.gov/articles/PMC11890758/)、[Hemberg et al. 2024: Evolving Code with an LLM](https://arxiv.org/abs/2401.07151)
- WorldQuant BRAIN Fast Expression 算子目录（社区版：Scribd "World Quant Brain Alpha Documentation"、"Research On Each of These WorldQuant Brain Operators"；[101 Formulaic Alphas](https://arxiv.org/abs/1601.00991)）
