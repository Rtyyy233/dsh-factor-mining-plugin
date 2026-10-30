# 规划书：换手率定价与 tail 轨 net 换基 — 2026-08-28

> 动机（2026-08-28 对话定稿）：把换手率纳入考量——用户提的形态是
> 「对低换手因子给予奖励」。**拍板立场：定价，不奖励。**
>
> 为什么不做奖励项（λ·turnover 一类）：
> 1. **单位**：成本只能从收益序列里减；IC 是相关系数，不存在「净
>    IC」对象。spread/超额有收益单位，可以诚实地净。
> 2. **校准**：λ 没有市场来源，塞进统计判据 = 塞口味，且混合统计量
>    的 null 要整个重造而不可解释。
> 3. **可钻**：挖矿 agent 会生成超慢因子骗奖励。定价没有这个问题——
>    没 alpha 的慢因子照样过不了 G1/G3。
>
> 为什么 IC 轨的门不动：IC 轨判「信息存在性」（免成本概念），tail 轨
> 判「信息变现后剩多少」（收益空间）。换手部分是**实现选择**（任何
> 因子可降频交易），把执行成本烧进信息轨会把「有信息但需聪明执行」
> 的方向在信息层杀掉。偏好不缺席，落在四处：净诊断字段（全条目）、
> tail 轨 net 判定、漏斗平局裁决（c* 排序）、策略层消费过滤。
>
> **现状取证（2026-08-28 读码）——成本机器已在生产**：
> - `_top_n_excess`（evaluate.py:334-367）已算 gross/net/turn：
>   单边成本 = `cal.cost`（历史默认 10bps），换手 = 相邻持有集
>   对称差 / (2·top_n)（单边口径，首期 1.0），net = gross − turn·2·cost，
>   含 PIT/T+1。
> - **G1 placebo 已判 net**：topn_placebo（tail.py:65-133）全程用
>   real["net"] 对 null_nets（同成本重放）。
> - 判毛口径的只有三处：**G3 的 spread_ir**（`_topk_block` 无成本）、
>   **G2 spread 噪声门**与 **WS1 null 校准**（`spread_ir_statistic`
>   无成本参数）。
> → 本规划的迁移 = 把 G3 系输入换成 net 序列 + 补诊断 + 漏斗排序，
>   不造新模拟器。

## 0. 决策点

已定（对话拍板，不再重议）：

| # | 决策 | 结论 |
|---|---|---|
| T1 | 形态 | 定价不奖励；无 λ 系数 |
| T2 | IC 轨 | 门不动；净诊断字段照带（两轨条目都有） |
| T3 | tail 轨 | 判定基毛→net，分阶段（v1 双报 → WS1 net 重校 → 切基） |
| T4 | 换手测量规则 | 钉死 tail 规则 (k_frac=0.2, sample_step, horizon)——放开再平衡频率 = 新试验进指纹 |
| T5 | 成本进键 | cost 值 + cost_model_version 进 tail 块/registry 条目；变更 = 新键重计（结果不可比） |

待定（校准期定档，不阻塞实现）：

| # | 决策点 | 默认 |
|---|---|---|
| C1′ | 切基时点 | WS1 net 段重跑 + bar 数字定档后（照 WS4 legacy→corrected 节奏） |
| C2′ | cost-fragile 标注下限（ic 轨条目可选标注） | c* < 2×当前成本假设时标注；标注不是门 |
| C3′ | c* 呈现口径 | 每再平衡期单边 bps 为主，年化变体随附（换算系数随 sample_step/日历写入文档） |

## 1. 设计

### WS-T1 诊断三件套（先做，无门语义变化）

1. **turnover**：tail 块顶层 `turn_avg`（`_top_n_excess` 的 cal.top_n
   规则已有此数，evaluate.py:120 处 topn 块照旧；新增 tail 规则
   (k_frac) 口径的 `turn_tail`——`_topk_block` 顺手算连续持有集对称差）。
2. **break_even_cost** c* = mean(gross) / (2·mean(turn))（单边 bps）：
   **每单位换手赚多少**——免假设报法，不先拍成本数字即可比。
   「奖励低换手」的严格版本：同档 alpha 下 c* 高者胜。进 evaluate
   响应、trail 条目、registry 紧凑视图（与 tail 线规划 WS-A 的字段
   白名单合并交付）。
3. **rank_autocorr**：截面秩 t vs t+h 相关（按 horizon），向量化
   （逐行 argsort + 行对 corr，毫秒级）——rank autocorrelation 超时
   事故的正名：想法对，实现要向量化。信号持续性诊断，预测任何组合
   规则下的换手。

### WS-T2 net 换基（分阶段）

1. `_topk_block` 增换手序列：相邻采样行 top-K 集对称差 → turn_t；
   `net_spread_t = spread_t − 2·cost·turn_t`；
   `net_spread_ir = mean(net)/std(net)`；net 版高阶矩
   （spread_moments 同式，供 WS4 t 修正）。
2. `spread_ir_statistic` 加 `cost` 参数（默认 None = 毛口径不变）：
   G2 噪声门与 WS1 landscape spread 段在 net 模式下同口径调用。
   合成世界无 env——cost 由调用方（bridge）按 cal.cost 传入。
3. 阶段：
   - **v1 双报**：tail 块同时给 spread_ir（毛，判定照旧）+
     net_spread_ir（陪跑 + registry 标注 `net_basis: "reported"`）；
     MINING_CONFIG 加开关 `tail_net_basis`（默认 false）。
   - **WS1 net 重校**：null 校准 spread 段按 net 口径重跑（随机树
     同样付成本——null 分布整体左移，s0/bar 重定档；s0 教训：解析
     式不可信，重跑定数字）。
   - **切基**：`tail_net_basis: true` 后 G3/G2 判 net_spread_ir，
     G1 本就 net 不动；registry 标注 `net_basis: "adjudicated"`。
4. 门公式形态零改动——G1/G2/G3 原样，变的只是喂进去的序列与校准
   它的 null。这是切换低风险的原因。
5. 例子（回归测试期望，单边 10bps，生产口径 net = gross − 2·cost·turn
   ——换手的买卖两侧各付一次单边成本）：

   | 因子 | 毛超额/年 | 年化单边换手 | 拖累 | 净超额/年 |
   |---|---|---|---|---|
   | 快 A | 8% | 200× | 2×10bps×200 = 40% | −32%（死） |
   | 慢 B | 6% | 5× | 1% | +5.0%（活） |

### WS-T3 漏斗（「奖励」唯一合法住所）

explore 双列表（tail 线规划 WS-C）扩三列表：`top_k_ic` / `top_k_tail`
/ `top_k_net`（按 c* 排序——免假设，优于按 net_spread_ir 排序：后者
依赖成本档位）。landscape 在场时附 net 分位查表。语义 = 统计平局
裁决，非判据篡改。

## 2. 生产行为变化

- v1：evaluate/submit 响应与 registry 条目多 turnover/c*/rank_autocorr/
  net_spread_ir（陪跑）；判定全部照旧——agent 与账本零风险增量。
- 切基后：tail 轨 G3/G2 判 net；高换手因子的入册难度如实上升
  （成本定价输出，非人为系数）；IC 轨照旧。
- WS1 重校后：landscape spread 段数字整体左移，null 分位查表换版
  （landscape 键带 cost_model_version，旧版保留可查）。

## 3. 实现清单（文件级）

| 文件 | 变更 |
|---|---|
| `factor/tail.py` | `_topk_block` 增 turn_t 序列与 net spread；`tail_metrics` 输出 `turn_tail` / `net_spread_ir` / `net_spread_moments` / `break_even_cost` / `rank_autocorr`；`spread_ir_statistic` 加 `cost=None` 参数（net 模式） |
| `factor/tailgate.py` | G3/G2 输入接线：`tail_net_basis` 开关下读 net 字段（缺省回毛 + 标注 legacy）；尾块/条目记录 `cost_used`（bps）与 `cost_model_version` |
| `factor/random_gen.py` | null 校准 spread 段 net 模式（同成本重放）；landscape 键带 cost_model_version |
| `bridge.py` | evaluate/submit/registry 响应透传新字段；registry 紧凑投影白名单加 c*（若 tail 线 WS-A 未落地则独立加） |
| `state.py` | MINING_CONFIG 增 `tail_net_basis`（默认 false）与 `cost_model_version`（默认 "flat:v1"） |
| `python/tests/` | 新增 `test_turnover_net.py`；landscape/null 相关测试适配 net 模式参数 |

## 4. 测试细目

1. **换手算术**：构造已知相邻 top 集合（全换/半换/不动）→ turn =
   0/0.5·K 变动比/0 精确；首期 1.0。
2. **net 回归**：合成序列使毛均值/换手已知 → net 均值精确等于
   gross − 2·cost·turn；§1.5 例子表作参数化断言（8%/200× → 净为负）。
3. **c\***：公式 mean(gross)/(2·mean(turn))；零换手策略 c* 缺省不炸。
4. **rank_autocorr**：与朴素逐日实现一致（小面板对照）；向量化版本
   在生产规模面板上 <100ms（perf 断言——事故正名的验收标准）。
5. **双报阶段**：`tail_net_basis=false` 下 G3 判毛、net 字段只陪跑；
   `=true` 下判 net、毛字段转陪跑；两阶段 registry 标注正确。
6. **null 左移**：随机树 net_spread_ir 分布均值显著低于毛版（重校
   的前提证据）。
7. **成本进键**：cost_model_version 变更 → 尾账本新键；同键重评 =
   更新不新增。
8. 全量回归绿（233+ 项）。

## 5. 部署

pytest 全绿 → commit（不碰公共 repo）→ 重装 site-packages →
**重启 dsh web 由用户执行**。TS 零改动。

## 6. 不做的事

- 不改 IC 轨的任何门（λ 混合统计量明确否决——单位不通、null 不可
  校准、可被慢因子钻空子）；
- 不放开再平衡频率旋钮（钉死按 horizon 采样；放开 = 进指纹的新试验）；
- 不动 G1（本就 net）与 `_top_n_excess` 成本公式；
- 不统一 G1 的 cal.top_n 与 G3 的 k_frac 两条规则（既有缝隙，记录
  在案，数字漂移再议）；
- 不动策略层规划（其成本档位与本规划 C1′ 同源对齐即可，双层口径
  不漂移）；
- 不做年化 c* 的门（只呈现，不做判据）。
