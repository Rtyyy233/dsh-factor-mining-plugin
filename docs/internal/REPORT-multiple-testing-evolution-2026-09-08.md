# 多重检验校正演进史 — 公式层调研报告

- 日期:2026-09-08
- 调研方法:`git log` 全历史(415 提交,2026-07-11 初始化至今)pickaxe 检索(多重检验 / deflated / N_eff / Šidák / Bonferroni / 置换等),逐一核读关键提交正文,并对照现行代码(`factor/evaluate.py`、`factor/gates.py`、`factor/tailgate.py`、`factor/permute.py`、`factor/flatness.py`、`strategy_lab/gates.py`)与 `AUDIT-2026-08-18.md`。
- 本文目标:每一阶段的统计校正机制给出**可核验的公式推导**,并标注其后来被修正/退役的原因。公式为 GitHub 可渲染 LaTeX。

---

## 0. 总览

| 阶段 | 时间 | 机制 | 核心公式 | 终态 |
|---|---|---|---|---|
| 0 前史 | 07-11 ~ 08-16 | 前视防御、列置换 | 置换分位 p | 仍为单试验组件 |
| 1 校正进入 | 08-16 (`d0f32c7`) | Harvey t>3 + N_eff + Šidák | $\alpha_0 = 1-(1-\alpha)^{1/N_{\mathrm{eff}}}$ | N_eff 解析式被 v3 退役 |
| 2 DSR | 08-18 (`021b613`/`dbfc940`/`3b36de9`) | Deflated Sharpe 硬门 | $p = 1-\Phi(t_{\mathrm{adj}})$, $sr_0=\sigma_\times\sqrt{2\ln N}$ | $sr_0$ 形态被 v3 取代 |
| 3 选择运气直算 | 08-21/22 (`d2f6934`/`e283ddf`) | $E[\max\lvert X\rvert]$ 蒙特卡洛 + landscape null | $sr_0 = \bar\sigma \cdot \sigma_{\mathrm{pool}}$ | **现行**(v3/v4) |
| 4 null 完备化 | 08-24~26 | 双轨独立校正、置换 null、高阶矩 | $\alpha_{\mathrm{track}} = 1-(1-\alpha)^{1/2} \approx 0.0253$ | **现行** |
| 5 策略层移植 | 08-27 起 | 预注册 + 账本 + test 一次性锁 + G1-G3 | $E[\max X]\cdot s_0 + z_{1-\alpha}\,s_0$ | **现行** |
| 6 账本维度计价 | 09-05/06 (`d521bf1`/`4e95d31`) | 相似性防线 + 正交门 | $\lvert\bar\rho\rvert \ge 0.80$ 拒 | **现行** |

主线:**阈值启发 → 解析校正 → 模拟直接计价 → 分轨道独立校正 → 账本有效维度计价**。每次升级几乎都由生产事故或审计实证驱动。

---

## 记号与公共设定

- $M$:已试假设(试验)总数;$\rho_{ij}$:试验 $i,j$ 统计量之间的实测相关;$\bar\rho$:平均两两相关。
- $\mathrm{IC}$:因子值与未来收益的截面秩相关(Pearson on ranks);$\mathrm{IC}_{\mathrm{IR}} = \bar{IC}/s_{IC}$:IC 序列的均值除以标准差(信息比率)。
- $\Phi$:标准正态 CDF;$\bar\Phi = 1-\Phi$;$z_q = \Phi^{-1}(q)$;$\varphi$:标准正态密度。
- $\gamma_3, \gamma_4$:日度(或逐期)收益/序列的三阶标准化矩(偏度)与四阶标准化矩(峰度,非超额)。
- $T$:时间序列观测期数(采样日数)。
- 显著性水平统一取 $\alpha = 0.05$,除非另有声明。

---

## 阶段 0:前史 — 只有前视防御,没有选择偏差意识(至 2026-08-16)

harness v0.1(`94abded`/`664721e`)的统计防线是逐试验的,与"试验间如何分摊错误率"无关:

1. **因果性扰动测试**:对因子源码做时间截断/滞后注入,检测未来函数(实现层,无解析式)。
2. **列置换(column-perm)**:把因子值在**截面(股票间)**随机重排,保持因子截面分布与收益截面分布两个边缘不变,只破坏"因子→收益"的配对。零假设 $H_0$:因子对截面收益排序无预测力。置换 $R$ 次得 null 分布 $\{t^{(1)},\dots,t^{(R)}\}$,单试验 p 值

$$
p_{\mathrm{perm}} = \frac{1 + \#\{r:\ \lvert t^{(r)}\rvert \ge \lvert t^{\mathrm{obs}}\rvert\}}{R+1},
$$

   分母加一(把原观测计入 null 集合)保证 $p>0$ 且检验在 $H_0$ 下精确(每排列等概率)。
3. **独立审计**:手写 Spearman 与 pandas rank 两套实现交叉验证(数值容差对账)。

此时防的是**前视偏差与实现错误**,不防 **best-of-$M$ 选择偏差**——同一天挖 50 个因子挑最好的入库,即使每个都过了置换门,家族错误率依然失控。这正是阶段 1 的出发点。

---

## 阶段 1:校正进入 — Harvey 阈值 + 有效试验数 + Šidák(2026-08-16,`d0f32c7`)

### 1.1 问题设定

$M$ 个候选因子各自做水平 $\alpha_0$ 的双侧检验,统计量 $T_i$,零假设下 $T_i \sim N(0,1)$(大样本)。决策规则是"取 $\lvert T_i\rvert$ 最大者入册"。家族错误率(FWER):

$$
\mathrm{FWER} = \Pr\!\Big(\bigcup_{i=1}^{M} \{\lvert T_i\rvert > z_{1-\alpha_0/2}\}\Big).
$$

若不做校正、且各试验独立:

$$
\mathrm{FWER} = 1-(1-\alpha_0)^{M} \xrightarrow{M\to\infty} 1,
$$

即试验池一大,"至少一个假阳性"几乎必然发生。挖因子是持续进行的批量搜索,这是首要敌人。

### 1.2 Bonferroni 与 Šidák 校正的推导

**Bonferroni**(无需独立性):由 Union Bound,

$$
\mathrm{FWER} \le \sum_{i=1}^{M}\Pr(\lvert T_i\rvert > z) = M\alpha_0
\;\Longrightarrow\;
\alpha_0^{\mathrm{Bf}} = \frac{\alpha}{M}.
$$

**Šidák**(独立下精确):独立性给出 $\Pr(\text{无拒绝}) = (1-\alpha_0)^M$,解 $1-(1-\alpha_0)^M = \alpha$:

$$
\boxed{\;\alpha_0^{\mathrm{Šid}} = 1-(1-\alpha)^{1/M}\;}
$$

二者的关系:$\alpha_0^{\mathrm{Šid}} = 1-e^{\frac{1}{M}\ln(1-\alpha)} = \frac{\alpha}{M} + O(\alpha^2)$,即 Šidák 略小于 Bonferroni(不保守得那么狠),且在独立性成立时**精确**。

**相关性与保守性(Slepian 引理)**:对零均值高斯向量,联合 CDF $\Pr(X_1\le c_1,\dots,X_M\le c_M)$ 关于每一对协方差单调递增。因此只要所有两两相关 $\rho_{ij}\ge 0$,

$$
\Pr(\text{全部不拒绝}) \;\ge\; \prod_i \Pr(\text{第 } i \text{ 个不拒绝}) = (1-\alpha_0)^M,
$$

正相关下 Šidák 仍保守(valid)。注意反向不成立:若试验间存在显著负相关,Šidák 可能失效——项目的实测相关矩阵以正相关为主,该前提在实务中成立(阶段 3 会改为直接模拟,不再依赖此论证)。

### 1.3 有效试验数 $N_{\mathrm{eff}}$:项目最初采用的线性式

试验并非独立,把 Šidák 的指数从 $M$ 换成有效独立试验数 $N_{\mathrm{eff}}$。项目 v1 采用:

$$
\boxed{\;N_{\mathrm{eff}} = 1 + (M-1)(1-\bar\rho)\;}
$$

**性质核验**(这是当时采用它的依据,也是它后来被推翻的伏笔):

- $\bar\rho = 0 \Rightarrow N_{\mathrm{eff}} = M$(全独立);$\bar\rho = 1 \Rightarrow N_{\mathrm{eff}} = 1$(全冗余)。边界正确。
- 可解读为:每增加一个与既有试验平均相关 $\bar\rho$ 的新试验,只贡献 $1-\bar\rho$ 份"独立信息";共同分量计 1 个自由度。它是独立信息量的**一阶线性计价**,不是任何严格统计量的精确解。

对照两个更严格的参照系,便于看清它丢掉了什么:

1. **设计效应式**(精确匹配样本均值的方差):对可交换相关(exchangeable,$\rho_{ij}\equiv\bar\rho$),
   $$\mathrm{Var}(\bar X) = \frac{\sigma^2}{M}\big[1+(M-1)\bar\rho\big] \;\Longrightarrow\; M_{\mathrm{eff}}^{\mathrm{deff}} = \frac{M}{1+(M-1)\bar\rho}.$$
2. **谱参与率**(v2 实际使用的):$PR(\Sigma) = \frac{(\sum_k \lambda_k)^2}{\sum_k \lambda_k^2}$。若谱等效为 $N_{\mathrm{eff}}$ 个相等特征值,则 $PR = N_{\mathrm{eff}}$;一般情形它是"等谱等效维数"。

三者在大 $M$、弱相关下的量级:$N_{\mathrm{eff}}^{\mathrm{lin}} \approx M(1-\bar\rho)$(线性于 $M$),$M_{\mathrm{eff}}^{\mathrm{deff}} \approx 1/\bar\rho$(饱和),$PR \to 1/\bar\rho^2$(饱和)。**线性式最激进,谱式最保守**——这个差异在阶段 3 会被证明是方向性的错误来源。

### 1.4 Harvey–Liu–Zhu 式的 $t>3$

单试验阈值从 $z\ge 2$(名义 5%)提高到 $z\ge 3$。其量级论证:若已试假设总数达数百量级,Bonferroni 单侧阈值

$$
t^* = \Phi^{-1}\!\Big(1-\frac{\alpha}{M}\Big):
\quad
M=100 \Rightarrow 3.29,\quad
M=1000 \Rightarrow 3.89.
$$

Harvey–Liu–Zhu(2016)对已发表截面因子的多重检验框架给出的经验要求 $t\ge 3.0$ 正对应数百量级试验池的校正强度。项目直接采纳为单试验门,与 Šidák 侧的双保险并行。

**本阶段配置**(`evaluate_batch` 内):批内 $M$ 个候选 → 实测两两相关 → $N_{\mathrm{eff}}$ → Šidák 折减 → `best_deflated_p` 作为最终显著性。

**遗留问题(后被实证)**:$N_{\mathrm{eff}}$ 的任何标量摘要都回答不了"这一批里挑最大者的期望运气是多少"(见 §3.1);且此时尚无跨批次累积计数——不同批次间的选择不被计价。

---

## 阶段 2:Deflated Sharpe Ratio — 校正第一次真实生效(2026-08-18)

`021b613` 引入 Bailey–López de Prado(B-LP)式 deflated Sharpe:对"按已试假设数折减后"的 SR 给单因子 p 值。同日 `dbfc940`/`3b36de9` 的事故修复构成这一阶段的真正内容。

### 2.1 基础量:$\mathrm{SR}$ 估计量的精确渐近分布(delta method 完整推导)

设逐期收益(项目里即逐日 IC 序列的元素)$r_t$ iid,$E[r_t]=\mu$,$\mathrm{Var}(r_t)=\sigma^2$,标准化矩 $\gamma_3, \gamma_4$。目标参数 $\mathrm{SR} = \mu/\sigma$。以参数对 $(\mu, v)$ 记($v=\sigma^2$),估计函数 $g(\mu, v) = \mu v^{-1/2}$,梯度

$$
\nabla g = \Big(v^{-1/2},\; -\tfrac{1}{2}\mu v^{-3/2}\Big).
$$

样本矩向量的协方差( iid 四阶矩展开):

$$
\mathrm{Cov}\!\begin{pmatrix}\hat\mu\\ \hat v\end{pmatrix}
= \frac{1}{T}\begin{pmatrix} \sigma^2 & \mu_3 \\ \mu_3 & \mu_4 - v^2 \end{pmatrix}
= \frac{\sigma^2}{T}\begin{pmatrix} 1 & \gamma_3 \\ \gamma_3 & \gamma_4-1 \end{pmatrix},
$$

其中 $\mu_3 = \gamma_3\sigma^3$,$\mu_4 = \gamma_4\sigma^4$。Delta method 给出

$$
T\cdot\mathrm{Var}(\widehat{\mathrm{SR}})
= \nabla g\; \sigma^2\begin{pmatrix} 1 & \gamma_3 \\ \gamma_3 & \gamma_4-1\end{pmatrix} \nabla g^{\top}
= 1 - \gamma_3\,\mathrm{SR} + \frac{\gamma_4-1}{4}\,\mathrm{SR}^2,
$$

(逐项:$v^{-1/2}\sigma^2 v^{-1/2}=1$;$2\cdot v^{-1/2}\cdot(-\tfrac12\mu v^{-3/2})\mu_3 = -\mu\mu_3/v^2 = -\gamma_3\,\mathrm{SR}$;$(\tfrac14\mu^2v^{-3})(\mu_4-v^2) = \tfrac14 \mathrm{SR}^2(\gamma_4-1)$。)

即

$$
\boxed{\;\sqrt{T-1}\,\frac{\widehat{\mathrm{SR}} - \mathrm{SR}}{\sqrt{1-\gamma_3\widehat{\mathrm{SR}} + \frac{\gamma_4-1}{4}\widehat{\mathrm{SR}}^2}} \;\xrightarrow{d}\; N(0,1).\;}
$$

这就是 B-LP 标准误公式:**偏度抬高估计误差、峰度抬高估计误差、SR 自身平方项降低它**。高斯收益($\gamma_3=0,\gamma_4=3$)退化为经典的 $\mathrm{Var} \approx (1+\mathrm{SR}^2/2)/T$(Lo 2002)。

### 2.2 PSR 与 $t_{\mathrm{adj}}$

给定基准 $\mathrm{SR}^*$,"真实 SR 大于 $\mathrm{SR}^*$"的概率(Probabilistic Sharpe Ratio)由上式直接读出:

$$
\mathrm{PSR}(\mathrm{SR}^*) = \Phi\!\left(
\frac{(\widehat{\mathrm{SR}} - \mathrm{SR}^*)\sqrt{T-1}}{\sqrt{1-\gamma_3\widehat{\mathrm{SR}} + \frac{\gamma_4-1}{4}\widehat{\mathrm{SR}}^2}}
\right),
\qquad
t_{\mathrm{adj}} = \frac{(\widehat{\mathrm{SR}} - \mathrm{SR}^*)\sqrt{T-1}}{\sqrt{1-\gamma_3\widehat{\mathrm{SR}} + \frac{\gamma_4-1}{4}\widehat{\mathrm{SR}}^2}}.
$$

项目最终的 `t_adj`(WS4, `d5ed63f`,spread 版)与上完全同构:

$$
t_{\mathrm{adj}} = \frac{(\mathrm{spread}_{\mathrm{IR}} - sr_0)\sqrt{n-1}}{\sqrt{1 - g_3\,\mathrm{spread}_{\mathrm{IR}} + \frac{g_4-1}{4}\,\mathrm{spread}_{\mathrm{IR}}^2}},
$$

deflated p 值即 $p = \bar\Phi(t_{\mathrm{adj}}) = 1 - \mathrm{DSR}$。

### 2.3 基准 $sr_0$:选最大者的期望运气(极值理论推导)

$M$ 次试验里挑最大的那个,它的 null 期望不是 0,而是 $M$ 个标准正态最大值的期望。设 $T_{(M)} = \max_{i\le M} T_i$,$T_i \stackrel{iid}{\sim} N(0,1)$:

$$
\Pr(T_{(M)} \le x) = \Phi(x)^M.
$$

取 $x = u_M + y/u_M$,其中 $u_M = \Phi^{-1}(1-1/M)$。Mills 比给出 $\bar\Phi(u_M)\cdot u_M \approx \varphi(u_M)$,即 $M\varphi(u_M)\to 1$,于是

$$
M\ln\Phi\!\Big(u_M + \frac{y}{u_M}\Big) \approx -1 + N\text{-阶修正} \;\longrightarrow\; -e^{-y},
$$

得 Gumbel 极限 $\Pr(T_{(M)}\le x)\to \exp(-e^{-y})$,其均值为欧拉常数 $\gamma\approx 0.5772$。展开 $u_M$(对 $\Phi^{-1}(1-1/M)$ 用 $\bar\Phi(u)\approx\varphi(u)/u$ 配平):

$$
u_M = \sqrt{2\ln M} - \frac{\ln\ln M + \ln 4\pi}{2\sqrt{2\ln M}} + o\!\big((\ln M)^{-1/2}\big),
$$

$$
\boxed{\;E[T_{(M)}] = u_M + \frac{\gamma}{u_M} + o\!\big((\ln M)^{-1/2}\big)\;\approx\;\sqrt{2\ln M}\;}
$$

最后一阶即项目所称 $\sqrt{2\ln N}$。两个修正项不可省:对 $M=10^4$,$\sqrt{2\ln M}=4.29$ 而 $u_M+\gamma/u_M = 3.85+0.15 = 4.00$。

**尺度匹配(v2 的核心修正,`dbfc940`)**:试验统计量 $T_i$ 不是单位方差——不同试验的 $\mathrm{IC}_{\mathrm{IR}}$ 有截面离散度 $\sigma_\times$(pool_std)。标准化后 $T_i/\sigma_\times \sim N(0,1)$,所以

$$
\boxed{\;sr_0 = \sigma_\times\cdot E[T_{(M)}] \;\approx\; \sigma_\times\sqrt{2\ln N_{\mathrm{eff}}}\;}
$$

(项目所称"B-LP 式 4 池缩放"。)事故链完整的三个坑:

1. **校正从未生效**:计数通道依赖 agent 主动上报,`n_trials` 恒为 1 → $sr_0=\sigma_\times\sqrt{2\ln 1}=0$,零惩罚。修复:引擎侧按 trail 的 unique `source_hash` 硬统计。
2. **尺度错配**:旧实现拿裸 $\sqrt{2\ln N}$(量级 3~4)直接与 $\mathrm{IC}_{\mathrm{IR}}$(量级 0.4)比较 → $N>1$ 时全灭、$N=1$ 时零惩罚。修复即上面的池缩放。
3. **系数 bug**(`AUDIT-2026-08-18` P1-1):代码把 $\sqrt{2\ln N}$ 写成 $\sqrt{\ln N}$(多乘 $0.5^{0.5}$),门槛低估 $\sqrt 2$ 倍,方向为高估显著性。修复。

### 2.4 从报告数字到硬门(`3b36de9`)

注入验证:$z=7.08$、deflated $p=0.9997$ 的因子在旧代码 `verdict=pass`——**p 算得再准也不进判据列表**。修复后的门逻辑:

- deflated $p > 0.05$ → red_flag + 准入拒绝;
- $N>1$ 但 $p=\mathrm{None}$(缺 pool_std 基线)→ red_flag,引导先跑 null 校准;
- 复用既有链路(red_flags 非空 → needs_review → submit 拒),零新增机制。

**fail-closed 原则在此确立**:算不出校正就拒绝准入,不给不可信数字。

---

## 阶段 3:选择运气直接计价 — $E[\max\lvert X\rvert]$ 蒙特卡洛(2026-08-21/22)

### 3.1 谱换算为什么必须退役:一个严格论证

v2 链路是"相关矩阵 → 谱参与率 $PR=(\sum\lambda)^2/\sum\lambda^2$ → $sr_0 = \sqrt{2\ln PR}\cdot\sigma_\times$"。`d2f6934` 证伪之:**有效维数标量不是 $E[\max]$ 的充分统计量**。可给出精确论证:

**等相关下的精确分解。** 设可交换相关 $\rho_{ij}\equiv\rho$,则

$$
X_i = \sqrt{\rho}\,F + \sqrt{1-\rho}\,\varepsilon_i,\qquad F,\varepsilon_i \stackrel{iid}{\sim} N(0,1),
$$

(验证:$\mathrm{Cov}(X_i,X_j) = \rho$,$\mathrm{Var}=1$。)于是

$$
\max_i X_i = \sqrt{\rho}\,F + \sqrt{1-\rho}\,\max_i \varepsilon_i
\;\Longrightarrow\;
E[\max_i \lvert X_i\rvert] = \sqrt{1-\rho}\; E\big[\max_{i\le M}\lvert\varepsilon_i\rvert\big],
$$

后者由 §2.3 渐近为 $\sqrt{1-\rho}\,\sqrt{2\ln(2M)}$:**随 $M$ 无界增长**。

**而谱参与率在等相关下饱和。** 等相关谱 = 单点 $1+(M-1)\rho$ 加 $(M-1)$ 重 $1-\rho$,故

$$
PR = \frac{M^2}{M^2\rho^2 + (M-1)(1-\rho)^2}
\;\xrightarrow{M\to\infty}\; \frac{1}{\rho^2},
$$

**与 $M$ 无关**。两个量随维数的行为根本矛盾:$M\to\infty$(固定 $\rho$)时真实选运气差发散,谱路线给出常数。数值例($M=10^4,\rho=0.2$):$PR\to25$,$\sqrt{2\ln 25}\approx 2.54$;真实 $E[\max\lvert X\rvert]\approx\sqrt{0.8}\times 4.25\approx 3.80$,低估 1.5 倍——且差距随 $M$ 增大继续拉大。项目在生产 trail 实测相关矩阵上测得低估 **3.4 倍**,并据此整体退役谱→B-LP 换算链。

结论:**$E[\max\lvert X\rvert]$ 依赖相关矩阵的完整结构,必须对 $R$ 矩阵整体计价,不能用任何等效维数摘要。**

### 3.2 v3 机制:`_LuckSampler`

定义门参数(双侧——agent 按 $\lvert\mathrm{IC}\rvert$ 挑最优,正负尾都是选择):

$$
\bar\sigma \;=\; E\big[\max_{i\le M}\lvert X_i\rvert\big],\qquad X \sim N(0, R),
$$

$R$ 为全部试验对**两两实测**的相关矩阵(尾对齐:截到共同有效区间,修掉"长度分桶"造成的 90% 稀疏假象)。蒙特卡洛直算,两个保证:

- **CRN 公共随机数 + 单调性**:同一随机数实现下,候选集扩大只会让 $\max\lvert X\rvert$ 逐路径不减(逐点单调),因此估计量对候选集单调不减——这是**耦合定理**的直白应用,不是数值补丁;超集扫描免重采样,且防止"加入候选反而压低运气基线"的伪影。
- 边界:$M=0 \Rightarrow \bar\sigma=0$;$M=1 \Rightarrow \bar\sigma = E\lvert Z\rvert = \sqrt{2/\pi}\approx 0.798$。

由此 $n$ 试的运气项与 §2.3 对齐:$E[\max\lvert X\rvert]\approx u_{2M} + \gamma/u_{2M}$(双侧 = 约 $2M$ 个尾事件)。

**门参数唯一化为 $\bar\sigma$**:$n_{\mathrm{trials}}/n_{\mathrm{eff}}$ 降级为纯遥测。配套**单调包络**防跨版本灌水:

$$
\bar\sigma_{\mathrm{at\,write}} = \max\big(\bar\sigma_{\mathrm{raw}},\ \bar\sigma_{v3\text{章}},\ \mathrm{B\text{-}LP}_{v2\text{旧章}}\big),
$$

(旧章换算已被证明 ≤ 诚实双侧值,保守方向,可安全并入 max。)

### 3.3 v4:null 基线的污染事故与 landscape-only(2026-08-22,`e283ddf`)

最终门形态:

$$
sr_0 = \bar\sigma \times \sigma_{\mathrm{pool}},\qquad
p = \bar\Phi(t_{\mathrm{adj}}),\qquad
t_{\mathrm{adj}} = \frac{(\widehat{\mathrm{IC}}_{\mathrm{IR}} - sr_0)\sqrt{T-1}}{\sqrt{1-\gamma_3\widehat{\mathrm{IC}}_{\mathrm{IR}} + \frac{\gamma_4-1}{4}\widehat{\mathrm{IC}}_{\mathrm{IR}}^2}}.
$$

**事故**(52 小时 session 轨迹取证):amihud 族真信号($\mathrm{IC}_{\mathrm{IR}}\,0.52\text{–}0.59$)写入 trail 后,若用 trail 实测 std 作 $\sigma_{\mathrm{pool}}$,则 $\sigma_{\mathrm{pool}}$ 膨胀 70%($0.179\to0.304$)→ $sr_0$ 从 0.37 抬到 0.63 → $\mathrm{IC}_{\mathrm{IR}}=0.590$ 被拒($p=0.67$)→ agent 试图 reset 洗账。

**统计诊断**:校正基线 $\sigma_{\mathrm{pool}}$ 应估计 **null 分布的尺度**;把找到的真信号混入估计,等于"用成功证据拓宽 null"——选择成功惩罚选择成功,方向反了。修复:

$$
\sigma_{\mathrm{pool}} = \sigma_{\mathrm{landscape}}\ (\text{仅指纹门后的 null 地形}),\qquad
\frac{s_{\mathrm{trail}}}{\sigma_{\mathrm{landscape}}} \ge 1.5 \Rightarrow \text{报“试验集大概率含真信号”(探测器,不进门)}.
$$

同族刷屏攻击(刷低 $\sigma_\times$ 压门槛)由 landscape floor 独立防住。配套:

- **reset 硬拦**:trail≥50 或 registry 非空时 agent 自助 reset 直接拒绝(搜索史不可单方抹除;统计上,reset = 试图把 $M$ 清零重计,直接摧毁 deflation 的输入)。
- **ledger.json 永久审计账本**:每次 submit 落盘门参数与 p 值,不随任何 reset 清除;**不做门的输入**(曾评审的"K 锚点门"撤回:batch 内 $K$ 选 1 的跨族选择只有 trail 全量计数能计价)。
- **batch selection_cost 透明化**:批响应明示本批 $K$ 个成员的选择成本增量($\sigma$ 与 $\mathrm{IC}_{\mathrm{IR}}$ 双单位)。

重评 131 个历史因子:4 个被污染错杀的因子复活($p$ = 0.022/0.035/0.039),TSI 族天花板 0.39($p\approx0.46$)确认不过门。

---

## 阶段 4:null 空间完备化与分轨道(2026-08-24 ~ 26)

### 4.1 计数与停点分离(2026-08-24,`c4904e3`)

终身 $M$ 参与停点判定造成"一次到顶永久锁死"(生产 521≥400 实证)。修正的统计依据:**deflation 的 $M$ 在家族意义上必须全量累积**(少计 = 低估运气 = 高估显著性),但**停点是行为控制**不是统计量,应当响应边际信息。故:

- 终身计数保留,**只进 deflation**;
- 停点改"簇试验上限"(尾部同族链,与最新试验 $\lvert\rho\rvert\ge0.6$ 的连续块)+ 滑窗收敛(最近 $W$ 条最佳 vs 前一窗最佳),新强因子出现自动解除。

### 4.2 尾部线独立校正与跨轨 Šidák(2026-08-25,`2a07443`)

IC 线与尾线(top-$K$ 组差)是两个统计量、两个 null、两个家族几何,故**分账**:

**独立账本**:从 trail 派生,键 $(source\_hash, horizon, k_{\mathrm{frac}})$——带尾块条目即一次尾部试验。

**家族几何 = 名单 MinHash Jaccard**:IC 相关在尾部失效(实测 $\mathrm{IC}$ 两两 $\rho=0.75$ 的因子,其 top-$K$ 名单可只重叠 0.4),改按"选出的股票名单"度量家族。Jaccard $J(A,B)=|A\cap B|/|A\cup B|$,MinHash 估计量:均匀随机哈希 $h$ 下,

$$
\Pr\big[\min_h A = \min_h B\big] = J(A,B),
$$

(最先被随机置换命中的元素均匀取自 $A\cup B$,落在交集的概率即 $J$。)取 $k=32$ 个哈希,

$$
\hat J = \frac{1}{k}\sum_{j=1}^{k}\mathbb{1}\big[\min_{h_j}A = \min_{h_j}B\big],\qquad
\mathrm{Var}(\hat J) = \frac{J(1-J)}{k}\ \le\ \frac{1}{4k} = \frac{1}{128}\;(\text{std}\le 0.088).
$$

无偏、维数常数(128B/试验)。$\hat J$ 超阈值贪心聚类,族数即尾部 $N_{\mathrm{eff}}$。

**独立 deflation bar**(单侧——尾部准入规则是 $\mathrm{spread}_{\mathrm{IR}} \ge$ bar,方向唯一):

$$
\mathrm{bar} = E[\max X](N_{\mathrm{eff}})\cdot s_0 + z_{1-\alpha_{\mathrm{track}}}\cdot s_0,
\qquad s_0 = \frac{1}{\sqrt{n_{\mathrm{days}}}}.
$$

**$s_0$ 的推导**:设逐采样日组差 $d_t$ 近似 iid,$\mathrm{spread}_{\mathrm{IR}} = \sqrt{n}\,\bar d/s_d$。由 $t$ 统计量的自正交性,$\sqrt{n}\,\bar d/s_d \xrightarrow{d} N(0,1)$,故 null 下

$$
\mathrm{spread}_{\mathrm{IR}} \sim N\!\Big(0, \tfrac{1}{n}\Big)\ \Rightarrow\ \mathrm{std} = s_0 = \tfrac{1}{\sqrt n}.
$$

bar 的两项分解:$E[\max X]\cdot s_0$ 是"家族里最幸运的 null 试验期望达到的水平"(§2.3 的 $N_{\mathrm{eff}}$ 维版本),$z_{1-\alpha}\cdot s_0$ 是单试验临界值;两者相加 = 家族运气叠加单试验阈值,方向保守。

**跨轨 Šidák**(全项目唯一显式 FWER 设计):双轨准入 = 每个候选有两次通过机会,若各轨都跑 $\alpha=0.05$,

$$
\mathrm{FWER}_{\mathrm{global}} = 1-(1-\alpha)(1-\alpha) = 2\alpha - \alpha^2 \approx 0.0975,
$$

接近翻倍。解 $1-(1-\alpha_{\mathrm{track}})^2 = \alpha$:

$$
\boxed{\;\alpha_{\mathrm{track}} = 1-(1-\alpha)^{1/2} = 1-\sqrt{0.95} \approx 0.0253.\;}
$$

(两轨统计量近似独立时精确;正相关下由 Slepian 保守。)

**高阶矩修正**(`d5ed63f`):尾部 $\mathrm{spread}$ 序列偏度/峰度不可忽略,G3 判定改 §2.2 的 $t_{\mathrm{adj}}$ 分母修正版;缺矩的 legacy 条目 denom=1(判定不因缺数据翻转)。

### 4.3 day-perm:时序分量的置换 null(2026-08-25,`82978dd`)

列置换杀**截面分配**分量;时序对齐分量(因子值"在哪个时刻出现"携带信息,如短周期反转)需要孪生 null:**day-perm 随机重排因子截面与收益截面的日期配对**,保持两个边缘分布原封不动。零假设:配对(时序对齐)无信息。

- 统计量参数化(IC、spread 通用);$R$ 次全随机配对(非循环移位——循环移位对慢因子退化:momentum 250 移 5 天 ≈ 自身,是 date-shift 的已知盲区);
- p 值:$p_{\mathrm{align}} = (1+\#\{T^{(r)} \ge T^{\mathrm{obs}}\})/(R+1)$ 方向性;$p_{\mathrm{two}} = 2\,p_{\mathrm{align}}$,故**双侧粒度下限 $2/(R+1)$**(单侧最小 $1/(R+1)$ 乘 2);
- **为什么不能做无条件硬门**(实现前推演钉死,决策论):
  - $p$ 小 = 真短周期因子与时序过拟合的**并集**,样本内不可分;
  - $p$ 大 = 持久倾斜结构主导(如非流动性溢价,任意配对都复现 IC)——是合法截面 alpha,由列置换认证。
  
  即 day-perm 与列置换是**正交分解**的两半,各自不能单独裁决,只能互相注释 → report-only,硬门方向与阈值留待 Phase 6 用 registry × 证伪库的混淆矩阵校准。

### 4.4 参数平坦性门(2026-08-25,`9d61497`)

防"调参刀锋"(参数贴噪声导致的悬崖)。无解析分布,签名规则:申报参数邻域 $\pm\,\mathrm{step}$ 内出现**翻号 / 塌到中心 50% 以下 / 中心显著而邻居退化** → 悬崖。统计立场:真实 alpha 是平滑峰(收益对参数的结构依赖连续),断崖只可能来自拟合噪声。同样 report-only 待校准。**确定性扰动不计入 deflation 的 $M$**——它不是选择试验,计数会虚高。

### 4.5 test 区绝不进计价(2026-08-25,`a5525ea`)

双防线:tail_ledger 加 stage 过滤(test 不是试验,不进 deflation 计价);submit 反查尾块排除 test 块。原则:**test 是消耗品,看一眼都是一次试验——但它的试验次数无法入账(没有"之后还有没有"的语义),所以物理隔离而不是计数。**

### 4.6 拒绝分类(2026-08-26,`f1eeb96`)

烧名事故(hash 漂移被程序性拒绝、重试被铁律拦死)后,entry 带 `reject_kind`:procedural(基础设施/反查失败,可治愈,旧 entry 删除放行——门引擎侧确定性重跑无翻盘随机性)vs substantive(门真判了,烧名,拒绝即信息)。**多重检验体系里"拒绝"本身是数据**:实质性拒绝进证伪库,影响后续家族几何。

---

## 阶段 5:策略层移植 — 从因子 IR 到交易臂(2026-08-27 起)

ETF 策略线(`dsh_strategy_lab`)把同一架构复制到臂/交易层面,载体适配但思想同构:

- **P3 账本**(`6888f97`):指纹五元组、trail 键 dedup、**计-与-不计试验语义**(诊断性计算与选择试验分开计账)、test 一次性锁。
- **三区物理截断** dev/wf/test + 同窗裁决(`cc27590`):所有臂比较在同一窗口、同一基线上做,杜绝跨窗择优(跨窗择优 = 未计价的选择)。
- **预注册**:择时树 55 候选的 L0–L3 漏斗与网格在跑数前冻结(`000ad5a`)。统计作用:把探索性检验(事后挑统计量)转化为验证性检验,**选择发生在看数据之前,trial 集合封闭**,deflation 的 $M$ 因此可精确枚举。
- **G1–G3**(`strategy_lab/gates.py`,与因子线 tailgate 同构):G1 placebo(随机入场 null)、G2 artifact 门(随机游走对照,$\lvert z\rvert<3$ 拒)、G3 deflation:

$$
\mathrm{bar} = E[\max X](N_{\mathrm{eff}})\cdot s_0 + z_{1-\alpha}\cdot s_0,
$$

  其中家族几何用 **trade 级 MinHash**(对交易名单做 MinHash,同一架构换个元素类型),$N_{\mathrm{eff}}$ = 贪心聚类族数。

策略线没有直接搬 DSR 高阶矩数学:臂级试验的样本结构(持仓重叠、事件稀疏)使 $t_{\mathrm{adj}}$ 的 iid 矩假设更弱,故以"预注册 + WF + 一次性 test 锁 + 记账"替代——校正强度来自**试验集合的事后不可扩性**,而非分布内插。

---

## 阶段 6:从次数计价到账本维度计价(2026-09-05/06)

### 6.1 探索相似性防线(2026-09-05,`d521bf1`)

deflation 的输入是 trial 计数与家族几何;**换皮重交**会同时污染两者(计数虚高、家族聚类失真)。防线(校准:23 拦 / 4 误杀):

- **S1 同构重交门**:候选与历史先例相似度 $\hat J \ge 0.8$(复用 MinHash 估计量)+ 常量未变 + 先例 verdict=pass 才拦;拒绝消息携带先例结果(拒绝即信息,不浪费这次提交的信息量);
- **S2 宣言对账**:record_trail 宣称的"新信息源/新构造"与 L1 指纹对账,不符拒收重写(防止叙事层伪造新试验);
- **S4 署名**:trail 条目带 `authored`(= origin≠random),根治"阶段标签 ≠ 署名"——random 生成的对照试验不许冒充自主探索(反之亦然),否则家族几何被投毒。

### 6.2 正交门:对账本有效维度定价(2026-09-06,`4e95d31`)

**定位(提交说明原文的统计转述)**:显著性门回答"它自己是否真实"(时间维度的运气计价,即 deflation);正交门回答"账本是否已知"(横截面的冗余计价)。deflation 对**试验次数**定价,正交门对**账本有效维度**定价——若无此门,账本里堆入同源因子会使 $\mathrm{book}$ 的有效维度虚增,让后续所有候选的"增量"判断失真。

机制:在册因子 dev 区日度**秩缓存**(uint16 npz),候选与每个在册因子做逐日秩 Pearson(= Spearman,公共有效日 ≥300):

$$
\bar\rho = \frac{1}{B}\sum_{b=1}^{B} \mathrm{corr}\big(\mathrm{rank}(r^{\mathrm{cand}}_t),\, \mathrm{rank}(r^{(b)}_t)\big),
\qquad
\begin{cases}
\lvert\bar\rho\rvert \ge 0.80 & \text{substantive 拒绝(冗余非无效,可程序重议)}\\
0.60 \le \lvert\bar\rho\rvert < 0.80 & \text{放行 + entry.orthogonality 强制标注}\\
\text{基础设施失败} & \text{跳过:不拒绝、不烧名}
\end{cases}
$$

用秩而非值:与 IC 口径一致、对单调变换稳健。符号不敏感($\lvert\bar\rho\rvert$,实测有符号构造性相反的同源对,$\lvert\rho\rvert\approx0.70$)。阈值 0.80 是"信息实质重叠"的行为阈值,非分布推导;0.60–0.80 灰带强制标注而非拒绝(标注进 registry,供后续增量判断消费)。动机实证:2026-09-05 全量正交审计发现量价相关域与历史 IDO 系同源、tail 线 vol_of_vol 八连注册两两相关至 1.000——显著性门全部放行,因为它们各自确实真实。

**与 deflation 的对偶总结**:

$$
\underbrace{p = \bar\Phi(t_{\mathrm{adj}}(sr_0))}_{\text{时间维:这批试验里挑最大者的运气}}
\;\times\;
\underbrace{\text{正交门}}_{\text{横截面:账本里还有没有没被占的维度}}
\;\times\;
\underbrace{\text{test 锁}}_{\text{未来:消耗一次性的确证预算}}
$$

---

## 附录 A:公式速查表

| 量 | 公式 | 来源/推导 |
|---|---|---|
| Šidák 单检验水平 | $\alpha_0 = 1-(1-\alpha)^{1/N}$ | 独立性下 FWER 精确解;Slepian 保证正相关下保守 |
| HLZ 阈值 | $t^*=\Phi^{-1}(1-\alpha/M)\approx 3$@$M\!\sim\!10^2$ | Bonferroni 量级论证(Harvey–Liu–Zhu 2016) |
| v1 有效试验数 | $N_{\mathrm{eff}} = 1+(M-1)(1-\bar\rho)$ | 线性启发式;边界正确;v3 退役 |
| 谱参与率 | $PR=(\sum\lambda)^2/\sum\lambda^2 \to 1/\rho^2$ | 等相关下饱和 → 与 $E[\max]$ 矛盾,v3 退役 |
| SR 标准误 | $T\mathrm{Var}(\widehat{\mathrm{SR}}) = 1-\gamma_3\mathrm{SR}+\frac{\gamma_4-1}{4}\mathrm{SR}^2$ | delta method(§2.1 完整推导) |
| 极值基准 | $E[T_{(M)}] = u_M+\gamma/u_M,\ u_M=\sqrt{2\ln M}-\frac{\ln\ln M+\ln 4\pi}{2\sqrt{2\ln M}}$ | Gumbel 极限(§2.3 推导) |
| 缩放基准 | $sr_0 = \sigma_{\mathrm{pool}}\cdot E[\max\lvert X\rvert]$ | 池尺度匹配;pool 只取 null 地形(v4) |
| 选择运气 | $\bar\sigma = E[\max_i\lvert X_i\rvert],\ X\sim N(0,R)$ | 蒙特卡洛直算;CRN 单调耦合 |
| deflated p | $p = \bar\Phi\big(\frac{(\widehat{\mathrm{SR}}-sr_0)\sqrt{T-1}}{\sqrt{1-\gamma_3\widehat{\mathrm{SR}}+\frac{\gamma_4-1}{4}\widehat{\mathrm{SR}}^2}}\big)$ | B-LP;$t_{\mathrm{adj}}$ 同构用于 spread |
| 跨轨 Šidák | $\alpha_{\mathrm{track}} = 1-\sqrt{1-\alpha}\approx 0.0253$ | 双轨 FWER 精确解 |
| MinHash | $E[\hat J]=J,\ \mathrm{Var}=\frac{J(1-J)}{k}$ | 均匀置换最小哈希;尾部/交易名单家族几何 |
| 置换 p | $p = \frac{1+\#\{\lvert T^{(r)}\rvert\ge\lvert T^{\mathrm{obs}}\rvert\}}{R+1}$ | 精确 null;双侧粒度下限 $2/(R+1)$ |
| spread null 尺度 | $\mathrm{std}(\mathrm{spread}_{\mathrm{IR}}) = 1/\sqrt{n}$ | $t$ 统计量自正交性 |
| 尾部 G3 bar | $\mathrm{bar} = E[\max X](N_{\mathrm{eff}})\cdot s_0 + z_{1-\alpha_{\mathrm{track}}} s_0$ | 家族运气 + 单试验临界 |
| 正交门 | $\lvert\bar\rho\rvert \ge 0.80$ 拒;0.60–0.80 标注 | 秩相关;账本有效维度计价 |

## 附录 B:时间线(commit 锚点)

| 日期 | commit | 事件 |
|---|---|---|
| 08-16 | `94abded`/`664721e` | harness v0.1:因果性/列置换/双实现审计,**无多重检验** |
| 08-16 | `d0f32c7` | **校正进入**:z≥3 + $N_{\mathrm{eff}}$ + Šidák |
| 08-18 | `021b613` | DSR 引擎化(指纹/receipt/双池/trail 同批) |
| 08-18 | `dbfc940` | n_trials 恒 1/尺度错配/√(lnN) bug 修复;引擎侧硬计数 |
| 08-18 | `3b36de9` | deflated p 升为入册硬门;fail-closed |
| 08-21 | `d2f6934` | v3:$E[\max\lvert X\rvert]$ 直算,谱换算退役(低估 3.4×) |
| 08-22 | `e283ddf` | v4:pool_std landscape-only + reset 硬拦 + 永久 ledger |
| 08-24 | `c4904e3` | 计数与停点分离(去终身棘轮) |
| 08-25 | `58c09de`/`2a07443` | 尾部三件套;尾部独立校正 + 跨轨 Šidák |
| 08-25 | `82978dd`/`9d61497` | day-perm 置换 null / 平坦性门(均 report-only) |
| 08-26 | `d5ed63f`/`f1eeb96`/`a5525ea` | 高阶矩 $t_{\mathrm{adj}}$/拒绝分类/test 不进计价 |
| 08-27 | `25df896`/`99ccc10`/`6888f97` | 策略层:P3 账本/五道锁/test 一次性锁 |
| 08-29 | `a6fe274` | 策略线终局五门全过(G4 门 bug 修复) |
| 09-05 | `d521bf1` | 相似性防线 S1/S2/S4(重交门/宣言对账/署名) |
| 09-06 | `4e95d31` | 正交门(账本有效维度计价) |

## 附录 C:明确不做的(反面清单)

1. **不跨轨混算**:IC 线与 tail 线独立账本、独立家族几何、独立 deflation,只通过跨轨 Šidák 联结。
2. **未校准的 null 不升硬门**:day-perm、平坦性门先 report-only,待 registry × 证伪库混淆矩阵给出方向与阈值。
3. **确定性扰动不计入 deflation 的 $M$**(平坦性邻域非选择试验)。
4. **永久 ledger 只取证、不做门输入**(batch 内 $K$ 选 1 的跨族选择只有 trail 全量计数能完整计价;"K 锚点门"评审撤回)。
5. **终身计数不做停点**(防棘轮;停点是行为控制,响应边际信息)。
6. **test 区物理隔离而非计数**(看了无法入账,所以不看)。
7. **基础设施失败不烧名**(程序性拒绝可治愈;只有门的实质性判断才消耗名字)。

---

*公式与数值均按现行代码(`factor/gates.py`、`factor/evaluate.py::_LuckSampler`/`_deflated_sharpe_p`、`factor/tailgate.py`、`factor/permute.py`、`strategy_lab/gates.py`)与提交正文核验;历史错误(√(lnN) 系数、n_trials 恒 1、尺度错配、pool_std 污染、谱换算低估)的叙述以事故修复提交(`dbfc940`/`3b36de9`/`d2f6934`/`e283ddf`/`AUDIT-2026-08-18`)为准。*
