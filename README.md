# dsh-factor-mining-plugin

**让 LLM Agent 在引擎强制的研究纪律下自主挖掘截面因子。**

本项目是 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）平台的因子挖掘插件，
包含一个**可独立使用的 Python 研究纪律引擎**（评估/审计/防作弊校验）与 DSH 平台的
TS 工具层接入。不依赖 DSH 也能把 Python 引擎当作「带诚信护栏的因子评估库」直接使用。

## 主要设计

主要的设计来自于作者在使用Agent进行量化研究时遇到的问题，并逐一使用纯代码的方式解决，以限制Agent犯错。
测试中使用qwen3.7 plus，所有设计是以防止小模型出现数据窥探、错误代码、无效搜索等问题进行的设计，模型的自由度仅在搜索信息、设计因子这一层

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
1.插件初始化时会生成null landscape来锚定当前数据集的特性
2.对产出的因子会做column-perm test，取|z|=3为阈值
3.多重检验校正：系统会计数Agent的搜索轮次N，依据搜索轮次提高p的阈值

### 重复探索

#### 因子库设计
因子库采用双库结构，原始库中使用贪心算法挑选Ic_IR最高、且截面相关性低的因子族构成强因子库；原始库中因子按相关性聚类，新因子与旧因子的相关性通过对不同簇的相关性计算实现，以避免因子数量爆炸带来的计算爆炸。

#### 探索轨迹
与通过算子进行穷举不同，项目将因子设计的自由度交给LLM，在每轮搜索中，LLM需要在trail中记录自己的探索方向、结果以及推理，并在下一次探索中排除已搜索过的方向；prompt中会建议LLM在多次设计失败后搜索arxiv中有时效性的论文作为可选项。
触发50轮次探索或挖掘出3个通过注册校验的因子时，自动终止。

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
│   evaluate.py    IC/IC_IR/列置换/DSR/衰减/topN/敏感性 数学核心  │
│   causality.py   噪声扰动前视检验（缓存：source+环境指纹）      │
│   audit.py       独立双实现审计（Spearman tie 对齐等）          │
│   random_gen.py  随机因子生成器（explore + null 地形校准）      │
│   ops.py         向量化算子库（ts_*/cs_*/二元/一元，因果安全）  │
│ discipline.py    指纹/receipt/结构签名/低效扫描/荒谬值判据      │
│ worker.py        用户因子代码的子进程隔离执行                   │
│ state.py         状态文件（trail/registry/pool，原子写）        │
│ data/adapters.py 数据适配（parquet/csv，long/wide，三区划分）   │
└──────────────────────────────────────────────────────────────┘
              ▲ JSON-RPC over stdio 子进程
┌─ TS 层（DSH 平台）───────────────────────────────────────────┐
│ ts/factor-mining/          Service Definition（类型契约）      │
│ ts/factor-mining-python/   Provider（驱动 Python 子进程）      │
│ ts/tool-factor-mining/     Consumer（factor_* 工具注册）       │
│ ts/bundle-factor-mining/   bundle（挂载到 DSH profile）        │
└──────────────────────────────────────────────────────────────┘
              ▲ 工具调用
        LLM Agent + preset/factor-mining/skill/factor-mining/SKILL.md
        （行为纪律：分层自主制——启动决策用户拍板，挖掘过程全自主）
```

运行时状态全部在用户 stateRoot（默认 `<cwd>/.factor-mining/`）：数据配置、
null 地形、registry、双层 trail（引擎硬事实 + Agent 叙事）、双池、test_lock。
**本仓库不含任何用户数据。**

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

# 2) 校准池子难度（随机因子的 IC_IR 经验分布基线）
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

# 4) 通过全部门控后入册（receipt 逐位校验数字）
b.dispatch("registry.submit", {"envId": "etf", "name": "mom20",
                               "source": "<同上>", "signal": "20日动量",
                               "diagnosis": diag})
```

因子写法契约（`def factor(env)`，只用过去数据，输出 `(T,N)` 数组）与全部工具的
方法清单见 `docs/FACTOR_GUIDE.md`，设计决策与理由见 `DESIGN.md`。

### B. 作为 DSH 插件使用

```bash
# 在 DSH profile 中安装 4 个包 + preset（按 DSH 插件机制）
dsh plugin add <bundle tarball>          # 或按 profile 的 bundles 机制挂载
cp -r preset/factor-mining/skill/factor-mining ~/.agents/skills/
```

重启 DSH 后对 Agent 说「我要做因子挖掘」即可。SKILL 会引导 Agent 走完整流程：
数据探测 → 三区确认（用户拍板）→ null 校准 → 挖掘起点确认 → 自主挖掘
（causality → evaluate → trail 留痕 → audit/walk_forward → 入册）。
test 区在最终验收时一次性消费（`stage="test"`），锁死后不可重置。

### C. 关键口径（换口径=换研究，报告中必须声明）

- `horizon`：前瞻收益窗口（默认 20 bar）；执行模型 t1（信号 T 收盘 → T+1 开盘入场 → T+H 收盘出场）
- `dev_end`/`sel_end`：三区分界（必须成对出现；不写则按数据 60/20/20 自动保底并标注 auto）
- `ic_sample_every`：IC 采样步长（必须 ≥ horizon，强制不重叠口径）
- `profile`：`cn_etf_daily` / `cn_stock_daily` / `cn_etf_minute` / `cn_stock_minute`（个股预设含涨跌停一字板 mask）
- **换数据文件或换分界后，null 地形必须重新校准**

## 测试

```bash
cd python && python -m pytest tests/ -q        # 86 项（含历次事故回归锁）
```

测试覆盖全部纪律设计的「计算→判据→拦截」三层；历次生产事故均有对应回归测试。

## 局限（诚实边界）

- 随机生成器的算子集是**人工策划的先验**——这是「结构化空间的随机探索」，不是无偏搜索
- null 地形 n=50 的 p95 估计误差约 ±10 个百分位（返回值自带提示；正式决策建议 n≥200）
- registry 入册时 receipt 校验失败仅降级 `verified=False`，不阻断（已知设计缺口，见 docs）
- 仅供研究用途，不构成投资建议

## 许可证

[AGPL-3.0](./LICENSE)。选择 AGPL 的原因：本项目的核心价值是**研究诚信护栏**——
我们希望任何把它改成网络服务提供给他人使用的衍生品，同样必须开源其修改，
防止护栏被悄悄拆掉后以闭源形态重新兜售。
