# 规划书：tail 线全栈适配 — 视野 / 转向 / 漏斗 — 2026-08-27

> 动机（2026-08-27 取证）：tail 线目前只在**审判席**（submit 准入
> G1/G2/G3）就位，在导航系统的其余三处全部缺席：
>
> 1. **后视镜（视野）**：registry.get 返回全量 232KB（29 条 × ~8KB），
>    trail_summary 带全字段 last_engine_trail[-5:]（每条 ~2.4KB，其中
>    tail 块 0.9KB；trail_engine 全量 835 条 / 1.57MB）；TS 层
>    `JSON.stringify(null, 2)` 再膨胀 ~40%。DH 呈现层按字符截断
>    （「已截断，共 N 字符」）——registry 按时间序追加，**截掉的正是
>    最新条目**（拒收理由、tracks/dual_pass 标注在每条 entry 尾部）；
>    trail_summary 的 last_engine_trail 排在响应 dict 末位，同样最先
>    被截。agent 实际活在「看得见 loop 指令、看不见账本与历史」的
>    状态：重复提已烧方向、正交性审计盲审、学不到拒收理由。
> 2. **转向盘（导航）**：`_loop_directive` 的全部停点与策略机器
>    （`_family_convergence` / `_family_marginal` / streak / arc /
>    `_pick_strategy` R1-R8）**只读 `ic_ir`**，没有任何一处读 trail
>    条目的 tail 块。IC 平坦但 tail 有苗头的方向会被误判枯竭强制
>    换向；也没有任何机械信号把 agent 推向尾部维度。
> 3. **望远镜（漏斗）**：random_gen explore 模式的 `light_ic_scan`
>    只算 ic_ir，top_k 按 IC 排序——**尾部强、IC 平庸的树永远浮不上
>    来**。算子空间本身够用（ts_max/min、ts_skew/kurt、cs_rank），
>    缺的是选择标准。（null-calibration 模式 WS1 已适配，不动。）
>
> 技术路线钉死：**TS 零改动**。已核实 `factor_registry_get` 参数表固定
> （仅 envId 透传），加参数须走 TS 部署管线；本规划全部走「响应形状
> 变化 / Python 内部逻辑」，不碰 TS。

## 0. 决策点（待拍板，附建议）

| # | 决策点 | 建议 |
|---|---|---|
| D1 | registry.get 默认紧凑投影、**无 agent 侧全量入口**（全量 = 人读 registry.json） | 是；TS detail 参数列为后续可选增强（需 TS 管线，缓） |
| D2 | 族收敛双轨化口径：**两线都平才 must_rotate**（IC 滑窗 ∩ spread 滑窗），单线平只出 escalation | 是；这是停点语义从单指标改双指标 AND（更难触发），需明确拍板 |
| D3 | explore top_k 口径：**双列表**（top_k_ic + top_k_tail，可重叠，如实报告） | 是；复合分会隐藏权重选择 |
| D4 | spread 线「改善」的 delta 口径 | 绝对值与 IC 线同值（默认 0.05；bar≈0.43-0.51 语境下 ~10%），独立配置键 `fam_conv_delta_tail` 可调 |

## 1. 设计

### WS-A 紧凑视图（截断修复，纯响应形状）

1. **`_registry_get` 默认紧凑投影**：

   ```
   {"count": N, "accepted": n, "by_track": {"ic": .., "tail": .., "dual": ..},
    "entries": [
      {"name", "ts", "accepted", "admit_basis", "tracks",
       "ic_ir", "spread_ir",                     // 双轨主数字
       "reject_kind", "reject_reason[:120]"}     // 拒收条目：程序性/实质性 + 理由摘要
      ...每条目标 ≤ ~200B],
    "note": "紧凑视图（防呈现层截断）；投影字段覆盖正交性审计与避坑
             所需；完整诊断为引擎内部数据，人读 stateRoot/registry.json"}
   ```

   目标尺寸：29 条 × 200B ≈ 6KB（较 232KB 缩 ~40×），截断永不触发。
2. **trail_summary 条目投影 + 前移**：`last_engine_trail` 每条仅保留
   `{ts, source_hash[:12], horizon, stage, ic_ir, verdict,
   red_flags[:2], tail:{spread_ir, placebo_z}}`（≈150B/条）；并移到
   dict 前部（loop 之后）——即使未来再被截，最近历史最先存活。
3. `pool.stats()`、loop、termination 等既有字段不动（纯增量替换
   last_engine_trail 的形状）。

### WS-B loop 双轨化（转向盘）

1. **tail 遥测块**进 loop 响应（status / evaluate / batch /
   trail_summary 自动携带）：

   ```
   "tail": {"n_trials_tail": .., "n_eff": ..,
            "bar": {"value": .., "s0_source": ..},
            "near_misses": [{"hash_prefix", "spread_ir", "gap"}...最多3],
            "note": "尾部线接近门槛的方向（spread_ir 距 bar < 0.10）
                     ——若与当前族不同源，优先构造变体"}
   ```

   near_misses 从 `tail_ledger` 派生：spread_ir ∈ [bar−0.10, bar) 的
   条目按 ts 取最近 3 条（bar 用当前 N_eff/s0 现算，t_adj 全量重算
   不可行也不必要——legacy 公式近似即可）。给 agent「差一点」的
   方向感，替代现在的一片漆黑。
2. **族收敛双轨 AND**（D2）：`_family_convergence` 并行计算 spread_ir
   滑窗（同族、同去重口径、同 ≥2·Wf 门槛）：
   - IC 收敛 **且** spread 收敛 → must_rotate（现行语义）
   - 仅 IC 收敛、spread 仍在改善 → 不触发，escalation 文案改写为
     「IC 线收敛但尾部线仍在改善（+δ）——继续尾部维度或换向由
     策略分流」
   - spread 线参与判定的门槛与 IC 线相同（≥2·Wf 去重条目），
     弱波动不得成为赖着不换向的口子
3. **`_family_marginal` 双线汇报**：diag 增 `tail_marginal`（族内
   spread_ir 滑窗对族前史最佳）；escalation 文案双线（IC 边际 +
   尾部边际）。`_pick_strategy` R1/R2（literature/rotate）触发条件
   改为**两线都枯竭**，与收敛口径一致；单线枯竭只出 escalation。
4. **不新增 strategy type**（第一步）：尾部探索引导走 rotate 的
   directive 文案 + tail 遥测 near_misses；drive.ts 对 type 无关，
   零改动（已核实 drive.ts:150-164 只读 state + strategy 四字段）。

### WS-C explore 双轨扫描（漏斗）

1. `light_ic_scan` 追加 spread 计算：同一次 F，调用
   `spread_ir_statistic(F, fwd, pit, step, t_end=_train_end_of(env))`
   （与 null 校准 spread 段同口径）；landscape 在场时附 spread
   分位（对照 spread 段 p50/p90，廉价查表）。
2. explore 模式返回**双列表**（D3）：`top_k_ic`（现行）+
   `top_k_tail`（按 spread_ir 排序）；重叠如实报告。轻量诊断每树
   增 `{spread_ir, spread_pct}`。
3. 成本：spread_ir_statistic 向量化，校准环路已对 200 树 × 全
   horizon 菜单验证；explore n 默认 50 树，增量秒级。
4. null-calibration 模式不动（WS1 已含 spread 段）。

## 2. 生产行为变化

- registry.get / trail_summary 响应缩 ~20-40×：截断不再触发，agent
  重新看得见最新条目、拒收理由（含 reject_kind 程序性/实质性区分）、
  tracks 标注；
- loop 多 `tail` 遥测块；族收敛触发变严（双线 AND）——若 D2 拍板
  通过，当前族若 IC 平但 spread 在改善，will no longer 被 IC 单线
  强制换向；
- random_generate explore 多 `top_k_tail` 列表与 per-tree spread
  诊断；尾部强 IC 平的树首次可见。

## 3. 实现清单（文件级）

| 文件 | 变更 |
|---|---|
| `bridge.py` | `_registry_get` 紧凑投影；`_state_trail_summary` 条目投影 + 前移；`_loop_directive` tail 遥测；`_family_convergence` 双轨 AND；`_family_marginal` 双线；`_pick_strategy` R1/R2 条件 |
| `factor/tailgate.py` | `tail_near_misses(ledger, bar_value, window=0.10)` 派生 helper（纯函数） |
| `factor/random_gen.py` | `light_ic_scan` → 加 spread（保持函数名与返回兼容）；explore 返回组装双列表 |
| `state.py` | `MINING_CONFIG` 增 `fam_conv_delta_tail`（默认 = fam_conv_delta） |
| `python/tests/` | 新增 `test_registry_view.py` / `test_loop_tail_signals.py` / `test_random_gen_dual_scan.py`；适配 `test_loop_directive.py` 现有收敛/边际断言（双轨语义） |

## 4. 测试细目

1. **视图**：registry.get 紧凑形状（每条 ≤ 白名单字段、count/by_track
   正确、拒收条目含 reject_kind + reason 截断到 120）；空 registry
   → count=0 不炸；trail_summary 的 last_engine_trail 字段白名单
   （无 ic_series_sketch/suspects/construction_fp）且位于 loop 之后。
2. **遥测**：直写带 tail 块的 engine trail → loop.tail.n_trials_tail /
   n_eff 正确；构造 spread_ir 落在 [bar−0.10, bar) 的条目 →
   near_misses 命中且 ≤3 条。
3. **双轨收敛**：构造族 IC 滑窗平但 spread 改善 ≥ δ → 不触发
   must_rotate 且 escalation 文案含双线数字；两线都平 → 触发；
   spread 线条目 < 2·Wf → 只有 IC 线参与判定（回到现行行为）。
4. **explore**：双列表存在且各自排序正确；同树两指标一致；无
   landscape 时 spread_pct 缺省不炸。
5. 全量回归绿（重点：test_loop_directive 现有断言、test_tail_* 系列）。

## 5. 部署

pytest 全绿 → commit（不碰公共 repo）→ 重装 site-packages →
**重启 dsh web 由用户执行**（split-brain 教训：bridge 内存态持旧码）。
TS 零改动 = 无 TS 管线步骤。

## 6. 不做的事

- 不改 DH 呈现层（截断本身不动——让载荷小到不需要截）；
- 不给 TS 工具加 detail 参数（D1 缓；需要时单独立项走 TS 管线）；
- 不动 arc/cluster 断链谓词（仍 IC sketch / 构造血缘双信号——
  **不**引入 tail Jaccard 作第三断链信号，断链语义最小化）；
- 不动 null-calibration 模式；
- 不新增 strategy type（tail_explore 类指令待遥测跑一段后按需再议）。
