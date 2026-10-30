# dsh-factor-mining-plugin

**让 LLM Agent 在引擎强制的研究纪律下自主挖掘截面因子。**

本项目是 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）平台的因子挖掘插件，
包含一个**可独立使用的 Python 研究纪律引擎**（评估/审计/防作弊校验）与 DSH 平台的
TS 工具层接入。不依赖 DSH 也能把 Python 引擎当作「带诚信护栏的因子评估库」直接使用。

## 主要设计

主要的设计来自于作者在使用Agent进行量化研究时遇到的问题，并逐一使用纯代码的方式解决，以限制Agent犯错。
测试以中小规格模型为主，所有设计是以防止小模型出现数据窥探、错误代码、无效搜索等问题进行的设计，模型的自由度仅在搜索信息、设计因子这一层。

### 前视偏差：

#### OOS划分：
数据默认使用60/20/20的比例划分development/selection/test三区，可修改；test区有锁机制，只有用户首肯才会被动用。

#### 未来数据：
Agent提交的因子会使用causality.py检验因子是否使用未来数据：
1.抽样多个时间点t（目前为6个，有需要可自己配置），将t之后的数据置换为随机噪声进行检验（这来自于我的经验，即使你物理隔离了数据，Agent也会在写的代码中出现data[t+1]这种data-hacking行为）
2.抽样多个时间点t（注释同上），将t之后的数据置换为NaN，检验输出是否出现NaN

#### Ic_IR 计算：
由于时序数据的自相关性，Agent在采样计算Ic_IR时会由于重叠的采样窗口过度夸大因子表现，`adapters.py`和`evaluate.py`会自动检验采样步长和时间窗口长度，对重叠窗口予以拒绝。

### p-hacking:
1.插件初始化时会生成null landscape来锚定当前数据集的特性（随机因子在同一环境下的IC_IR经验分布，给出"在这个数据上瞎蒙能蒙多好"的基线）
2.对产出的因子会做column-perm test，取|z|=3为阈值
3.多重检验校正（deflation 墙）：系统机械记账Agent的**全部**评估试验N（包括失败的），入册门槛取 E[max|X|]——N 次试验里"最幸运的纯运气"该有的水平，随 N 单调抬升。与教科书校正不同的是，试验间的相关矩阵是**实测**的（对每个试验留存的相关性指纹做蒙特卡洛直算），避免了 Bonferroni 类校正在高相关试验族上失真的问题。IC 轨（秩相关）与尾部轨（多头名单重叠）各记一本独立账。算子库或数据面板变更会改变环境指纹，强制重跑 null 校准——墙的口径不会静默漂移。

### 重复探索

#### 因子库设计
因子库采用双库结构，原始库中使用贪心算法挑选Ic_IR最高、且截面相关性低的因子族构成强因子库；原始库中因子按相关性聚类，新因子与旧因子的相关性通过对不同簇的相关性计算实现，以避免因子数量爆炸带来的计算爆炸。另设正交门：新因子与在册因子的平均秩相关过高会被直接拒绝。

#### 探索轨迹
与通过算子进行穷举不同，项目将因子设计的自由度交给LLM，在每轮搜索中，LLM需要在trail中记录自己的探索方向、结果以及推理，并在下一次探索中排除已搜索过的方向；灵感源以「文献检索 / 随机生成 / 机制自提」三模式轮转出题，另有概念碰撞卡作跨域刺激。

#### 永续循环
挖掘循环**没有自动终止**——不设"挖满N轮/挖到K个因子就停"的终点，终态只有死线（用户设定的时间预算，落盘持久化、重启自动恢复）与用户指令。引擎侧以三原语驱动循环：**继续**（扫描未结算的评估/未派发的假设等欠账）、**扩张**（枚举未覆盖的前沿坐标、按边际价值排序）、**救援**（对失败方向再定价/构造正交组合/送孵化通道复检）。所有方向性偏差（绕圈、草率收弧、家族产出异常）以红字告警暴露给模型，但不替模型做决定。

#### 推导优先与导航（防同质化的机制层）
LLM 挖因子有个实证的毛病：默认从记忆里搬经典公式（动量/反转/波动率），越挖越像。对策不在提示词里喊"要创新"（实测无效），而在结构上改变"哪条路省力"：
- **预期先注册**：评估之前，Agent 必须在推理日志（journal）里写下机制假设与可检验预测（方向/半衰期/换手量级……），并点名因子代码用到的算子与字段；评估响应自动附「预期 vs 实测」的机械并置（镜像），Agent 据此结算学到了什么。
- **提交顺序门**：提交入册时引擎核对——预测条目是否真实存在于评估之前（时间戳）、代码里的构造是否都能在推导文字中对账；"先抄公式再补故事"在门口被抓住（先影子记账后强制拒绝，按预设规则自动切档）。
- **导航素材**：生成上下文注入三样纯名词素材——覆盖地图（哪些字段×算子区域被挖了多少）、区域采尽三档读数（前缘/边际衰减/采尽）、高频构造禁忌表（被用烂的构造模式点名禁止再当主体）。零分数、零数字，给方向感不给模仿对象。
- **逃逸距离遥测**：每轮量化"第一念头"与"最终提交"的构造重合度——重合度过高=这轮没逃出惯性。历史基线已实证：低逃逸桶的预测准确度最差，即"回忆路径"的假预测会在数据上露馅。

### 收益可实现性

#### 多头端收益：
由于中国A股做空限制的特性，Agent在探索因子时除了Ic_IR以外，还会额外计算Top-N多头收益，并在注册因子的时候一并写入

#### 扰动敏感性：
`evaluate.py _train_sensitivity` 会对因子的启动时点、不同时区的表现做计算，以查明因子对扰动的敏感性

#### 荒谬值：
如果某个因子通过了所有检验，但是出现诸如Ic_IR=5,CAGR=200%这类荒谬结果，会在因子库中特别标注，用以人工核验（直接构造测试，在某个因子的所有测试中p值过关、无前视的情况下，会触发此设计，但大概永远用不到吧）

## 架构

```
┌─ Python 引擎（dsh-factor-mining 包，可独立使用）─────────────┐
│ bridge.py        JSON-RPC 分发 + 全部纪律护栏的执行点          │
│ factor/                                                       │
│   evaluate.py    IC/IC_IR/列置换/deflation/尾轨/敏感性 数学核心│
│   causality.py   噪声扰动前视检验（缓存：source+环境指纹）      │
│   audit.py       独立双实现审计（Spearman tie 对齐等）          │
│   random_gen.py  随机因子生成器（类型文法+模板+健康门；          │
│                  explore + null 地形校准双模式，算子集进指纹）   │
│   ops.py         向量化算子库（61 个：ts_*/cs_*/二元/一元/       │
│                  比较/条件，含残差/共矩/稳健族，因果安全）       │
│   tailgate.py    尾部轨评估（多头名单 MinHash 指纹/价差诊断）    │
│   tailwall.py    尾墙 v2（名单重叠即相关的 E[max|X|] 校正）     │
│   evidence.py    因子真实性证据账本（时间稳定/截面/构造/外部）   │
│ discipline.py    指纹/receipt/结构签名/低效扫描/荒谬值判据      │
│ journal.py       推理日志：预期预注册/预期-实测镜像/蒸馏/继承    │
│ primitives.py    loop 三原语判定（继续/扩张/救援，影子态）      │
│ scheduler.py     研究预算调度器（派生图+边际价值打分，影子态）   │
│ navigation.py    导航素材（逃逸距离/覆盖地图/采尽三档/构造禁忌）│
│ ordergate.py     提交顺序门（预注册时序+推导链对账，影子态）    │
│ divergence.py    多底模型混布编排（未启用，等第二底模）         │
│ collision.py     概念碰撞卡（跨域灵感刺激）                     │
│ incubate.py      孵化通道（样本外复活证据的唯一豁免口）          │
│ worker.py        用户因子代码的子进程隔离执行                   │
│ state.py         状态文件（trail/registry/pool/开关，原子写）   │
│ data/adapters.py 数据适配（parquet/csv/分钟特征，long/wide，     │
│                  dev/sel/test 三区划分）                       │
└──────────────────────────────────────────────────────────────┘
              ▲ JSON-RPC over stdio 子进程
┌─ TS 层（DSH 平台）───────────────────────────────────────────┐
│ ts/factor-mining/          Service Definition（类型契约）      │
│ ts/factor-mining-python/   Provider（驱动 Python 子进程）      │
│ ts/tool-factor-mining/     Consumer（factor_* 工具注册 +       │
│                            drive 注入器：送达三通道/死线/       │
│                            卡死恢复/矿工名册，自动拉起循环）     │
│ ts/bundle-factor-mining/   bundle（挂载到 DSH profile）        │
└──────────────────────────────────────────────────────────────┘
              ▲ 工具调用
        LLM Agent + preset/factor-mining/skill/factor-mining/SKILL.md
        （行为纪律：分层自主制——启动决策用户拍板，挖掘过程全自主）
```

运行时状态全部在用户 stateRoot（默认 `<cwd>/.factor-mining/`）：数据配置、
null 地形、registry、双层 trail（引擎硬事实 + Agent 叙事）、推理日志、双池、
test_lock、切档审计。**本仓库不含任何用户数据。**

更深一层的架构文档（总览卷+十二分篇：永续不变量/死线机制/送达三通道/
多重检验墙的完整推导与历史重放等）从 `docs/HARNESS_ARCHITECTURE.md` 进入，
文档地图见 `docs/DOCUMENT-MAP.md`。

## 使用指南

### A. 独立使用 Python 引擎（不需要 DSH）

```bash
pip install -e ./python          # 或 pip install dsh-factor-mining（发布后）
```

```python
from dsh_factor_mining.bridge import Bridge

b = Bridge(state_root="./.factor-mining", execution_mode="in_process")

# 1) 配置数据（long 格式 parquet/csv：symbol/date/OHLCV/amount）
b.dispatch("config.save", {"config": {"version": 1, "environments": {
    "etf": {
        "source": {"type": "parquet", "path": "my_panel.parquet"},
        "layout": "long",
        "mapping": {"symbol": "symbol", "date": "date", "open": "open",
                    "high": "high", "low": "low", "close": "close",
                    "volume": "volume", "amount": "amount"},
        "calibration": {"profile": "cn_etf_daily", "horizon": 20,
                        "cost_bps": 10,
                        "dev_end": "2023-08-01", "sel_end": "2025-02-01"},
    }}}})
b.dispatch("data.load", {"envId": "etf"})

# 2) 校准池子难度（随机因子的 IC_IR 经验分布基线，正式决策建议 n≥200）
b.dispatch("factor.random_generate",
           {"envId": "etf", "mode": "null-calibration", "n": 200})

# 3) 评估因子——前视/多重检验/荒谬值全部引擎强制
diag = b.dispatch("factor.evaluate", {
    "envId": "etf", "stage": "development",
    "source": "def factor(env):\n"
              "    import pandas as pd\n"
              "    c = pd.DataFrame(env.c)\n"
              "    return (c / c.shift(20) - 1.0).values\n"})
print(diag["verdict"], diag["ic_ir_train"], diag["deflated_train"])

# 4) 入册前先在 journal 注册预期（评估后引擎回传"预期vs实测"镜像）
b.dispatch("journal.append", {"level": "live",
           "title": "H-1 registered 20日动量",
           "content": "预期：方向+，半衰期约20日，换手中……\n"
                      "ref 待评估后补 source_hash",
           "source": "<与评估相同的因子源码>"})

# 5) 通过全部门控后入册（receipt 逐位校验数字，不一致=拒收）
b.dispatch("registry.submit", {"envId": "etf", "name": "mom20",
                               "source": "<同上>", "signal": "20日动量",
                               "diagnosis": diag})
```

因子写法契约（`def factor(env)`，只用过去数据，输出 `(T,N)` 数组）与全部工具的
方法清单见 `docs/FACTOR_GUIDE.md`，设计决策与理由见 `DESIGN.md`。
单独用作评估库时，journal 注册不强制（提交顺序门对无 journal 的用法自动豁免）；
整套「预注册→镜像→对账」是为 Agent 自主挖掘设计的，见下。

### B. 作为 DSH 插件使用

```bash
# 在 DSH profile 中安装 4 个包 + preset（按 DSH 插件机制）
dsh plugin add <bundle tarball>          # 或按 profile 的 bundles 机制挂载
cp -r preset/factor-mining/skill/factor-mining ~/.zcode/skills/
# （旧版宿主技能目录为 ~/.agents/skills/，二选一）
```

重启宿主后对 Agent 说「我要做因子挖掘」即可。SKILL 会引导 Agent 走完整流程：
数据探测 → 三区确认（用户拍板）→ null 校准 → 挖掘起点确认 → 自主挖掘
（journal 预注册 → causality → evaluate → 镜像结算 → trail 留痕 →
audit/walk_forward → 入册门链）。TS 侧驱动注入器在会话结束后自动拉起下一轮，
死线到点收官并落盘，宿主重启后自动续跑——挖掘不停，除非死线或你叫停。
test 区在最终验收时一次性消费（`stage="test"`），锁死后不可重置。

### C. 关键口径（换口径=换研究，报告中必须声明）

- `horizon`：前瞻收益窗口（默认 20 bar）；执行模型 t1（信号 T 收盘 → T+1 开盘入场 → T+H 收盘出场）
- `dev_end`/`sel_end`：三区分界（必须成对出现；不写则按数据 60/20/20 自动保底并标注 auto）
- `ic_sample_every`：IC 采样步长（必须 ≥ horizon，强制不重叠口径）
- `profile`：`cn_etf_daily` / `cn_stock_daily` / `cn_etf_minute` / `cn_stock_minute`（个股预设含涨跌停一字板 mask）
- **换数据文件、换分界或换算子集后，null 地形必须重新校准**（环境指纹绑定，自动判 mismatch）
- 同一数据面板拆多个 stateRoot 分头挖 = 规避试验计数，被环境指纹纪律明令禁止

## 测试

```bash
cd python && python -m pytest tests/ -q        # 757 项（含历次事故回归锁）
```

测试覆盖全部纪律设计的「计算→判据→拦截」三层；历次生产事故均有对应回归测试。
（注意须在 `python/` 目录下运行；仓库根直接跑会有路径性误报。）

## 局限（诚实边界）

- 随机生成器的算子集是**人工策划的先验**——这是「结构化空间的随机探索」，不是无偏搜索；实证上多样性的瓶颈在数据面（叶子字段）不在算子表
- null 地形 n=50 的 p95 估计误差约 ±10 个百分位（返回值自带提示；正式决策建议 n≥200）
- 推导优先层（提交顺序门/导航素材）2026-10 新上线，提交门当前为影子记账（只记录不拒收），按预设违规率规则自动转强制
- 多底模型混布（divergence）已备而未启用：同款模型的偏好重叠实测过高，等真实第二底模数据
- 仅供研究用途，不构成投资建议

## 许可证

[AGPL-3.0](./LICENSE)。选择 AGPL 的原因：本项目的核心价值是**研究诚信护栏**——
我们希望任何把它改成网络服务提供给他人使用的衍生品，同样必须开源其修改，
防止护栏被悄悄拆掉后以闭源形态重新兜售。
