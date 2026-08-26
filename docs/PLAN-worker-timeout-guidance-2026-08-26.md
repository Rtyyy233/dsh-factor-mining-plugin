# 规划书：worker 超时的可行动引导 — 2026-08-26

> 动机（生产实证）：agent 手写 rank autocorrelation 一致性因子，
> per-ETF Python 循环 → 单次 `factor(env)` 计算 > 300s → worker 子进程
> 被杀 → 只收到一句 `worker 超时（>300s），已终止` → agent 误判
> 「bridge 崩了需要等恢复」，随即**放弃研究方法**换「更简单的方法」。
> 正确行为是向量化实现（groupby/rolling），研究方向不变。
>
> 结论：超时本身是正常的基础设施事件（事务语义已正确——零写入、
> 不烧名、可重试），坏在**引擎没有把这三件事告诉 agent**，也没有
> 在更早的时刻暴露慢因子的结构性天花板。本方案补三块：可行动的
> 错误信息、evaluate 阶段的性能预警、loop 指令的超时引导。

## 0. 三个缺口（均已核实到代码位置）

| # | 缺口 | 位置 |
|---|---|---|
| 1 | 超时错误信息不含行动指引（bridge 是否存活 / 是否零写入 / 根因 / 修法全都没有） | `bridge.py` `_run_worker` TimeoutExpired 分支 |
| 2 | 引擎知道 submit 的结构性上限但从不早说：噪声门至少重跑因子 10 个世界（`noise.py` 预算判断下限 + 预算 240s、worker 墙 300s），**单次计算 > ~24s 的因子 submit 必然事务中止**；而 evaluate 对 `F = fn(env)` 完全没有计时 | `worker.py` factor.evaluate 分支；`noise.py` noise_test |
| 3 | 超时以异常直通 `dispatch`，不进任何台账 → loop 指令（引擎对 agent 的引导声道）对「超时后弃方向」行为完全失明 | `bridge.py` dispatch |

## 1. 设计（钉死）

### W1 超时错误重写

`_run_worker` 的 TimeoutExpired 分支，新消息要素（method 名在作用域内）：

1. **事实澄清**：`{method}` 的 worker 子进程已终止并清理；bridge 本体
   未受影响，无需等待恢复，可立即重试；
2. **事务性**：本次调用零写入（registry / trail 均未动，不烧名）；
3. **诊断**：最可能根因 = `factor(env)` 单次计算超 300s，典型是
   per-symbol Python 循环；修法 = 向量化（`df.groupby("symbol")`
   的 shift/rolling、`unstack` 到宽表做矩阵运算）；
4. **行为纪律**：这是基础设施事件，**不是对因子/研究方向的判定**
   ——修实现，不换假设。

同分支的「worker 无结果输出」错误补一句 bridge 正常、零写入、可重试。

### W2 evaluate 单次计时 + submit 可行性预警

- `noise.py` 顶部提取常量 `NOISE_MIN_WORLDS = 10`、
  `NOISE_BUDGET_SECS = 240.0`（预算判断处改引常量，行为不变）；
- `worker.py` `factor.evaluate` 分支用 monotonic 计时包住
  `F = fn(env)`，结果附 `perf` 字段：

  ```
  perf = {
    "factor_runtime_s": t,
    "submit_noise_gate_estimate_s": NOISE_MIN_WORLDS * t,
    "verdict": "blocked" | "warn" | "ok",
    "note": "..."
  }
  ```

  - `10·t > 240` → `blocked`：「submit 噪声门至少重跑 10 个世界
    ≈ {10t}s > 预算 240s，submit 必然事务中止——先向量化」；
  - `10·t > 120` → `warn`；
  - 其余 `ok`。
- worker 返回字段经 `_wrap_diagnosis` 自然并入诊断对象——agent
  **第一次 evaluate 就看到天花板**，不必等到 submit 烧几分钟；
- 只做 `factor.evaluate`（agent 手写源的路径）；batch / walk_forward
  不动（引擎自生成源，快）。

### W3 infra 失败台账 + loop 注入

- `dispatch` 捕获 `BridgeError(code == -32005)`：append
  `stateRoot/tool_failures.json`（有界 100 条、原子写、字段
  ts/method），随后**原样 re-raise**——异常语义零变化；
- `_loop_directive` 读最近 30 分钟内的 -32005 条目，若 ≥1 在 loop
  dict 附加：

  ```
  "infra_failures": {
    "recent_count": k,
    "last_method": "...",
    "note": "worker 超时是基础设施事件：向量化实现，勿因超时更换研究方向；
             失败的调用零写入、可立即重试"
  }
  ```

- 纯附加遥测：不触碰 state 机（running / must_rotate / may_stop
  判定逻辑零改动）、不参与 strategy 选择。agent 超时后的下一个
  成功调用（status / evaluate / trail_summary）即收到纠偏引导。

## 2. 测试（W4）

新建 `python/tests/test_timeout_guidance.py`：

1. monkeypatch `subprocess.run` 抛 TimeoutExpired → evaluate 收
   -32005，断言消息含：方法名、「立即可重试」「零写入」「向量化」
   「修实现」关键词；
2. 慢因子源（source 内 `time.sleep`）+ monkeypatch 阈值常量 →
   断言 diagnosis.perf 的 verdict 与估算秒数；
3. 直写 tool_failures.json → status 的 loop.infra_failures 存在且
   note 含「勿因超时更换研究方向」；无失败文件时 loop 无该键；
4. 台账有界性（>100 条截断）。

全量回归：现有测试全部保持绿（loop 附加键不破坏既有断言——
现有断言均为字段存在性/取值，新增键不冲突）。

## 3. 部署（W5）

1. `python -m pytest python/tests/` 全绿；
2. commit（不碰 docs/internal）；
3. 重装到运行环境（site-packages），重启宿主进程（bridge 内存态
   持旧码，重启才收敛——split-brain 教训）；
4. 生产验证：让 agent 重跑原 rank autocorrelation 源 → 预期收到
   新超时消息，或 evaluate 阶段 perf 预警先行。

## 4. 不做的事

- **不降噪声门 10 世界下限**——统计下限是原则；天花板用预警告知，
  不是放松门来迁就慢实现；
- 不给 batch / walk_forward 加计时（范围控制）；
- 不改 harness 侧 agent prompt / skill——引擎侧消息是持久修复，
  任何模型都会读错误信息。

## 5. 验收标准

- [ ] 超时错误一条消息内含四要素（存活/零写入/根因修法/纪律）；
- [ ] 单次计算 >24s 的因子在 evaluate 即得 `perf.verdict=blocked`；
- [ ] 超时后 agent 的下一个引擎响应携带 loop.infra_failures 引导；
- [ ] 新增 4 测试 + 全量回归绿；
- [ ] state 机与 strategy 选择行为逐字节不变（纯附加）。
