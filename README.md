# dsh-factor-mining-plugin

**让 LLM Agent 在引擎强制的研究纪律下自主挖掘截面因子。**

> 核心哲学：**Agent 的自由在「挖什么」，不在「怎么验证正确性」。**
> 一切依赖 Agent 自觉的纪律都会失效——所以前视检查、多重检验、test 区锁、
> 数字校验全部在引擎层强制执行，Agent 无法绕过，也无需自觉。

本项目是 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）平台的因子挖掘插件，
包含一个**可独立使用的 Python 研究纪律引擎**（评估/审计/防作弊校验）与 DSH 平台的
TS 工具层接入。不依赖 DSH 也能把 Python 引擎当作「带诚信护栏的因子评估库」直接使用。

## 它防什么

LLM 挖因子有一类系统性风险：**研究者的自欺被 Agent 加速**。本项目把量化研究里
最容易自欺的环节做成了引擎硬门：

| 防线 | 机制 |
|---|---|
| **前视偏差** | `check_causality` 噪声扰动检验，evaluate 前引擎强制（写 `shift(-1)` 直接拒评） |
| **OOS 泄漏** | 三区时间隔离（dev/selection/test）；test 区一次性锁（`test_lock`，不可被任何 reset 解除）；walk_forward 强制限制在 selection 区 |
| **多重检验虚高** | deflated Sharpe（Bailey-López de Prado 池缩放式）；试验数 n_trials 由引擎从评估历史自动统计；**p>0.05 是入册硬门** |
| **截面结构幻觉** | 列置换 z≥3 硬门（逐日截面 shuffle 构造 null） |
| **好得离谱** | red_flags 荒谬值守门（\|IC_IR\|>5、净年化>200% 等默认按 bug 排查） |
| **Agent 编造数字** | registry 入册校验 receipt（与引擎缓存复算逐位比对）；同因子换名/同名重复登记均被铁律拒绝 |
| **选择偏差盲区** | 随机因子 null 地形（经验分布基线），新因子 IC_IR 必须对照 p95/p99/max |
| **重复挖坟** | 双池记忆（active/falsified）自动对表：截面 corr≥0.9 或方法签名高相似即报 duplicate_suspect |

每条防线的「为什么存在」都有真实事故来源——开发过程中曾在真实 Agent 会话里
依次踩过：seed 重放整轮空转、指定工具从部署首日起就无法工作、校正数字算了但
不拦截、test 区经旁路泄漏给入册决策等。全部已修复并有回归测试锁定。

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
