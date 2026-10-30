# 现行门槛总账：主线与候补线（2026-09-13）

- 范围：当前代码里**实际在跑**的每一道门槛——怎么算、公式是什么、数字从哪来。含 2026-09-13 假阴性补救批次落地后的候补线；含影子层（只算不拦）。
- 代码基线：worktree `residual-holding-correlation`（已 cherry-pick 生产主树，与部署版一致）。
- 生产有两根独立的挖掘线（各自独立 stateRoot）：**IC 线**（`.factor-mining-stock`）与 **尾部线**（`h10c30`）。每根线内部都有 IC 轨/尾轨双轨判定；文中"墙"的生产快照按两线分别给出。

## 0. 一图总览

```
生成端(不是门,是奖励)
  反同质化贪心选种 λ=0.3
        │
        ▼
┌─ 主线准入(提交期) ──────────────────────────────┐
│ IC 轨: deflated p ≤ 0.05   ← 计价核心 E[max|X|]×pool_std │
│ 尾轨:  G1 placebo ≥ 3 → G2 |z|<3 → G3 spread_ir ≥ bar │
│ 共同硬门: 噪声门 |z|≥3 拒 / 平坦性悬崖拒 / 正交门软门(annotate) │
│ 铁律: 同因子/同名不重复登记;程序性拒绝可治愈              │
└──────────────┬───────────────────────────────┘
               │ 未过线且 dev 优异
               ▼
┌─ 候补线(假阴性补救) ────────────────────────────┐
│ 备用池: |sr_hat| ≥ 0.75, 族内去重 |ρ|≥0.6          │
│ 通道① 重定价赦免(影子,待批次2翻转)                  │
│ 通道③ 规则化组合(两两|ρ|<0.3 取前k, dof=1)          │
│ 通道⑤ 前向孵化(120日新鲜窗, z=Φ⁻¹(1−0.0253/K))      │
│   └ 幸存者 → submit 豁免 dev deflated p(其余门照旧)  │
└────────────────────────────────────────────┘
```

## 1. 计价核心：选择运气采样器（两线共用）

**它回答的问题**：试了 $M$ 个因子、允许彼此相关，纯靠运气能挑出的最大假信号有多强？这道"运气底价"从每个因子的显著性里扣除。

### 1.1 试验账本与单元

- 账本 = `trail_engine.json`，评估即写（`verdict=fail` 也算——选择偏差发生在评估时刻，不在注册时刻）。
- 试验单元 = `(source_hash, horizon)`；同因子同 horizon 重评 = 更新不新增。`bridge.py:3518-3548`
- 尾部线另有独立账本：单元 = `(source_hash, horizon, k_frac, cost_model_version)`，只收 development/batch/composite 阶段（test 是消耗品，进计价=泄漏）。`tailgate.py:51-91`

### 1.2 相关矩阵 R 的构造（v4，2026-09-13 计价审计后）

$$
R_{ij} = \frac{\operatorname{cov}(\text{sketch}_i^{(-L)},\ \text{sketch}_j^{(-L)})}{\sigma_i \sigma_j},\qquad L = \min_k \|\text{sketch}_k\|
$$

- 所有点数 ≥ 60 的 IC 序列草稿（sketch）**截到公共尾长 $L$，做一次 Gram**——单一 Gram 矩阵天然半正定（PSD by construction），不依赖投影。`evaluate.py:456-504`
- **签名相关**（保留 ± 号）：± 对 E[max|X|] 同价，符号携带族间抵消结构（动量↔反转）。
- 短草稿与重叠不足对**不进 R**；缺测对 → 跨 horizon 先验 → 0（独立 = 最贵假设，缺证据 = 保守）。
- 事故背景（为什么这么构造）：旧版逐对尾对齐（每对用不同重叠段）+ `abs()` 抹符号 → R 带 1360 个负特征值 → PSD 投影造出巨型伪共同因子 → bar 被推到 4.58（超独立上限）。详见 `REPORT-p0-estimation-2026-09-13.md`。

### 1.3 门参数 bar_sigma = E[max|X|]

$$
\bar\sigma_M = \mathbb{E}\!\left[\max_{i \le M} |X_i|\right],\qquad X \sim \mathcal N(0, R)
$$

- 实现：公共随机数（CRN）条件采样，$K=20{,}000$ 样本，$Z$ 列固定种子；增列 $x_{new} = X v + \sqrt{s}\,z$（$v = R^{-1}r$，$s = 1 - r^\top v$）。`evaluate.py:418-440, 532-555`
- **单调性定理（防灌水）**：CRN 下试验集只增，逐样本 max 只增不减 → $\bar\sigma$ 天然单调。旧"包络棘轮"（章回读 max）已摘除（2026-09-13）——它是单向棘轮，曾把构造事故的高估永久锁死。`bridge.py:3504-3524`
- 冷启动底价：$M=1$ 时 $\bar\sigma = \mathbb E|Z| = \sqrt{2/\pi} \approx 0.798$（连符号都是选出来的）。
- 附带产出：$p_{fw}(z) = P(\max|X| \ge z)$（诊断字段，非门）；$\nu_i = 1/\operatorname{diag}(R^{-1})_i$（试验残差方差占比，探索多样性遥测）。

### 1.4 pool_std：把 σ 单位换算成 IC_IR 单位

$$
\text{pool\_std} = \operatorname{std}\big(\text{IC\_IR of null 因子}\big)_{\text{per-horizon, 指纹门后}}
$$

- 来源**只有** null 地形校准（landscape，按 horizon 分键、环境指纹校验通过才用）；v4 起 trail 实测 std 不进门（找到真信号会把 null 撑宽 → 信号越好门越高的反激励）。`bridge.py:3836-3880`
- **选运底价（运气在 IC_IR 尺度上的水平）**：

$$
sr_0 = \bar\sigma_M \cdot \text{pool\_std}
$$

- 缺 pool_std 且 $\bar\sigma > 0$ → **拒绝给出 p**（不给不可信数字）。

### 1.5 生产快照（2026-09-13，P0c 计价修复后估值）

| | IC 线 | 尾部线 |
|---|---|---|
| 账上 bar_sigma（棘轮时代，已废） | 4.578 | 4.303 |
| 实测 R 的诚实 E[max\|X\|] | **3.345** | **3.457** |
| 零相关 iid 上限（参照） | ≈4.35 | ≈4.33 |
| **墙 = bar × pool_std（IC_IR 单位）** | **≈1.09**（原 1.49） | **≈0.95**（原 1.18） |

> 墙是**活算的**（每次评估按当前 trail 重算），上表是修复时的生产快照估值；实际数字看当次 evaluate 响应里的 `bar_sigma`/`sr0` 字段。iid 上限的解析近似（B-LP，单侧）：

$$
\mathbb E\big[\max_{i\le N} X_i\big] \approx (1-\gamma)\,\Phi^{-1}\!\Big(1-\tfrac1N\Big) + \gamma\,\Phi^{-1}\!\Big(1-\tfrac1{Ne}\Big),\qquad \gamma \approx 0.5772
$$

（双侧 $|Z|$ 版把 $N$ 换成 $2M$ 即可；尾部线 G3 用的正是这个单侧解析式，见 §3.4。）

## 2. 主线 · IC 轨（deflated p 链）

### 2.1 单因子统计量（dev 窗口）

$$
\widehat{sr} = \frac{\overline{IC}}{\operatorname{std}(IC)},\qquad
g_3 = \widehat{\text{skew}},\quad g_4 = \widehat{\text{kurt}},\quad n = |\text{IC 日数}|
$$

### 2.2 Deflated p（矩修正， Bailey & López de Prado 口径）

$$
t = \frac{\big(|\widehat{sr}| - sr_0\big)\sqrt{n-1}}{\sqrt{\max\!\big(1 - g_3 \widehat{sr} + \tfrac{g_4 - 1}{4}\widehat{sr}^2,\ 10^{-8}\big)}},\qquad
p = \tfrac12\,\operatorname{erfc}\!\Big(\tfrac{t}{\sqrt2}\Big)
$$

`gates.py:106-126`。分母是偏度/峰度对 t 统计量方差的修正：IC 日收益若有厚尾/不对称，普通 t 会高估显著性。

**白话直觉**：先从你的 IC_IR 里扣掉"试了这么多试验、运气本应给出的最大值"（$sr_0$），剩下的部分再看它在 $n$ 天样本下还显不显著。试得越多（$M$ 越大）或试验越彼此独立（$\bar\sigma$ 越高），扣得越多，门槛自动升高；试验高度同质（族内相关高）则折扣小。

### 2.3 验收判定 `passes_acceptance`（`evaluate.py:1239-1265`）

| 门 | 判据 | 拒绝语 |
|---|---|---|
| 截面置换 | \|column-perm z\| ≥ 3 | 截面结构不显著 |
| beta 伪装 | \|beta\| < 0.3 | beta 暴露超标 |
| 样本 | IC 日数 n ≥ 20 | 样本不足 |
| **多重检验** | **deflated p ≤ α = 0.05** | 选择运气不可排除 |
| p 不可算 | p=None 一律拒（M=1 豁免已取消） | 先跑 null 校准 |

### 2.4 提交期重算（A3，堵冻结诊断绕过）

submit 时刻用**当时**的 trail 重算 p（诊断生成后的新试验全部计入）；充分统计量 $(\widehat{sr}, g_3, g_4, n)$ 优先取 trail_engine 的引擎侧权威记录，diagnosis 自报值仅在无记录时兜底（`receipt_verified=False` 标注不可信）。`bridge.py:5861-5919`

### 2.5 red_flags（判完统计量之后的纪律门）

红旗在场 → 拒收。例：test 区 IC_IR 显著优于 train（regime/前视红旗，`discipline.py:239-289`）。

## 3. 主线 · 尾轨（G1→G2→G3，`tailgate.py`）

尾部线准入三连门，顺序硬编码；IC 轨提交时也跑尾轨判定但只作 `tracks.tail` 标注（双过 `dual_pass` 是质量标注）。

### 3.1 有效试验数 N_eff（尾部线专用）

$$
N_{eff} = \#\{\text{聚类}\},\quad \text{聚类规则：MinHash Jaccard} \ge 0.25 \text{ 的试验并类}
$$

- 选择集 = {(采样日, top-K 资产)} 对的 32 哈希 MinHash 签名（128B/试验）；IC 相关 0.75 的两因子尾部名单可只重叠 0.4——家族几何必须按名单算。
- 无签名的旧条目按独立计（保守：$N_{eff}$ 只多不少）。

### 3.2 G1：placebo 门

$$
z_{placebo} \ge 3
$$

top-N 组合**净超额**（扣成本）对随机选股 null 的 z；权威值在 submit 用足样本重跑（draws ≥ 60，默认 120，预算 180s 自适应截断，缓存命中零成本），evaluate 轻量值（≈10 draws，σ 误差 ~24%）只作降级兜底并标 degraded。`tailgate.py:317-329`

### 3.3 G2：spread 噪声门

$$
|z_{spread\text{-}noise}| < 3
$$

合成噪声世界上的 spread 统计量 z（**恒毛口径**——net 口径下随机世界均值有确定性成本拖累，z 会全员爆负，不是 artifact）；z 不可判定 = fail-closed（尾轨事务中止，不烧名）。`tailgate.py:330-344`

### 3.4 G3：deflation 门（跨轨 Šidák）

$$
\alpha_{track} = 1 - (1-0.05)^{1/2} \approx 0.0253
$$

（双通道准入 = 两次通过机会，家族 0.05 对半分裂。）

$$
s_0 = \begin{cases} \text{landscape spread 段经验 std} & \text{（指纹门内，empirical）} \\ 1/\sqrt{n_{days}} & \text{（降级解析式，analytic）} \end{cases}
\qquad
sr_0 = \mathbb E[\max X](N_{eff}) \cdot s_0
$$

- 矩修正版（spread_moments 在场，判定走此式）：

$$
t_{adj} = \frac{(\text{spread\_ir} - sr_0)\sqrt{n-1}}{\sqrt{1 - g_3\,\text{spread\_ir} + \tfrac{g_4-1}{4}\,\text{spread\_ir}^2}} \ \ge\ \Phi^{-1}(1-\alpha_{track}) \approx 1.955
$$

- legacy 版（缺矩）：$\text{spread\_ir} \ge sr_0 + z_{\alpha}\, s_0$。
- 降级**不拒判**（与 IC 轨 pool_std 缺失即拒不同）：解析式有理论依据（null 下 $\text{spread\_ir} \sim N(0, 1/\sqrt n)$）且 G1 前置门在场。
- 判定基开关 `tail_net_basis`：True 时 G3 判 `net_spread_ir`（成本后口径，矩同口径切换）；默认 false = 毛口径。

**白话直觉**：G1 先证明"这个尾部组差不是随机选股也能拿到的"；G2 证明"它不是评价管道自己在噪声上也能造出来的假象"；G3 才问"考虑到尾部线已试过 $N_{eff}$ 个彼此相关的名单，这个 spread_ir 还够不够突出"。三问各自独立，缺一不可。

## 4. 主线 · 共同硬门（两轨提交都过）

### 4.1 噪声硬门

$$
z = \frac{\overline{\text{stat}}_{\text{worlds}}}{\operatorname{std}(\text{stat})/\sqrt m},\quad m \approx 50 \text{ 个噪声世界}，\quad |z| \ge 3 \Rightarrow \text{无条件拒收}
$$

真 alpha 按构造不可预测噪声；噪声上仍显著 = 因子公式在拟合评价 artifact（管道偏差/重叠窗构造）。无法判定（有效世界不足）= **事务中止**（不落盘、不烧名、可重试）；基础设施失败同语义。`noise.py:199-256`、`bridge.py:5961-6080`

### 4.2 参数平坦性门（申报制）

- 提交时申报 ≤ 4 个可调参数 `{name, value, step}`；引擎自主在最小步长邻域重评。
- **悬崖签名**（符号翻转/塌陷/退化）→ 硬拒收："真实 alpha 在参数空间是平滑峰，最小步长不该有断崖；断崖 = 调参贴噪声"。
- 申报不实（value 不在 source 中）→ 事务中止。校准实证：在册 19 因子 0/19 假阳性。`bridge.py:6096-6132`

### 4.3 正交门（2026-09-06 上线；**2026-09-10 用户裁决降级为软门，现行语义**）

$$
\bar\rho_j = \operatorname{mean}_{d}\Big(\operatorname{Pearson}\big(\text{rank}_{d}^{\text{cand}},\ \text{rank}_{d}^{(j)}\big)\Big)_{\text{公共有效}\,\ge\,300},\qquad
\max_j |\bar\rho_j| \begin{cases} \ge 0.80 & \text{annotate+hint（软门，不烧名——test 区仲裁）} \\ \ge 0.60 & \text{标注} \\ < 0.60 & \text{放行} \end{cases}
$$

- **软门理由（2026-09-10 用户决策）**：深挖式重组（绑已知强腿过 deflation 墙）与正交硬拒结构性冲突，前者恰是新域唯一现实入口；冗余的最终仲裁在 test 区。`ortho_hard_gate=True` 可恢复硬拒（拒收+烧名）。
- 09-13~09-14 曾因两树分叉以硬门运行一晚（烧名一次），2026-09-15 用户复裁恢复软门，两树归一。
- 候选秩 vs 在册账本逐日秩缓存（uint16，逐日矩阵乘 <2s）。**白话**：显著性门回答"它自己是不是真的"，正交门回答"账本是不是已经知道它了"——deflation 对试验**次数**定价，本门对账本**有效维度**定价。`book_ranks.py:127-172`

### 4.4 day-perm：永久 report-only

校准实证 19/19 好因子全部 alignment-dependent（p 小 = 真短周期因子与时序过拟合的并集，方向不可判）→ 只做分解诊断，不做拒收门。`bridge.py:6118-6123`

### 4.5 铁律与程序性治愈

- 同一因子（source_hash）不得重复登记为新发现；同一名字只允许一条 entry。
- **程序性拒绝**（基础设施失败/尾块反查失败——因子从未被真正评审）可治愈：删除旧条目放行重试；**实质性拒绝**（门真判了）烧名。`bridge.py:5805-5860`
- source/diagnosis 一致性硬校验：提交 source 的 hash ≠ 诊断 `_meta.source_hash` → 事务性拒绝；diagnosis 关键数字与引擎 receipt 不一致 → 编造痕迹拒收。

## 5. 生成端 P0（奖励，非门）

### 5.1 反同质化贪心选种（P0a，λ=0.3）

$$
\text{score}(c) = \underbrace{\operatorname{rank}\big(|IC\_IR|(c)\big)}_{\in[0,1]} + \lambda\Big(1 - \max_{u \in \text{已选}\cup\text{历史}} |\rho(c, u)|\Big),\qquad \lambda = 0.3
$$

- 生产端标定（23/20 个 farm 周期反事实重建）：λ=0.3 用 10%/7% 的选种 IC 牺牲，买 54%/40% 的种子两两去相关。`bridge.py:609-650`
- 边界修正（实测）：全池相关结构已多样，此奖励的价值在**局部**（防 argmax 把变异出发点锁进单一吸引子），不在全局墙。

### 5.2 P0b 复杂度奖励：挂起

生产 trail 是 null+信号混合分布，最简档 σ 反而最高（简单随机因子大量落在有真信号的原始量价变换上）→ "简单→窄 null"未获证实，不接线到计价；生成端浅树先验可选。复杂度条件 null（方案 B 第三重锁的一段）同样等受控校准（3 档 × 100 只随机因子）。

## 6. 影子层：方案 B 分层计价（批次 1，只算不拦）

**动机**：现行墙对全部试验一价，journal 预注册候选（机制层）与机械喷射（机械层）混在一个 M 里互相抬价。分层后各层各付各的。

- **层定义**：机制层 = source_hash ∈ `registered_ledger.jsonl`（journal 预注册，`registered_ts ≤ eval_ts` 时机验证防补票）的试验子集；其余为机械层。`bridge.py:3882-3901`
- **层内计价**：子集走独立 CRN 采样器（不扰动全局状态），$\bar\sigma_{mech} = E[\max|X|]$ on 子集 R；影子字段 `shadow_stratum` = {p_mechanism, bar_sigma_mechanism, n_trials_mechanism, α_layer=0.0253}。`bridge.py:1222-1252`
- **α 层间 Šidák**：$\alpha_{layer} = 1-(1-0.05)^{1/2} \approx 0.0253$（复用跨轨先例；双轨×双层复合时四次方 ≈0.0127，影子期后再定）。
- **三重锁设计**（防层标签被滥用）：① 层内 M 自付——注册垃圾抬自己层的墙；② 复杂度条件 null——**挂起**待校准（§5.2）；③ 时机验证——台账 `registered_ts` 早于评估时刻。
- **生产预估**（层内 M 折叠，hypothesis origin n=421/349，|ρ|≥0.6 聚类 K=163）：机制层墙 **≈0.94**（IC 线）/**≈0.79**（尾部线），对比现行 1.49/1.18。
- **状态**：门不变；翻转（批次 2）待 1-2 个挖掘夜的影子双价分布审查。

## 7. 候补线（备用线：dev 优异未过线者的去处）

### 7.1 备用池谓词 `standby_view`（派生只读视图）

$$
\text{入选} \iff |\widehat{sr}| \ge 0.75\ \wedge\ \text{未入册}\ \wedge\ \text{阶段}\in\{\text{dev},\text{batch}\}
$$

- **族内去重**：草稿相关（≥60 点）$|\rho| \ge 0.6$ 者丢——同族只留最强代表。
- 层标注：机制层（在注册台账）vs 机械层；按 $|\widehat{sr}|$ 排序取前 30。
- 生产快照：原始 261 条 / 去族后 88 条。
- `standby.py:72-141`

### 7.2 通道①：重定价赦免（影子，待批次 2）

注册候选在影子机制层价下 $p_{mechanism} \le 0.0253$ 即"本应通过"→ 批次 2 翻转后按机制层价重新准入（预估外科手术式翻转 1-2 条）。当前只产出 `shadow_stratum` 字段。

### 7.3 通道③：规则化组合 `combine`（已上线）

```
规则(预注册 dof=1):候补池(|sr|≥0.75)内,按|sr|贪心取成员,
约束两两|ρ| < 0.3,凑 k=4 只(不足3只不出组);
组合体 = 等权符号对齐:z 化后按各自 dev 符号加权平均
```

$$
F_{comp}(t, a) = \frac{1}{k}\sum_{i=1}^{k} \operatorname{sign}(\widehat{sr}_i)\, z\big(F_i\big)(t, a)
$$

- 组合体源码由引擎机械拼装（成员函数体缩进嵌入），作为**一条新试验**走正常 evaluate/submit——规则是预注册的，组合自由度按 1 计价（dof=1），不可事后挑成员。
- 预估：正交成员组合的 deflated p ≈ 0（组合分散化 + 成员已各付过自己的选择价）。`standby.py:155-233`

### 7.4 通道⑤：前向孵化 `incubate`（已上线，等掘金日更喂新数据）

**入队**（enqueue）：hash 前缀解析 trail，**源码与方向符号冻结**（sign = dev 期 IC 符号），cohort 容量 ≤ 50，窗口 120 交易日（入队日起算）。

**判决**（judge，一次性）：

$$
t_i = \operatorname{sign}_i \cdot IC\_IR^{fresh}_i \cdot \sqrt{n_i} \quad > \quad z_{th} = \Phi^{-1}\!\Big(1 - \frac{0.0253}{K}\Big)
$$

- $K$ = cohort 成员数；$n_i$ ≥ 20 才判；新鲜交易日 < 120 → not_ready（不消耗判决）。
- 幸存者由引擎写入 `incubation/survivors.jsonl`（submit 豁免的权威源）。
- 数值感：K=50、n=120 时 $z_{th} \approx 3.24$，复活线 ≈ **IC_IR 0.30**；K=13 时 ≈0.26——对比 dev 墙 ≈1.09，4 倍折扣。

**为什么这么低（统计理由）**：新鲜窗口与 dev 期独立 →

$$
\mathbb E\big[\text{OOS 表现} \,\big|\, \text{dev 挑选},\ null\big] = 0
$$

dev 墙的 $sr_0 = \bar\sigma\cdot\text{pool\_std}$ 定价的是**在 dev 数据上挑了数千次**的运气；新数据上没有这个影子，只需付 cohort 内 $K$ 次比较的 Šidák 价。真因子保真度 ≥50% 时复活概率近 1（生产预估）。

### 7.5 提交豁免链（幸存者回主线）

幸存者 submit 带 `incubated_cohort` 参数 → 引擎验证 `survivors.jsonl`：

1. deflated p 红旗剥离（"deflated p"/"池分布基线"两条），原 p 存档 `p_dev_original`，判定 p 置 0.001（标 `p_incubation_waived=true`）；
2. **其余门全部照旧**：噪声/正交/平坦性/铁律/red_flags 其他项。
3. registry 条目标注 `incubated_admitted`（审计链）。`bridge.py:5921-5959`

## 8. α 家族与数字速查

| α | 值 | 用在哪 |
|---|---|---|
| IC 轨 deflated p | 0.05 | `passes_acceptance` 默认 |
| 跨轨/层间 Šidák $\alpha_{track}=\alpha_{layer}$ | 0.0253 | 尾轨 G3 z 分位；机制层影子价；孵化层内校正 |
| 孵化层内 | 0.0253/K | cohort K 次比较 |
| 噪声门/G1/G2 | z=3（单侧 p≈0.00135） | 固定 3σ |
| 正交门 | ≥0.80 annotate+hint（软门）/ ≥0.60 标注 | 平均秩相关（ortho_hard_gate=True 可恢复硬拒） |
| 备用池 | \|sr\|≥0.75；族内 \|ρ\|≥0.6 丢 | 谓词 |
| 组合通道 | 两两 \|ρ\|<0.3、k=4、≥3 只 | 规则 |

| 生产快照（2026-09-13 估值，活算以响应字段为准） | IC 线 | 尾部线 |
|---|---|---|
| 诚实 bar_sigma（σ 单位） | 3.345 | 3.457 |
| 墙（bar×pool_std，IC_IR 单位） | ≈1.09 | ≈0.95 |
| 机制层墙（方案 B 预估） | ≈0.94 | ≈0.79 |
| 孵化复活线（K=50@120日） | ≈0.30 | — |

> 注：尾部线 0.95 为修复时快照（估）；机制层 0.94/0.79 为生产数据预估、正式翻转前以影子字段实测为准。

## 9. 各门槛文件索引

| 门 | 位置 |
|---|---|
| 选运采样器/R 构造/bar_sigma | `python/src/dsh_factor_mining/factor/evaluate.py:418-655` |
| DSR p / sr0 / B-LP | `python/src/dsh_factor_mining/factor/gates.py:31-126` |
| IC 轨验收 | `evaluate.py:1239-1265`；提交重算 `bridge.py:5861-5919` |
| 尾轨 G1/G2/G3 | `python/src/dsh_factor_mining/factor/tailgate.py:152-373` |
| 噪声门 | `factor/noise.py:199-256`；`bridge.py:5961-6080` |
| 平坦性 | `factor/flatness.py`；`bridge.py:6096-6132` |
| 正交门 | `python/src/dsh_factor_mining/book_ranks.py:127-172` |
| 影子分层 | `bridge.py:1222-1252, 3882-3970` |
| 备用池/组合③ | `python/src/dsh_factor_mining/standby.py:72-233` |
| 孵化⑤ | `python/src/dsh_factor_mining/incubate.py:74-193` |
| 选种 λ | `bridge.py:609-650` |
