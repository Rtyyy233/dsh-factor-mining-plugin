# 择时树特征漏斗执行简报(2026-09-03)

> 任务书 PLAN-etf-timing-tree-features(v3.1)§1-§2 的实施记录:
> 载体归类预注册 → 三层流水线生成 55 候选 → L0/L1/L2 零标签漏斗 →
> L3 方向登记 → 特征卡冻结。脚本与数据均在 `python/scratch/`。

## 1. 层 A 载体落地(carrier_registry.json 预注册)

- 归类素材:etf_universe.csv(1772 只,名称+上市日期)为主,名称规则
  见 registry;三轮修正后才干净:**ETF 联接/LOF/FOF/混合/增强全部排除**
  (载体品种纯度),「标普中国A 股」类外方编制 A 股指数不判海外,
  「国证/中证石油天然气」类 A 股行业股不判商品。
- 终版成员(纯 ETF):**bond 23 / gold 7 / commodity 3 / overseas 94 /
  broad 161**;逐日成员 = 归类 AND 当日有效 AND 面板内 ≥252 日 AND
  250 日日均成交额 ≥2000 万(Man 流动性标准)。
- **数据边界(dev 区 2015-2021)**:gold/broad 健全;bond≈511010 独苗
  (成员≥3 要到 2022);commodity 2020 年前缺位;overseas 2016-2021 仅
  2-5 只。债券/商品两载体在 dev 区不可用——非构造缺陷,市场结构边界。

## 2. 流水线与候选(ts_pipeline.py / ts_candidates.py)

- 载体序列:篮子等权收益 + ex-ante 波动率标准化(EWM63,shift(1));
- 55 候选 = 连续族 7 形态×5 载体 + 离散族 2×5 + 相对族 5 + 聚合 4 + 量能 1;
- 构造参数修正 1 处(记录):状态化基准 expanding 750 → **rolling 500**
  (首轮漏斗实证 750 暖机吃掉 dev 大半;rolling 对非平稳更稳健)。

## 3. 漏斗结果(ts_funnel_report.json)

**55 → L0 30 → L1 28 → L3 PASS 17 → 特征卡 8。**

门口径修正 3 处(均有记录,杀错对象而非放水):
1. 覆盖门:暖机期占比 → **首有效值后连续性 ≥95%**;
2. 切换率下界 1/60 → **1/250**(1/60 误杀 breadth/corr 等慢状态量;
   常数情形由非退化门覆盖;上界 1/5 杀噪声放大器不变);
3. time_stable:符号占比漂移(反映 regime)→ **构造尺度稳定**
   (滞后自相关差 <0.2 + std 比 [0.5,2]);neigh_flat 按任务书口径
   「方向不变」(≥0.5),代码 v1 的 0.85 是误加严。

死亡名单要点:bond/commodity 全灭(数据边界);gold__chan_pos126
冗余(ρ=0.90 vs tsmom_ms);hl_spread/turnover_state 切换率超上界
(噪声放大器);rel__bond_eq L3 前剔除(dev 段 bond 未过成员门槛,
证据力弱——一致性修正)。

## 4. L2 功效账(硬数字)

- dev T=1700、h=20 → **N_eff≈85**(块折减);
- Bonferroni 下 K=8 需单特征 AUC≥0.635——真实时序特征 AUC 常在
  0.55-0.62,**严格校正只养得起 K≤4**;
- 采「方向登记」口径(L3 不称显著,裁决移交计价层 deflation)→
  **D8 卡上限=8**(任务书 D8 拍板项,按此落地)。

## 5. L3 方向登记(ts_l3_report.json;计价 27 次)

标签=宽基篮子 fwd20−26bps>0;仅 dev 前半段。TOP:

| 特征 | AUC 前半 | 年一致 |
|---|---|---|
| broad__eff60(路径纯净度) | 0.657 | 1.00 |
| gold__asym_vol63(非对称波动) | 0.645 | 1.00 |
| agg__corr200(拥挤温度计) | 0.636 | 1.00 |
| overseas__chan_pos126 | 0.629 | 1.00 |
| overseas__dd_depth126 | 0.628 | 1.00 |
| overseas__tsmom_ms | 0.622 | 1.00 |

## 6. 特征卡冻结(8 席,D8 预算)

broad__eff60、gold__asym_vol63、agg__corr200、overseas__chan_pos126、
overseas__dd_depth126、overseas__tsmom_ms、broad__tsmom_ms、
agg__dispersion(机制覆盖:趋势质量/非对称波动/拥挤/通道/回撤/多尺度
动量×2/分化度;两两 |ρ|<0.90 由 L0 冗余门保证;含一离散备份
broad__tsmom_cocount 未入卡——树卡满)。

## 7. 下一步(策略层阶段,待启动)

新状态根 + 基座书(D7=T-P5 待移植) + 日频重叠标签树训练
(depth-2 锚点/depth-3 变体)+ 三方对照计价(检验十件套)。
