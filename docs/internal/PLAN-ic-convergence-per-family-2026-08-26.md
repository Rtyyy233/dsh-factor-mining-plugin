# 规划书：IC 收敛按族判定 — 2026-08-26

> 动机：全局收敛（`_ic_convergence`）的参考系是**全试验全局 max**，
> 被历史峰值族主导——换到真实但更弱的新族探索时，永远追不上前窗口
> 旧族峰值 → 误判全局枯竭 → 静默终态 + 注入器死机。生产实例：
> 最近 60 条最佳 0.364 vs 前窗口 0.836（dk/amihud 峰值时期打出的
> 全局最佳）。本需求把收敛判定搬进族内，让新方向重获机会。

## 0. 决策记录（用户 2026-08-26 已拍板）

| 决策点 | 结论 |
|---|---|
| 停点性质 | **纯 must_rotate**——族收敛永远只是换向信号，不设静默终态；全局枯竭不再自动判定，静默终态只剩 finalize / fail_streak |
| 判定口径 | **族内滑窗对滑窗**——本族最近 Wf 条最佳 vs 本族前一窗口最佳，改善 < δf → 收敛 |

## 1. 设计（钉死）

1. **族定义**：复用 `_family_chain`（尾部链；`_entries_linked` 对称
   双信号 OR：IC sketch 尾对齐 \|ρ\|≥0.6 / 构造血缘 Jaccard≥0.25；
   缺信号条目 = 单例族）——与 cluster_trials 计数、arc 断链归零
   **同一谓词同一事件**，不引入第二套族几何。
2. **判定**（新函数 `_family_convergence(engine_trail, mining)`）：
   - 族内序列：当前族链条目按时间序，`(source_hash, horizon)` 去重
     （保序取最新值——与全局版去重口径一致，防跨 stage 重复虚高窗口）
   - 门槛：去重后族内 ≥ 2·Wf 条才判（Wf 默认 20 → 族需 ≥40 条）；
     不足 → enough_data=False 不判（小族由 `_family_marginal` 软信号兜底）
   - 收敛 ⟺ `max(|IC_IR|[-Wf:]) − max(|IC_IR|[-2Wf:-Wf]) < δf`
   - 参数从 mining_state 读：`fam_conv_window`（≤0 = 关闭）/
     `fam_conv_delta`；旧键 ic_conv_window / ic_conv_delta 成为死键
     （读作不存在，不迁移）
3. **停点接线**（`_loop_directive`）：
   - 触发 → stop_kind 复用 **"direction_budget"**（`drive.ts` 只看
     `loop.state`，TS 零改动），stop_reason 标明「族内 IC 收敛
     （本族最近 Wf 条最佳 X 未超前窗口最佳 Y 达 δf）——必须换向」
   - state=must_rotate；obligation / 策略走既有 direction_budget 分支
     （`_pick_strategy` 强制 rotate，族边际 < −0.10 用 literature）
   - 解除：断链（新试验不接当前族链）→ 新族窗口从头累计 → 自动回
     running。同族参数微调不断链，门持续压着
4. **全局收敛退役**：`_ic_convergence` 删除；convergence stop_kind
   消失。drive.ts 注释「静默终态仅 convergence/finalize/fail_streak」
   留待下次 TS 变更时顺带更新（纯注释不改行为，不为此走 TS 部署管线）
5. **与 `_family_marginal` 分层**（不合并）：
   - marginal：软信号——本族最近小窗（W=min(10, len//2)）vs 族前史
     最佳，escalation 文案 + 策略 R1/R2，族 ≥5 条就响
   - 族收敛：硬停点——族内滑窗对滑窗，大窗（≥2·Wf）才判，触发
     must_rotate
   - 时序：marginal 先响（换向建议）→ 族收敛后响（强制换向）。
     两层参考系不同（族前史最佳 vs 族内滑窗），并存不合并
6. **遥测**：loop 响应加 `family_convergence` 诊断
   `{enough_data, family_size, window, recent_best, prev_best, delta}`
   （PASS-FAIL 教训：多维报告不坍缩）。

## 2. 生产行为变化（需知晓）

部署后生产 loop 从 may_stop(convergence) 恢复 **running**：当前族
22 条 < 40 门槛 → 族收敛不判 → 注入器恢复自动推进（当前族边际软信号
已在催 rotate：0.346 vs 0.364，边际 −0.018）。这是本需求的直接目的。

## 3. 实现清单

- `state.py`：MINING_CONFIG 删 ic_conv_window / ic_conv_delta，加
  `fam_conv_window=20` / `fam_conv_delta=0.05`
- `bridge.py`：`_family_convergence` 新增；`_loop_directive` 接线替换
  并删除 `_ic_convergence`；loop 返回加 family_convergence 遥测
- `tests/test_loop_directive.py`：4 个全局收敛测试改族内语义
  （触发 / 改善不触发 / 断链重置 / 去重），新增 must_rotate 状态与
  策略断言、enough_data 门槛、fam_conv_window=0 关闭
- TS：零改动

## 4. 生产回放校准（部署前必做）

生产 trail_engine（706 唯一试验）逐时间点回放：各族触发点表
（族长 / 触发时族内条数 / recent vs prev）；当前态判定（预期不触发）；
Wf/δf 敏感性（默认 20/0.05 vs 10/0.03、30/0.05）。历史触发过密
（如族 <15 条就触发）→ 上调窗口；过松 → 下调 δf。数字写入附录
后定稿部署。

## 5. 验收

- 单测全量（220 基线 ± 改造新增）
- 回放表入附录 B1
- pip 部署 + 生产 status 验证（state=running +
  family_convergence 遥测在场）→ 附录 B2

## 6. 不做

- 不做连环 K 族静默终态（用户已选纯 must_rotate）
- 不动 `_family_marginal`（软信号层保持原样）
- 不动 arc 50 / cluster 200 上限
- 不做 TS 侧改动（含 drive.ts 注释更新）
- 不做按族 deflation 计价（另一话题——计价仍全局全试验
  (hash, horizon)）

## 7. 风险

| 风险 | 缓解 |
|---|---|
| 生产从 may_stop 恢复 running，注入器自动推进恢复 | 用户已决策；escalation 软信号继续引导 rotate |
| 族 <40 条时门沉默 | `_family_marginal` 软信号兜底（族 ≥5 就响） |
| Wf/δf 默认未在本面板校准 | 回放步骤（§4）定稿后再部署 |
| stop_kind 复用 direction_budget 语义混淆 | reason 文案明确「族内 IC 收敛」+ loop 遥测带判定细节 |
| 同族反复触发（agent 忽略换向） | 与 direction_budget 同语义：门持续压着直到真断链 |

## 附录（2026-08-26 回填）

### B1 生产回放校准（711 条 trail，逐前缀判定）

**族规模分布（119 族，去重）**：两极——5 个大族（45/69/51/28+180/72，
早期 amihud/tsi/pw15 族，sketch 齐全）+ 大量 1-5 条碎片（缺 sketch 的
旧条目 = 单例族）；近期族（dk 等）20-27 条量级。≥40 条的族仅 5/119。

| 参数 | 历史触发段 | 判定质量 |
|---|---|---|
| Wf=20, δf=0.05（规划书初值） | **0 段** | 门形同虚设——近期族 20-27 条 < 40 门槛 |
| Wf=15, δf=0.04 | 2 段（第 146/415 条，族内 30 条，amihud 0.73 平台） | 高但覆盖窄 |
| **Wf=10, δf=0.05（定稿）** | **5 段**：第 136/404/438 条（amihud 族 0.72-0.78 平台×3）+ 第 709/711 条（dk 族 0.348 vs 0.364×2） | **全部真实平台**，无误触发 |
| Wf=10, δf=0.03/0.02 | 同 5 段（触发处改善本就 ≈0，δ 无需收紧） | 同上 |

**定稿：fam_conv_window=10、fam_conv_delta=0.05**（已入 MINING_CONFIG，
定稿理由见代码注释）。定稿逻辑：规划书 §4 的「过松」分支——不是 δf
松（触发处改善 ≈0），是 Wf 门槛在本面板族尺度下不可达；must_rotate
可解除（断链重置），误触发代价 = 一次换向，而 Wf=20 不触发的代价 =
族平台期无人强制换向（amihud 族 180 条就是这么磨出来的）。

### B2 部署验证（2026-08-26 13:53）

- pip 部署完成；生产 `_loop_directive` 实测：
  `state=must_rotate, stop_kind=direction_budget, strategy=rotate`，
  reason「族内 IC 收敛（本族最近 10 条最佳 |IC_IR| 0.355 未超前窗口
  最佳 0.354 达 0.05）」，family_convergence 遥测在场（族 27 条）。
- **与 §2 预测的差异**：预测「恢复 running」基于 Wf=20 初值（族 22<
  40 不判）；定稿 Wf=10 后 dk 族过门槛 → 当前态是 **must_rotate**
  （dk 族平台 → 强制换向 + 注入器挂 rotate 推进）。这更符合意图：
  静默等用户 → 强制换向继续挖。
- 部署时生产有活跃会话（trail 711→716 条正在评估）。本次改动不碰
  evaluate 计算路径（只动停点/loop 指令），运行中旧进程的 worker
  子进程加载新代码无行为差异；**旧 loop 语义（convergence 静默）
  仍在运行中进程的内存里——重启 dsh web 后 v8 完全生效**（用户执行）。
- commit：6dae8d1（规划书）→ 1af0754（实现）→ 参数定稿随附录提交。
  全量 225 passed。
