# P6 校准运行册（待真实数据）— 2026-08-27

> 状态：**待跑**。P0-P5 已交付并通过测试（合成面板）；本册记录首批
> 真实账本上必须完成的校准项与判据（规划 §7 P6 / §0 C1-C4）。
> 数字是结构不是定稿——按因子层教训（解析 s0 实测低估约 2 倍），
> 首批真实账本上校准后定档。

## 前置

1. `pip install -e python/`（本 IDE Python 环境，含 dsh_strategy_lab）。
2. 真实 ETF 面板 → `build-env --env-spec` → npz；factor-refs 指向
   生产因子 registry（只读）已 accepted 条目。
3. 跑 ≥3 个真实策略（不同族）各走 evaluate → submit，落首批
   `.strategy-lab/strategy_trail.json`。

## C1 成本档定档

- 现默认：佣金 2.5bps + 滑点 5bps + 现金腿年化 1.5%（`CostModel`
  字段，`--cost-file` 可调）。
- 判据：对首批策略扫 2-3 档成本（如 (2.5,5) / (5,10) / (10,20)），
  看接受结论是否对档位敏感——敏感面大的策略族（高换手）在报告中
  必须标注档位依赖；定档后写死进 config 并在 skill/README 声明。
- 注意 `cost_model_version` 进指纹：跨档结果不可比，勿混账本。

## C2 placebo 抽样下限与预算

- 现默认：evaluate 轻 m=30（degraded 标注）/ submit 权威 m=60、
  预算 240s（给 worker 墙 300s 留头部）。
- 判据：真实面板上 null Sharpe 分布的 σ 估计稳定性——m=60 下 σ 的
  相对误差应 <15%（bootstrap 重抽样核对）；不达标升 m 或收紧 degraded
  语义（拒绝用轻值兜底）。

## C3 启动点扰动门（v1 软门 → 硬门与否）

- 现状：≥8 出生日偏移点，报告 delta 中位/IQR/正占比，不判拒。
- 判据：对 matched null（G1′ 的 null 路径集）计算同网格的 delta
  离散度；真实策略的 IQR 显著超出 null IQR（如 >p95）才升硬门，
  阈值从 null 分布分位数取，不拍脑袋。

## C4 随机游走门显著性口径

- 现默认：G2′ z ≥ 3 即拒（与噪声门同口径），z 不可判定 = 不判过。
- 判据：首批账本上 RW z 分布的零假设校准——m=10 世界下 z 的实际
  水平集；若假阳性率异常（合成校准已测 ≈0），考虑升世界数或改
  block 校正。同时构造一个真实 exploit 策略验证 G2′ 真的能拒
  （§8.6 的另一半：v1 模拟器暂无已知成交漏洞——出现可疑策略时
  先补 exploit 用例再改门）。

## null z 分布验证（placebo 校准，§8.3 的真实面板版）

- 首批真实面板上：拿 1 个被 G1′ 拒收（或故意平庸）的策略，其
  placebo null Sharpe 标准化后应 ≈ N(0,1)（均值 |·|<0.3、std ∈
  [0.8,1.2] 容差）；偏离 = null 构造有偏（matched 近似的换手偏高
  方向需要重设计）。

## 完成动作

- C1-C4 定档值回写：`simulator.CostModel` 默认 / `placebo.py` 常量 /
  `gates.g2_artifact` 阈值 / `robustness.startup_perturbation` 升硬门
  与否——各自带测试更新；skill（strategy-lab）同步修订数字。
- 校准结论记入本册下方（数据/日期/结论三要素）。
