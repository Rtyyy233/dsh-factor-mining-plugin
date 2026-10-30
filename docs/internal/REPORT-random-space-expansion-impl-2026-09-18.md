# REPORT: 随机搜索空间扩充实施记录

- 日期：2026-09-18
- 任务来源：用户三拍板——①算子集内容纳入指纹（正修）②结构扩充由实施方设计③算子按调研方案第一批。
- 调研依据：`RESEARCH-random-search-space-expansion-2026-09-18.md`（P0/P1/P2 范围）
- 测试：`python/tests/test_random_gen_expansion.py` 14 项全绿 + 既有 random_gen 系列通过（全量回归见文末）

---

## 交付内容

### P0 前置必修（缝）

| 缝 | 修复 | 位置 |
|---|---|---|
| 缝1 指纹 | **null 地形绑定含算子集指纹**：新 `_landscape_env_fingerprint` = 环境三元组 + `+op:` + `opset_fingerprint(effective_operator_set(stateRoot))`。算子集指纹覆盖：算子名单+参数类别+**算子实现源码哈希**（改实现=改 null 分布）+窗口/延迟/阈值采样集+模板集与占比。写入（null 校准）与校验（`_landscape_fingerprint_status`）同一合成。**作用域只绑地形**——receipt/缓存/trail 的环境指纹不含算子集（手写因子不依赖随机生成器采样配置） | bridge.py + random_gen.opset_fingerprint |
| 缝2a | ts_momentum 保号除零保护（分母零/贴零的停牌限价列曾产 inf；正常正价格下数值逐字节不变） | ops.py |
| 缝2b | 生成树输出健康门 `matrix_health`：后半面板 finite 占比<0.5 / \|p99\|>1e12 / 零方差 → 病态。explore 标 error 不进选种；null 校准跳过并计数（`n_pathological` 进 landscape——此前被 IC mask 静默吃掉） | random_gen.py + bridge.py |
| 缝2c | explore 叶子按 env.amount 可用性过滤（与 null 校准同款 `env_leaves`） | bridge.py |
| 缝3（附带发现） | **ts_slope 死算子复活**：时间索引误用窗口长度构造 (w,N) 矩阵配全长 index——任何含 ts_slope 的树整棵 ValueError 被生成端 except 静默吞掉，ts_slope 实际从未参与过采样。修复=全长行号计数器（窗口内与窗口内相对位置协方差等价，同数学）。新增回归测试 `test_all_registry_ops_evaluate`（注册表内每个算子必须在合成面板上可评估）堵此类缺口 | ops.py |

### P1 结构扩充（空间类型 + 条件结构 + 模板）

**空间类型系统**（`_space`，最小类型规则，只约束组合合法性不改数学）：
- 空间：leaf=raw；ts=raw；cs=norm；unary 传递；mul/div 双 norm 才 norm；gt/lt=bool；where=分支空间。
- R1：cs 输入必 raw（cs_rank(cs_zscore(x)) 按日仿射不变性恰等于 cs_rank(x)，纯冗余——FARM_REVIEW 实测废树类）。
- R2：add/sub/minv/maxv/signed_diff 与 gt/lt 两子树同空间（单位匹配）；where 两分支同空间。**mul/div 自由**（量腿加权 mul(价格腿,排名腿) 正是战役兑现结构，不得误伤）。
- 实现方式：`_grow` 增加 `need` 参数（any/raw/norm/bool）向下传播；force_ts/force_cs 与空间冲突时的边界解：norm+force_ts → cs(ts(leaf))；raw+force_cs → ts(cs(leaf))；force 标志继续沿子树传播，≥1ts+≥1cs 保证不变。

**条件结构算子**：`gt/lt`（逐元素比较→0/1）+ `where(cond,a,b)`（条件三分支，cond·a+(1−cond)·b）。战役六入册全部是"门+腿"形态而旧语法树无法表达条件分支——这是比算子数量更大的缺口。gt(c,o) 类叶子对门控语义合法（与 binary 算术的禁全叶约束区分）。

**结构模板**（`TEMPLATES`，出身=结构先验，非零先验，诚实标记）：
| 模板 | 形状 | 战役出处 |
|---|---|---|
| leg_weight | mul(ts腿, cs(叶)) | 量腿加权三度兑现 |
| hedge_resid | cs(ts_resid(y,x,w)) | 七元腿负相关对冲=残差结构 |
| window_spread | cs(sub(同字段同算子异窗)) | triplewin 族 |
| cond_gate | cs(where(gt(ts腿,ts腿), ts腿, ts腿)) | "门+腿"形态 |
- 占比 `template_share` 默认 0.3，`disable_templates` 可禁单个；配置走 operator_set.json 覆盖（TS 层 override 为 json 透传，新键不被剥——描述文案已更新）。
- **null 校准同通道采样**（`generate_one` 共用入口）：墙必须覆盖实际采样空间；模板与占比进算子集指纹，改配置=改墙口径=须重校准。
- 出身记账：渲染源码首行 `# tree: ... | template: <名> | expression: ...`；explore 三列表（top/top_tail/top_net）每条携带 `template` 字段。

### P2 算子第一批（无新字段依赖）

| 算子 | 类别 | 语义 |
|---|---|---|
| ts_resid(y,x,w) | ts 双输入 | 滚动 OLS 残差（对冲腿机械化） |
| ts_beta(y,x,w) | ts 双输入 | 滚动 OLS 斜率（敏感度/对冲系数） |
| ts_decay_exp(x,w) | ts | 指数衰减加权均值（公比 0.5 近端高；测试抓出首版权重反向已修） |
| hump(x,h) | ts | 换手抑制单步近似（相对变化<h 保持前值；新参数类 "h"，DEFAULT_HUMPS=[0.01,0.05,0.10]） |
| gt/lt/where | cmp/cond | 见 P1 |

分组族/共矩族按调研留待字段腿（P3）落地后。

## 部署注意（用户操作）

1. **重启 DSH 后 null 地形判 mismatch（设计内）**：算子集变更 → 旧 landscape 失效 → pool_std 缺失 → deflated p 拒绝给出 → **入册暂停直至重跑校准**。两线（h10c30 与 ETF root）各跑一次 `factor.random_generate mode=null-calibration`（或等 runner 侧触发）。墙读数会变：新算子+ts_slope 复活+健康门滤掉病态树，null 分布更宽/更干净皆有可能——这是诚实重定价，不是事故。
2. 评估缓存不受影响（C1 缓存键不含算子集）；receipt/trail 口径不变。
3. ts_momentum 保护：正常正价格面板数值逐字节不变，仅零/贴零分母列从 inf 变有限值。
4. ts_slope 从"从未产出"变"可用"：有效采样空间变大，已反映在算子集指纹中。

## 第二批算子（2026-09-19 追加，用户"算子可以再扩充一些"）

选型原则不变：不加新数据字段、全部向量化（或与既有慢算子同成本档）、每族有战役/入册因子出处。**17 个新算子，注册表 42→59**：

| 族 | 算子 | 出处 |
|---|---|---|
| 加权族 | ts_wmean(x,wgt,w) 加权滚动均值 | 量腿加权的单算子化（战役三度兑现，此前只能 mul 组合） |
| 回撤/区间族 | ts_drawdown、ts_range_pos、ts_arg_max、ts_arg_min | 距高点/区间位置/极值新近度（hv 第四域"高价惩罚"的邻域）；arg 族与 decay 族同成本档（rolling.apply） |
| 稳健统计族 | ts_median、ts_iqr、ts_winsor、ts_rank_corr、cs_robust_z | 稳健主题（ML 特征卡 winsor 主题；ts_winsor=volmom_winsor_only 入册因子的机械化） |
| 持续性族 | ts_streak | 符号持续天数（每列 O(T) 纯 numpy；实现差一已修——新符号首日记 1） |
| 共矩族 | ts_coskew、ts_triple_corr | 尾部相依战役手工构造的机械化（**首个三输入算子**，生成器 THREE_INPUT_TS 支持） |
| 截面中性族 | cs_demean | 去均值不除 std（低分散日不放大） |
| 变换族 | log_signed（带号对数压缩）、softsign（有界保号）、sfdiff（无量纲差，免同空间约束） | 手写构造高频使用的带号压缩此前缺失；sfdiff 补跨量纲比较 |

配套：TWO_INPUT_TS +3（ts_wmean/ts_rank_corr/ts_coskew）；THREE_INPUT_TS 新集合；sfdiff 刻意不在 SAME_SPACE_BINARY（无量纲差跨空间合法）；Node.to_code 渲染泛化为任意元数（三输入+参数此前会丢第三子树）；全算子可评估回归测试补三输入分支；新增 10 项数值测试（共偏度/三阶共矩对暴力逐窗参照 1e-8）。

指纹注意：新算子自动进算子集指纹——**与第一批共用同一次待执行的重启+null 重校准，无额外成本**。



- **不 bump ENGINE_VERSION**：实现变更已通过算子集源码哈希进地形指纹（比全局版本号更精准——receipt/test_lock/trail 不受无关注联失效）；先例=历次引擎修复均未动静态版本串。
- ENGINE_VERSION 静态串问题（discipline.py 三元组）留档：将来若有 ops.py 之外的影响评估数学的引擎变更，仍需人工 bump。

## 测试

- 新增 `tests/test_random_gen_expansion.py`：新算子数学（resid/beta/decay/hump/where）·动量除零·空间规则性质测试（300 棵）·模板结构+渲染+执行·占比开关·配置覆盖·健康门·叶子过滤·指纹敏感度·null 校准带模板·**全注册算子可评估回归**。
- 既有 test_random_gen.py / test_random_gen_dual_scan.py 不变通过（generate_tree 旧签名兼容）。
