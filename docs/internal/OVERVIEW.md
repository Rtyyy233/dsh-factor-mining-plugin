---
name: dsh-factor-mining-overview
description: 因子挖掘 DSH 插件项目总览 — 架构、三处部署位置、问题解决史、用户动线
tags: [project, investment, dsh, plugin]
created: 2026-08-17
---

# dsh-factor-mining-plugin 总览

> 因子挖掘 harness 的产品化：把 [[deep-signal-mining-methodology]] 的挖掘纪律
> （三区隔离 + 因果检查 + 独立审计 + 诚实轨迹）封装成 DeepSeek Harness 插件，
> 便宜模型在 DSH 会话里通过结构化工具执行「假设 → 验证 → 归因 → 下一假设」循环。
> 代码开源、用户数据/因子库/挖掘状态全部留在用户侧（四平面解耦，见 [[DESIGN.md]]）。

## 1. 一句话现状

**已按官方规范挂载到用户 DSH web profile，端到端验证通过**：任意 web 会话可调用
25 个 `factor_*` 工具（24 常开 + arxiv 门控）→ JSON-RPC bridge → Python 因子引擎（含 worker 子进程隔离、
36 项与原 harness 的 parity 对账）。数据配置走 `factor_data_probe` → 原生
`ask_user_question` 引导，不进任何仓库。

## 1.1 批次2（2026-08-18 全架构审计修复）

5 分片并行审计发现 P0×3 / P1×20 / P2×15（完整清单 docs/AUDIT-2026-08-18.md），同日全部修复：
- **P0-1 registry diagnosis 三层贯通**——此前 DSH 端所有 factor_registry_submit 必败（工具接口无 diagnosis 参数，receipt/铁律流程不可达）
- **P0-2/3 calibration 数值域校验**——单边分界拒绝（防 auto 覆写用户显式值）、horizon>0、cost_bps>=0、ic_sample_every>=horizon（防重叠 IC 通胀 IC_IR）、日期格式、分钟 annualization 自动派生
- **数学修正**（数字与旧版不可比，null 地形需重跑）：DSR sr0 √(lnN)→√(2lnN)（旧版反保守）；block-bootstrap z 恒≈0 死检验改 mu/se；audit Spearman tie 公式对齐（旧 footrule 假 FAIL）；年化分母 horizon→sample_step
- **机制补漏**：causality 缓存 key 加环境指纹（防跨环境误命中）；吞错改显式 `_pool_error`/stderr；fp_cache 128 上限；receipts 4096；trail_engine 进 mining reset；reset 逐文件跟踪+备份毫秒防冲突；lockfile O_EXCL 原子化+PID 词边界；stateRoot 根目录防御；原子写全覆盖
- **client**：BridgeRpcError 保留 JSON-RPC code；ready 超时杀进程可重试；null 结果直通
- **e2e 补齐**：mining-loop 全链（evaluate→trail→summary→export→registry_submit(diagnosis)→reset），工具覆盖 8→16
- 验证：Python 64/64（新增 test_audit_fixes.py 10 项）+ e2e 6/6 + 构建 0 错 + 4 tarball 重部署

## 2. 架构与三处位置

```
┌─ 源码（开发/验证）──────────────────────────────────────────┐
│ life-os worktree: projects/dsh-factor-mining-plugin/        │
│   ├── python/     ← PyPI 包 dsh-factor-mining（引擎+bridge）│
│   ├── ts/         ← 4 个 TS 包（seam/provider/tool/bundle） │
│   ├── preset/     ← 专用挖掘会话 preset（未安装，见 §6）     │
│   └── DESIGN.md   ← 设计文档（决策与边界的单一真相源）       │
└─────────────────────────────────────────────────────────────┘
        │ pnpm pack（tarball 快照）
        ▼
┌─ 构建（monorepo 副本）──────────────────────────────────────┐
│ deepseek-harness: packages/factor-mining/{4包}/             │
│   tsdown 产出 lib/index.js + lib/invariant.js               │
│   （tsc 产物 lib/types/ 是类型层，tsdown 是打包层）          │
└─────────────────────────────────────────────────────────────┘
        │ dsh plugin add（官方安装命令）
        ▼
┌─ 用户安装（运行时）─────────────────────────────────────────┐
│ ~/.dsh/profiles/web/                                        │
│   ├── pkgs/*.tgz           ← 4 个官方格式 tarball（持久）   │
│   ├── pnpm-workspace.yaml  ← overrides 把 bundle 的 semver  │
│   │                           依赖钉到本地 tgz（防 404）    │
│   ├── package.json         ← bundles 列表含 factor-mining-  │
│   │                           bundle；dependencies file:pkgs│
│   └── node_modules/        ← pnpm 正常管理（非手动塞）      │
+ Python 引擎: pip install -e（全局 python -m                │
  dsh_factor_mining.bridge --probe 验证）                     │
└─────────────────────────────────────────────────────────────┘
```

**运行链路**（一次 `factor_evaluate` 调用）：
`agent → tools.execute(factor_evaluate) → defineTool 参数校验 →
FactorMiningPythonService → JsonRpcClient(stdio) → python bridge 进程 →
worker 子进程（用户 factor 代码隔离执行，超时杀进程树）→ 诊断 JSON 原路返回`

## 3. 问题与解决史

### P1 — Python 引擎（pytest 10/10 全绿）

| 问题 | 根因 | 解决 |
|---|---|---|
| 核心对账测试被静默漏跑 | bridge/parity 测试是脚本式 `main()`，pytest 收集 0 个 | 补 pytest 函数入口，`pytest tests/` 全量收集 |
| test 锁错误泄漏 | `evaluate_test` 的 RuntimeError 未包装 | bridge 捕获转 `BridgeError(PRECONDITION)` |
| 用户代码在 bridge 主进程执行 | worker.py 写好了但没接线（DESIGN §10 要求默认 worker） | `_run_worker`：env→npz→子进程→结构化结果回传，超时 taskkill /T |
| registry_submit 耦合 | 提交注册竟要 env+编译+评估 | 改为纯数据操作：收诊断对象校验落盘 |

### P2 — TS 三件套（vitest 4/4 + 全仓 tsc 零错误）

| 问题 | 根因 | 解决 |
|---|---|---|
| 测试无法访问 `.N`/`.verdict` 等属性 | Service 返回类型全是 `JsonValue` | 定义 16 个具体返回接口；tool 层 `json()` helper 做框架 JSON 边界 |
| `ctx.dispose()` 不存在 | cordis API 误用 | 保存 `ctx.plugin()` 返回的 fiber，`fiber.dispose()` |
| vitest 拒绝运行测试 | 仓库约定每包需要 `src/invariant.ts` companion | 4 包补齐（空 installer 模式，同 brand 纯工具包） |
| **deepseek-harness 从未成功构建过** | tsdown.config 依赖 typert 编译产物，全仓无 lib/index.js 先例 | 先构建 typert 依赖链，再逐包 tsdown——首次产出 4 包 lib |
| vite 找不到 tsconfig | worktree `ts/` 与 DH `packages/factor-mining` 是 junction，realpath 后相对路径断裂 | junction → 物理副本，双份手动同步 |
| tsdown `--filter` 报 No valid configuration | Git Bash 把 `/regex/` 展开成 `D:/Git/...` 路径；字符串 filter 是精确匹配 | 用完整包名 `@deepseek-ai/dsh-*` 精确匹配 |

### P3+ — 部署（教训最深的一段）

| 问题 | 根因 | 解决 |
|---|---|---|
| preset 列表不显示 | `!!js` 用了反引号模板串（非法 YAML），composition 解析失败 → broken | 改官方裸表达式写法 `!!js process.cwd() + '/...'` |
| preset 显示但点击无反应 | **DSH web UI 的 makeDefault 写入问题（未定位根因）**，settings.yaml 无任何写入痕迹 | 手写 `settings.yaml` 的 `agent-presets.default` 绕过验证了链路；正式方案改走 bundle 不依赖 preset |
| **DSH 启动卡死 5 分钟**（我的错） | 手动往 pnpm 管理的 node_modules 塞未发布包 | 完整回滚（5 处）后走官方通道 |
| **选工作区无反应**（我的错） | `pnpm add` 在 web/ 下新建了 node_modules/lock，干扰模块解析 | 回滚删除 web/node_modules + lock |
| bundle 安装 404 | bundle 的 semver 依赖（`^0.1.0`）触发 registry 查询，包未发布 | `pnpm-workspace.yaml` **overrides** 钉到 profile 内 `pkgs/` 本地 tarball（pnpm 官方机制） |
| file: 依赖指向易失目录 | 初次安装指向 job tmp 目录 | tarball 复制进 `~/.dsh/profiles/web/pkgs/`，dependencies repoint |

**核心教训（已沉淀为流程）**：对运行中系统的安装类操作，顺序必须是
**读官方规范 → 产出官方格式制品 → 方案过审 → 官方命令执行 → dump-config 验证**。
官方规范位置：`deepseek-harness/docs/user/develop/basic/publish.zh.md`。

## 4. 用户动线

### 动线 A — DSH web 会话内（主通道，已部署）

任意 web 会话（无需选 preset，bundle 是全局层）：

```
1. 状态自检        「运行 factor_status」→ ready:true + 环境 + bridge 版本
2. 首次数据配置    agent 调 factor_data_probe(路径) → 返回候选列映射 + questions[]
                   → 原生 ask_user_question 让你确认映射
                   → factor_config_write 写入会话 cwd 的 .factor-mining/
                   → factor_load_env 校验加载
3. 挖掘内循环      每轮: factor_query_paths + factor_query_library 查重
                   → 写 factor(env) Python 源码
                   → factor_check_causality（必须 causal）
                   → factor_evaluate(stage:development) 读完整诊断对象
                   → factor_record_trail / factor_record_explored / record_search_path
4. 批次与深检      一批挖完: factor_evaluate_batch（deflated_p 是最终显著性）
                   单因子深检: factor_walk_forward（时间有序分块）
5. 宣称有效前      factor_audit（独立两实现审计，对不上=有 bug 阻断）
6. 最终验证        factor_evaluate(stage:test) —— 一次性锁，消费后不可再用
7. 入库            factor_registry_submit（纯数据操作，收诊断对象落盘）
```

Python 引擎侧的纪律由代码死约束强制（不依赖模型自觉）：
三区时间隔离（development 自由 / selection 半消耗 / test 一次性锁）、
column-perm z≥3、不重叠 IC、worker 子进程隔离。

### 动线 B — Python 直用（无 DSH，开发/调试）

```python
from dsh_factor_mining.bridge import Bridge
bridge = Bridge(state_root="...", data_config_path="...")
bridge.dispatch("factor.evaluate", {...})   # 同一套 JSON-RPC 方法
```

或跑测试：`cd python && python -m pytest tests/`（10 个测试，
含 36 项与原 harness 的 [[parity]] 对账——引擎适配漂移会立即暴露）。

### 动线 C — 源码迭代（开发循环）

```
改 worktree ts/ 或 python/
  → ts: 复制到 DH packages/factor-mining/ → tsc -b → tsdown --filter '<完整包名>'
  → pnpm pack 4 包 → 覆盖 ~/.dsh/profiles/web/pkgs/*.tgz
  → cd ~/.dsh/profiles/web && corepack pnpm install
  → 重启 DSH；dump-config 验证层还在
  → python: pip install -e 已是可编辑安装，改完即生效
```

## 5. 验证状态矩阵

| 层 | 验证方式 | 状态 |
|---|---|---|
| Python 引擎 | pytest 10/10（adapters×5 + bridge smoke×4 + parity 36 项对账） | ✅ |
| TS 三件套 | vitest 4/4（集成 2 + tool 层端到端 2） | ✅ |
| 全仓类型 | tsc -b tsconfig.host.json 零错误 | ✅ |
| bundle 层挂载 | `dsh --profile web --dump-config` 显示 bundle 层两行插件 | ✅ |
| 运行时解析 | profile 内 require 3 包导出正确 | ✅ |
| Python bridge | `python -m dsh_factor_mining.bridge --probe` → ok | ✅ |
| 隐私门禁 | privacy_check.py → PASS | ✅ |
| **会话内 factor_status 实测** | 待用户重启 DSH 后确认 | ⏳ |

## 6. 已知问题与边界

1. **DSH web UI 点击 makeDefault 无反应**（DSH 侧问题，非本插件）——
   手写 settings.yaml 有效；bundle 通道绕开了对 preset 的依赖。
2. **preset 未安装**——SKILL.md 挖掘纪律文档随 preset 分发，preset 装
   回需等 UI 问题定位。当前 agent 拿到工具但没有 SKILL 的纪律指令；
   工具参数描述里已内嵌关键约束（stage 消耗性、test 一次性）。
3. **更新流程是手动的**（§4 动线 C）——改代码要 pack+覆盖+install 三步。
   未来发布 PyPI/npm 后消失。
4. **junction 已换物理副本**——worktree ts/ 与 DH packages/factor-mining/
   是两份，改 TS 必须手动同步（junction 会破坏 vite realpath）。

## 7. 回滚（官方通道，一次清干净）

```powershell
npx @deepseek-ai/dsh plugin --profile web remove @deepseek-ai/dsh-factor-mining-bundle
npx @deepseek-ai/dsh plugin --profile web remove @deepseek-ai/dsh-tool-factor-mining
npx @deepseek-ai/dsh plugin --profile web remove @deepseek-ai/dsh-factor-mining-python
npx @deepseek-ai/dsh plugin --profile web remove @deepseek-ai/dsh-factor-mining
# 然后删 pnpm-workspace.yaml 的 overrides 段 + pkgs/ 目录
# Python: pip uninstall dsh-factor-mining
```

## 关联

- 设计决策与边界：[[DESIGN.md]]（本目录）
- 挖掘纪律源头：[[deep-signal-mining-methodology]] / [[oos-is-consumable-needs-authorization]]
- 引擎对账基准：life-os `modules/investment/data/factor-mining/harness/`
