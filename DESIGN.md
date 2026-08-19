# DSH Factor Mining Plugin — 设计与开源数据隔离方案

> 状态：设计稿 v2（已纳入用户 2026-08 决策：参考原文件 / 工具拆分 / PyPI 自定 / 原生 ask-user / 适配形态）
> 约束：**不修改、不移动原 harness**（`modules/investment/data/factor-mining/harness`）。本目录是独立工程，原 harness 仅作为只读契约参考。

## 0. 已确认决策

| 决策 | 结论 |
|---|---|
| 插件深度 | 完整 dsh capability seam（Service Definition + Provider + Consumer） |
| 适配形态 | **插件包（必需）+ bundle（分发）+ preset（会话体验）**，不单独出 profile |
| Python 进程 | 常驻 JSON-RPC stdio 进程 |
| Python 发布 | 独立 PyPI 包 `deepseek-harness-factor-mining`（模块 `dsh_factor_mining`），TS Provider 做运行时探测，不在 npm 包内嵌 Python 源码 |
| 原 harness 算法 | 允许只读参考并适配重写；原文件不修改、不移动 |
| 数据配置交互 | 工具拆分：`factor_data_probe` + `factor_config_write`；问题经由 DSH 原生 `ask_user_question` 提给用户 |
| 挖掘循环 | Agent 自主运行（工具 + 技能指令，不引入 goal/workflow 强制状态机） |
| 验证位置 | 当前 worktree 的 `projects/dsh-factor-mining-plugin` |
| 开源边界 | 代码开源；用户数据、已有因子库、探索轨迹、注册结果全部留在用户侧 |

## 1. 目标与非目标

### 目标

1. 让 DeepSeek Harness 内的 Agent 通过结构化工具执行“假设 → 验证 → 归因 → 下一个假设”的因子挖掘循环。
2. 保留原 harness 的验证纪律：因果检查、三区时间隔离、不重叠 IC、column-perm、批次 deflate、walk-forward、独立审计。
3. 把“用户数据、已有因子库、探索状态”与“插件代码”彻底分层，使仓库可以安全开源。
4. 插件主动引导用户提供数据，并适配常见数据形态。

### 非目标

- 不改动原 harness 的任何文件。
- 不在仓库中附带任何真实行情数据、真实因子定义或挖掘结果。
- 不把 Python 算法重写成 TypeScript；Python 侧是唯一计算实现。
- 不内置任何“必胜因子”；插件只提供方法和纪律，不提供 alpha。

## 2. 目录布局（新工程）

```
projects/dsh-factor-mining-plugin/
├── DESIGN.md                       # 本文件
├── README.md                       # 面向用户的安装与数据配置说明（不含任何真实数据）
├── python/                         # 独立 PyPI 包 deepseek-harness-factor-mining（开源、无个人数据）
│   ├── pyproject.toml
│   ├── src/dsh_factor_mining/
│   │   ├── bridge.py               # JSON-RPC stdio 服务入口
│   │   ├── api.py                  # 服务方法：数据/因子/评估/审计/库/状态
│   │   ├── data/
│   │   │   ├── config.py           # 数据配置 schema 与校验
│   │   │   ├── probe.py            # 格式探测与建议映射
│   │   │   ├── adapters.py         # long/wide/per-symbol/minutes 归一化
│   │   │   ├── validation.py       # 数据健康报告
│   │   │   └── cache.py            # 归一化缓存（可选、可关闭）
│   │   ├── factor/
│   │   │   ├── env.py              # 通用 FactorEnv（(T,N) PIT 矩阵视图）
│   │   │   ├── causality.py        # check_causality
│   │   │   ├── evaluate.py         # IC_IR / column-perm / deflate / walk-forward
│   │   │   ├── audit.py            # 独立双实现审计
│   │   │   └── registry.py         # 注册表纯函数
│   │   ├── library/
│   │   │   ├── contract.py         # 用户因子库契约（python_module / json / 表达式）
│   │   │   └── template.py         # 空库模板（开源示例，不含任何真实因子）
│   │   ├── state.py                # trail/explored/search_paths/registry 持久化
│   │   ├── worker.py               # 执行用户 factor 代码的独立 worker 子进程
│   │   └── resources/
│   │       └── config.template.yml # 数据配置模板
│   └── tests/                      # pytest + 合成 fixture
├── ts/                             # DeepSeek Harness 插件包源码（最终迁入 packages/factor-mining）
│   ├── factor-mining/              # Service Definition
│   ├── factor-mining-python/       # Provider：管理常驻 JSON-RPC 进程 + Python 包探测
│   ├── tool-factor-mining/         # Consumer：模型可见工具
│   └── bundle-factor-mining/       # cordis.patch.yml bundle + 接入现有 profile 的 patch 片段
├── preset/
│   └── factor-mining/              # 专用挖掘会话 preset
│       ├── preset.yml              # name/description 展示元数据
│       ├── agent.cordis.yml        # 组合 provider/tool/skill 行
│       └── skill/
│           └── factor-mining/SKILL.md   # 开源版挖掘技能（脱敏后的方法论，不引用个人路径/因子）
└── tests/
    ├── fixtures/                   # 合成数据（确定性生成，绝无真实行情）
    └── privacy/                    # 开源隐私检查门禁
```

## 3. 运行拓扑

```text
DeepSeek Harness (Cordis)
  agent
    └── tool-factor-mining  ── ctx.factorMining 服务 ── factor-mining-python Provider
                                                          │
                                          NDJSON JSON-RPC 2.0 over stdio
                                                          │
                              python -m dsh_factor_mining.bridge
                                 ├── data adapters  ── 用户数据文件（只读）
                                 ├── worker subprocess ── 用户 factor 代码（隔离执行）
                                 ├── user library    ── 用户已有因子库（只读）
                                 └── state store     ── 用户状态目录（trail/registry/cache）
```

硬边界：

- **代码平面**：TS 包 + Python 包，可开源。
- **数据平面**：用户原始数据文件，插件只读，不复制进仓库。
- **库平面**：用户已有因子库，插件通过契约加载，不内置真实因子。
- **状态平面**：轨迹、证伪路径、注册表、缓存全部写在用户状态目录。

## 4. DeepSeek Harness 能力缝（三件套）

### 4.1 Service Definition：`@deepseek-ai/dsh-factor-mining`

`ctx.factorMining` 暴露：

```ts
interface FactorMiningService {
  status(): Promise<ServiceStatus>
  dataProbe(req: DataProbeRequest): Promise<DataProbeReport>
  dataConfigValidate(req: DataConfigRequest): Promise<DataConfigReport>
  dataConfigWrite(req: DataConfigRequest): Promise<DataConfigWriteResult>
  listEnvironments(): Promise<EnvironmentDescriptor[]>
  checkCausality(req: FactorSourceRequest, signal: AbortSignal): Promise<CausalityVerdict>
  evaluate(req: EvaluateRequest, signal: AbortSignal): Promise<FactorDiagnosis>
  evaluateComposite(req: CompositeRequest, signal: AbortSignal): Promise<CompositeDiagnosis>
  evaluateBatch(req: BatchRequest, signal: AbortSignal): Promise<BatchDiagnosis>
  walkForward(req: WalkForwardRequest, signal: AbortSignal): Promise<WalkForwardDiagnosis>
  audit(req: AuditRequest, signal: AbortSignal): Promise<AuditReport>
  queryLibrary(req: LibraryQueryRequest): Promise<LibraryHit[]>
  queryPaths(req: PathQueryRequest): Promise<PathHit[]>
  appendTrail(req: TrailEntry): Promise<AppendResult>
  appendExplored(req: ExploredEntry): Promise<AppendResult>
  appendSearchPath(req: SearchPathEntry): Promise<AppendResult>
  registryGet(req: RegistryGetRequest): Promise<RegistryEntry[]>
  registrySubmit(req: RegistrySubmitRequest): Promise<RegistrySubmitResult>
  arxivSearch?(req: ArxivSearchRequest): Promise<ArxivSearchResult>
}
```

### 4.2 Provider：`@deepseek-ai/dsh-factor-mining-python`

职责只有一个：把上面的接口翻译成 JSON-RPC 请求，并管理常驻 Python 进程的生命周期。

- **运行时探测**：首次请求前执行 `python -m dsh_factor_mining.bridge --probe`，确认 Python 包已安装；失败时返回 `PYTHON_PACKAGE_MISSING` 并给出安装命令（`python -m pip install deepseek-harness-factor-mining`），**不自动安装**。
- 懒启动：首次请求时 `spawn(pythonExecutable, ["-m", "dsh_factor_mining.bridge"])`。
- `ready` 握手后放行首个请求。
- 进程死亡：区分“干净退出”和“崩溃”；崩溃按指数退避重启，超过 `maxRestarts` 后返回领域错误并停止服务。
- 取消：`AbortSignal` → JSON-RPC `cancel` notification；worker 级取消由 Python 侧杀子进程完成。
- 并发：同一环境同时只允许一个评估；其余请求返回 `busy`（模型应排队）。
- 空闲回收：超过 `idleTimeoutMs` 可退出常驻进程（可配置，默认开启）。
- dispose：插件卸载时发送 `shutdown` 并等待进程退出。

Config（全部 cordis.yml 可配，Provider 校验）：

```ts
interface PythonProviderConfig {
  pythonExecutable: string        // 默认 "python"
  bridgeModule: string            // 默认 "dsh_factor_mining.bridge"
  bridgeCwd?: string
  stateRoot: string               // 用户状态目录，默认 ${DSH_CWD}/.factor-mining
  dataConfigPath?: string         // 用户数据配置；缺省时由 factor_data_probe/factor_config_write 引导创建
  startupTimeoutMs: number        // 默认 30_000
  requestTimeoutMs: number        // 默认 0 = 不设全局上限，评估类请求自带超时
  idleTimeoutMs: number           // 默认 600_000
  maxRestarts: number             // 默认 3
  maxConcurrentEvaluations: number// 默认 1
  executionMode: "worker" | "in_process"  // 默认 "worker"
  env?: Record<string, string>    // 透传环境变量（不含密钥）
}
```

### 4.3 Consumer：`@deepseek-ai/dsh-tool-factor-mining`

工具即模型的操作面。每个工具都返回结构化 JSON（`output.schema`），模型不得解析散文。

| 工具 | 主要参数 | 返回 |
|---|---|---|
| `factor_data_probe` | 文件路径 / format / layout 提示 / 可选字段别名 | DataProbeReport + 建议映射 + 歧义事实（`ambiguous` / `missing_required`，Agent 据此构造 ask_user_question） |
| `factor_config_write` | 显式 mapping（用户确认后的答案）+ library spec | 配置校验报告 + 写入结果 |
| `factor_config_validate` | 配置对象或路径 | 完整性与试加载报告 |
| `factor_load_env` | envId | 环境元信息 + 数据健康报告 |
| `factor_check_causality` | source, envId | causal / violation + 定位证据 |
| `factor_evaluate` | source, envId, stage | 完整诊断对象（IC_IR、beta、逐年、衰减、column-perm） |
| `factor_evaluate_composite` | source, ingredients[], envId | 剥洋葱 L0/L1 + synthesis_gain |
| `factor_evaluate_batch` | sources{}, envId | 批次 deflate 后的显著性 |
| `factor_walk_forward` | source, envId, folds | 分块 IC_IR + fold_consistency |
| `factor_audit` | claim（含 source/envId/stage） | 两套实现对账报告 |
| `factor_query_library` | query | 用户已有因子命中 |
| `factor_query_paths` | layer(explored/search_paths), query | 已试路径命中 |
| `factor_record_trail` | entry | 落盘结果 |
| `factor_record_explored` | entry | 证伪条目落盘 |
| `factor_record_search_path` | entry | 搜索路径落盘 |
| `factor_registry_get` | envId | 当前注册因子 |
| `factor_registry_submit` | candidate | 提交/校验结果 |
| `factor_status` | — | 进程/环境/状态目录/Python 包版本摘要 |

关键约束在工具层再次强制：

- `stage: test` 是消耗品：Python 侧 `test_lock.json` 一次性，TS Provider 不缓存其结果以外的任何东西。
- `factor_evaluate` 前必须已有 `factor_check_causality` 的 `causal` 结果；否则返回 `precondition_failed`。
- 评估类工具是前台调用；超过 `requestTimeoutMs` 返回超时并保留 worker 清理路径。后台化是后续可选扩展（`ctx.jobs`）。

### 4.4 适配形态结论：插件 + bundle + preset，不是二选一

DSH 的 preset 只是“把已有插件组合成一个会话”的配置层，它不能替代插件本体；因此正确的交付是三层叠加：

| 层 | 角色 | 本项目是否交付 |
|---|---|---|
| capability seam 插件包 | 真正实现 JSON-RPC Provider 和模型工具的执行代码 | 必需，核心交付 |
| bundle | 分发单位：一个依赖把 provider/tool/skill 行挂进任意 profile，仍可被上层 patch | 必需 |
| preset `factor-mining` | 专用挖掘会话：独立 persona + 只暴露挖掘相关工具 + 预加载 SKILL.md | 推荐交付 |
| profile | 顶层用户组合，强制替换会入侵用户环境 | 不交付；只给“如何把 bundle patch 进 web/headless”的片段 |

推荐用户用法：

1. 普通用法：把 `dsh-factor-mining-bundle` 加入现有 profile 的 bundles 列表；通用 agent 按需触发 skill。
2. 长时间无人值守挖掘：新建会话时选择 `factor-mining` preset；该 preset 的 `agent.cordis.yml` 组合 provider/tool/skill，工具集干净、指令稳定、多个 mining session 共享同一个常驻 Python 进程。

实现期需要确认的细节：provider 的挂载层（global standing mount vs preset 内 `isolate` realm）按 `dsh-agent-presets` / `dsh-scope` 规则落地，目标是非 mining 会话不承担 Python 进程成本。

### 4.5 原生 ask-user 集成（first-run 数据配置）

`factor_data_probe` 自身**不阻塞等待用户**，而是返回歧义事实（`ambiguous` 列候选 / `missing_required` 缺失项）；Agent 按 SKILL.md 用这些事实构造问题并调用 DSH 原生 `ask_user_question` 工具，把用户答案回填给 `factor_config_write`：

```text
factor_data_probe(path, hints)
  → { report, suggestedMapping, ambiguous: {...}, missing_required: [...] }
ask_user_question(agent 自 ambiguous/missing_required 构造的问题)  # DSH 原生工具；headless 无 provider 时由 Agent 在最终回复中提问
  → { answers }
factor_config_write(mapping = suggestedMapping + answers, librarySpec)
  → { configPath, validationReport }
```

规则：

- 探测阶段只读文件头，不做全量读取；建议映射是“候选”，未经用户确认不落盘。
- `factor_config_write` 只接受显式 mapping；缺列/歧义必须返回 `DATA` 错误，不静默选择。
- 插件不直接调用 `ctx.userQuestions.ask()`——由模型通过原生工具提问，保持 loop 与日志语义不变。
- 数据路径、库路径写入的配置文件属于用户，不进仓库。

## 5. JSON-RPC 协议（bridge 契约）

- 传输：stdio，每行一个 JSON-RPC 2.0 消息（请求/响应/通知）。
- 服务端启动后发 `ready` notification（携带 `schemaVersion`、`environments`、`stateRoot`）。
- 请求必须带 `id`；取消用 `cancel {id}` notification。
- 错误分三类：
  - `PRECONDITION`：因果未过、test 已消费、环境未配置等——模型可自行修复。
  - `DATA`：映射缺失、字段错误、样本不足——需要用户/配置介入。
  - `INFRASTRUCTURE`：进程死亡、超时、资源不足——工具层呈现，禁止模型重试轰炸。
- 方法分组：
  - `config.*`：读/写/校验数据配置与库配置
  - `data.*`：probe、load、health
  - `factor.*`：check_causality、evaluate、evaluate_composite、evaluate_batch、walk_forward
  - `audit.*`
  - `library.*`、`paths.*`、`registry.*`、`state.*`
  - `arxiv.*`（可选）
- 所有返回都是 JSON-serializable；**绝不返回 OHLCV 原始行**，只返回统计与诊断。

## 6. 用户数据适配（开源核心设计）

### 6.1 分层原则

| 平面 | 归属 | 插件行为 | 是否可开源 |
|---|---|---|---|
| 插件代码 | 项目 | 读写自身 | 是 |
| 用户行情数据 | 用户 | 只读 + 可选归一化缓存到用户状态目录 | 否 |
| 用户已有因子库 | 用户 | 通过契约加载，只读 | 否 |
| 用户挖掘状态 | 用户 | 写用户状态目录 | 否 |
| 合成测试数据 | 项目 | 测试 fixtures 专用 | 是 |

仓库内禁止出现：真实 parquet/csv/pkl、真实因子定义、绝对个人路径、真实结果快照。

### 6.2 数据配置 schema（用户提供，插件不猜测）

```yaml
version: 1
state:
  root: /path/to/user-state          # 状态、缓存、注册表；默认 <cwd>/.factor-mining
data:
  environments:
    primary:
      label: "主挖掘池"
      kind: panel                    # panel | minute_features
      source:
        type: parquet                # parquet | csv | glob
        path: /user/data/panel.parquet
        options: {}
      layout: long                   # long | wide | per_symbol | multiindex
      mapping:
        symbol: symbol
        date: eob                    # 可为 null：表示日期来自 index
        open: open
        high: high
        low: low
        close: close
        volume: volume
        amount: amount               # 可选
        listed: null                 # 可选：上市日/可交易起始
        extra: {}
      time:
        dateFormat: auto             # auto | 显式 strftime
        frequency: auto              # auto | daily | minute
      constraints:
        minSymbols: 20
        minDates: 200
        requireFiniteOhlcv: "report"   # true(strict) | false(off) | "report"(默认: 缺口放行+摘要上报)
        allowZeroVolume: true
    crossval:
      # 第二个环境的完整配置（可选）
    minuteFeatures:
      kind: minute_features
      source: {...}
      layout: long
      mapping:
        symbol: symbol
        date: eob
        features:
          amihud_illiq: amihud_illiq
          realized_vol: rv20
          # 用户自己声明哪些列是分钟特征
```

### 6.3 支持的数据形态

| 形态 | 说明 | 归一化方式 |
|---|---|---|
| `long` | 一行 = symbol×date，列含 OHLCV | 直接 pivot 到 (T,N) |
| `wide` | 一行 = date，列编码 `{symbol}_{field}` 或 MultiIndex `(symbol, field)` | 按映射 pattern 解析列名 |
| `per_symbol` | glob 多文件，文件名含 symbol | 逐文件读取后 concat |
| `multiindex` | parquet 列是 MultiIndex(symbol, field) | 直接索引重组 |
| `minute_features` | 已计算特征的长表 | 构建特征矩阵 + 对齐主环境时间轴 |
| 文件格式 | parquet / csv（可 glob） | pandas/pyarrow 读取，统一 schema |
| 可选字段 | `amount`、`listed`、`extra` | 缺省降级并在 health report 中说明 |

明确**不自动猜测**用户列名语义；`factor_data_probe` 只做“探测 + 候选建议”，最终映射必须由用户确认或显式传入。自动猜测只在提示中列出置信度，不落盘。

### 6.4 数据配置的建立流程（first run）

1. Agent 调用 `factor_data_probe`，用户给出一个或多个文件路径与格式提示。
2. 插件读取文件头（限制行数，不读全量）并生成 `DataProbeReport`：
   - 文件格式、行列数、列名、dtype、日期范围、symbol 数；
   - 每个必需字段的候选列及置信度；
   - 明确的缺失项和不可恢复问题；
   - `ambiguous` / `missing_required`：需要用户拍板的歧义事实（字段映射二义性、日期格式等），Agent 据此构造选项式问题。
3. Agent 调用 DSH 原生 `ask_user_question(questions)` 获取用户答案；headless 无 provider 时在最终回复中提问。
4. Agent 把 `suggestedMapping + answers` 交给 `factor_config_write`，插件校验后写入 `<stateRoot>/factor-mining.config.yml`（该文件属于用户，插件仓库永不包含）。
5. `factor_load_env` 用完整配置做健康校验，失败则返回精确错误，不静默降级。

### 6.5 归一化管道

```text
原始文件(只读)
  → 采样探测(限制行数)
  → 完整读取 + 列映射校验
  → 时间解析/排序/去重
  → symbol 白名单 + 可交易性起始日(listed)
  → pivot 为 (T,N) 矩阵 + mask
  → FactorEnv（PIT 视图）
  → 可选：写入 stateRoot/cache/<sha256>.parquet
```

- 原始数据永不修改。
- 归一化缓存只在用户 stateRoot 内；缓存键 = 配置 hash + 源文件 mtime/size；可配置 `cache: false`。
- 任何一步失败都返回带上下文的 `DATA` 错误，绝不吞错。

### 6.6 适配不同用户数据形式的验证矩阵

测试必须覆盖（全部用合成 fixture）：

- long parquet 完整字段；
- long parquet 最小字段（无 amount/listed）；
- long csv + 自定义日期格式；
- wide 单层列（`{symbol}_close`）；
- wide MultiIndex 列；
- per_symbol 多文件；
- minute_features 长表 + 与主环境日期对齐；
- 字段别名（如 `open_price`/`OPEN`）；
- 坏数据：缺列、重复行、日期乱序、NaN 超阈值、high<low。

## 7. 已有因子库解耦（开源核心设计之二）

插件不内置用户的 16 个因子，也不内置任何真实因子注册表。用户因子库通过三种契约之一提供：

### 7.1 python_module

用户提供一个可导入模块路径，实现：

```python
def list_known_factors() -> list[dict]:
    # [{name, description, formula, ic_ir, ...}]
    ...

def get_known_factor(name) -> callable:
    # 返回 factor(env) -> np.ndarray
    ...

def query_known_factors(query: str) -> list[dict]:
    ...
```

### 7.2 json_registry

用户提供一个 JSON 文件，条目含 `name/formula/description`；由通用表达式引擎求值（仅支持内置算子子集）。

### 7.3 expression_list

纯表达式清单（`neg(returns(h,40))` 这类），用于轻量场景。

### 规则

- 未配置库 = 合法状态：`factor_query_library` 返回空 + `library_not_configured`，Agent 可以挖“全新维度”。
- 库路径、模块名属于用户配置，不进仓库、不进默认值。
- 开源仓库只提供 `library/template.py`（空实现 + 文档字符串），作为用户复制起点。
- 库函数在 worker 子进程中执行，与用户 factor 代码同等对待。

## 8. 状态解耦（开源核心设计之三）

`trail.json`、`explored_paths.json`、`search_paths.json`、`registry.json`、`test_lock.json`、归一化缓存：

- 全部写入 `stateRoot`，不是包目录、不是 harness 目录。
- 默认 `stateRoot = <DSH_CWD>/.factor-mining`；可被 cordis.yml 覆盖。
- 插件发布包 `files` 只含 `src/`、`skill/`、README/LICENSE；不包含任何 state 文件。
- Python 包 `pyproject` 不打包任何数据文件。
- `registry_submit` 只写用户 stateRoot 的 registry.json，不自动提交到仓库。

## 9. 开源隐私门禁（发布前必须通过）

1. **路径扫描**：src/docs/tests 中不得出现绝对用户路径、机器名或任何本地仓库路径。
2. **数据扫描**：发布包内不得出现 `.parquet/.pkl/.csv/.npz` 真实数据；仅允许 `tests/fixtures/` 下确定性生成的合成文件（测试运行时生成，或声明式小样本）。
3. **因子扫描**：不得出现任何用户已有因子的名称、公式或结果数值；快照只用通用基线因子（如 20 日收益）的合成结果。
4. **快照**：DSH 快照测试只用合成 fixture 和 mock bridge；不允许真实 API/数据参与 keyless snapshot。
5. **默认值**：所有默认路径都是相对用户目录或显式空值；缺失数据配置必须报 `DATA_CONFIG_REQUIRED`，不得有“开发者机器 fallback”。
6. **README 声明**：插件是方法框架，不含投资建议；用户数据与因子库版权归用户。
7. **日志面**：工具输出只含诊断/统计；如果未来支持展示原始行，必须是显式 opt-in 且标记 `sensitive`。

## 10. 执行安全（用户 factor 代码是任意 Python）

- 默认 `executionMode: worker`：bridge 主进程只做协议/数据/状态，不执行用户代码。
- 每次 `check_causality` / `evaluate` / `audit` 在独立 worker 子进程执行：
  - 数据从归一化缓存加载，或从主进程通过临时 memmap/parquet 传递；
  - 超时杀进程（进程树级）；
  - 可用内存/CPU 上限在后续版本接 OS 资源限制（原型期先做超时 + 进程隔离）。
- 用户数据目录对 worker **只读**；worker 只写 `stateRoot/candidates/<runId>/`。
- 所有 run 保留 source/参数/diagnosis 到 trail，保证可复现、可审计。
- `in_process` 模式仅用于受信本地调试，文档标注风险。

## 11. 技能、bundle 与 preset

- `preset/factor-mining/skill/factor-mining/SKILL.md`：由原 HARNESS.md 的方法论**重写**而来，不引用任何个人路径、个人因子、个人结果；保留“新维度优先、失败归因、下一假设、三区 OOS、column-perm、deflate、walk-forward、诚实轨迹、如何用 factor_data_probe + ask_user_question 完成 first-run”。
- bundle `@deepseek-ai/dsh-factor-mining-bundle`：
  - `cordis.patch.yml` 挂载 provider + tool + skill 三行；
  - 提供 profile 片段示例，用户可 patch 进 `web` / `headless`。
- preset `factor-mining`：
  - `agent.cordis.yml` 组合 provider/tool/skill，面向专用挖掘会话；
  - 专用 persona（system prompt section）把 Agent 约束为“因子挖掘 Agent”，降低通用闲聊/工具噪音；
  - 不写进默认 base bundle——它是可选能力，按需挂载。

## 12. 工作区验证计划（本 worktree 内，不动原 harness）

- **P0 数据/隐私设计评审**：本文件评审通过后再写代码。
- **P1 Python 包**：新目录内参考原 harness 契约实现 data adapters、FactorEnv、causality、evaluate、audit、library contract、state、bridge；全部用合成 fixture 测试。
- **P2 TS 包**：Service Definition / Provider / tool 三件套；Provider 单测用 fake JSON-RPC server，不依赖真实 Python。
- **P3 集成**：bundle + preset + skill + headless 快照，用合成数据跑一个通用基线因子的完整循环（含 `ask_user_question` 的 headless 降级路径）。
- **P4 私有验证**：仅在本机，用用户自己的数据配置指向真实数据，跑通后不提交任何配置/结果。
- **P5 开源准备**：跑隐私门禁、去掉 worktree 痕迹、迁移到正式仓库目录、补 README/许可、发布 Python 包。

## 13. 待确认问题

1. preset 是作为正式交付物随插件发布，还是先只在 worktree 验证、正式版只发 bundle + patch 片段？
2. `factor_data_probe` 的歧义事实在 headless（无 user-questions provider）时，SKILL 允许 Agent 把问题写进最终回复并等待用户下一条消息回答（已实现）。
3. Python 包名用 `deepseek-harness-factor-mining`，还是更中性的 `dsh-factor-mining`？如果最终不进 deepseek-harness 官方仓库，是否需要避免 `deepseek-ai` scope 和官方命名？
4. 原 harness 的评估算法是否需要在 P1 做输出对账（同一合成数据上与原 harness 结果一致）作为参考实现的验收标准？
