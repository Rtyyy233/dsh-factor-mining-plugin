/**
 * Model-facing Consumer of the `ctx.factorMining` capability seam.  Tool
 * results stay lossless JSON (`type: 'json'`) because the Python engine owns
 * the diagnostic vocabulary; presentation renders compact JSON text.
 * @module @deepseek-ai/dsh-tool-factor-mining
 */

import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { defineTool, type JsonValue } from '@deepseek-ai/dsh-tools'
import type { FactorMiningService, JsonRecord, OperatorsRequest, StateResetRequest } from '@deepseek-ai/dsh-factor-mining'
import type {} from '@deepseek-ai/dsh-factor-mining'
import { applyDrive, driveTrace, fileDeadlineApi, type DriveHandle } from './drive.ts'
import { bindSessionRoot, journalLaneOfConfig, laneOfExec, rootOfExec } from './lane.ts'

export const name = 'tool-factor-mining'
export const inject = ['tools', 'factorMining']

export interface Config {
  enableArxivSearch?: boolean
  /** Turn-boundary auto-drive injector (2026-08-24): on a completed turn of a
   * factor_* session, inject the engine's strategy directive as a user-role
   * followup after a quiet window. */
  enableDrive?: boolean
  /** Quiet window between turn end and injection (ms, default 60s). */
  driveDelayMs?: number
  /** Diagnostic cap for the consecutive-`continue` injection counter
   *  (default 5). Since 0.1.22 the counter no longer stops injection
   *  (perpetual mining — the anti-repeat guardrail lives engine-side); it
   *  is kept for logs/status only. */
  driveMaxConsecutiveSimple?: number
  /** Mining deadline, ISO 8601 (方案 B, 2026-09-17). While set, the deadline
   *  replaces the consecutive-simple cap: continuous mining until the
   *  wall-clock, then one wrap-up directive is injected and auto-drive
   *  disarms. Configure per-run via a `--patch` overlay (ephemeral), e.g.
   *  `driveDeadline: "2026-09-18T08:00"`. */
  driveDeadline?: string
  /** Fallback sweep interval in ms (default 0 = off): periodically re-run
   *  the injection check so a stalled session (no turn/end events) is
   *  re-ignited. Recommended alongside driveDeadline, e.g. 300_000. */
  driveWakeIntervalMs?: number
  /** Re-materialize persisted-but-unopened sessions before injecting
   *  (default ON, 2026-10-02): the desktop host has no live agent for a
   *  session that is not open in the UI, so every injection died at the
   *  first gate. With this on, the drive asks the host's
   *  sessionController to resume the session (same path the UI takes)
   *  and then injects normally. */
  driveAutoResume?: boolean
  /** Stable journal line id (e.g. "main") stamped onto journal calls and
   *  evaluations — cross-session belief inheritance for DSH sessions whose
   *  per-session lane is a UUID. One journal line per stateRoot. */
  journalLane?: string
}

export const Config: z<Config> = z.object({
  enableArxivSearch: z.boolean().default(false),
  enableDrive: z.boolean().default(true),
  driveDelayMs: z.number().default(60_000),
  driveMaxConsecutiveSimple: z.number().default(5),
  driveDeadline: z.string(),
  driveWakeIntervalMs: z.number(),
  driveAutoResume: z.boolean().default(true),
})

const JSON_RENDER = (_args: unknown, value: unknown) => [{
  type: 'text' as const,
  text: JSON.stringify(value, null, 2),
}]

/**
 * Tool results are lossless JSON: the Python engine owns the diagnostic
 * vocabulary, so the typed service return is flattened to the framework's
 * JsonValue at this boundary (never `undefined`-bearing optional fields).
 */
const json = <T>(promise: Promise<T>): Promise<JsonValue> => promise as Promise<JsonValue>

/**
 * LLM-side json params arrive in two shapes: a JSON object or a JSON string.
 * Normalize at this boundary — a bare cast lets string payloads crash deep
 * inside the Python bridge (`'str' object has no attribute 'get'`).
 */
function jsonRecord(value: unknown, field: string): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value)
      if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>
      }
      throw new Error('parsed value is not a JSON object')
    } catch (e) {
      throw new Error(`${field} 是 JSON 字符串但解析失败: ${String(e)}`)
    }
  }
  if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
    return value as Record<string, unknown>
  }
  throw new Error(`${field} 必须是 JSON 对象（收到 ${typeof value}）`)
}

export function apply(ctx: Context, config: Config): void {
  try { driveTrace('apply-entry enableDrive=' + String(config.enableDrive)) } catch {}
  const service = ctx.factorMining as FactorMiningService
  // Dual-line routing (2026-09-17): one session, one ledger. A session that
  // called factor_root_use sticks to that root's bridge.
  // 首调门（2026-09-19 用户拍板，0.1.16）：有会话身份但未声明账本的调用
  // 不再静默路由默认 root——返回一次性声明提示，错线路径在机械上不存在
  // （此前新会话首调会被隐式绑到默认个股账本：ETF 会话污染个股试验计数
  // + 收到个股引擎指令 + 错误绑定落盘）。无会话身份（工具/回放上下文）
  // 维持旧回退（单线用法正确）。factor_root_use 自身先 bind 后 svcFor，
  // 天然过门。
  const GATE_MSG = ('[账本未声明] 本会话尚未绑定因子账本，本次调用未执行。'
    + '个股线：factor_root_use {root:"default"}；ETF 线：factor_root_use '
    + '{root:"etf"}。声明后重试本调用（一次性声明，重启持久）。')
  const gatedService = (): FactorMiningService =>
    new Proxy({} as FactorMiningService, {
      get: () => () => Promise.reject(new Error(GATE_MSG)),
    })
  const svcFor = (exec: unknown): FactorMiningService => {
    const key = rootOfExec(exec)
    if (key === undefined) {
      const id = (exec as { agent?: { id?: unknown } } | undefined)?.agent?.id
      if (typeof id === 'string' && id.length > 0 && service.forRoot !== undefined) {
        return gatedService()
      }
      return service
    }
    if (service.forRoot === undefined) return service
    try {
      return service.forRoot!(key)
    } catch {
      return service
    }
  }

  // Turn-boundary auto-drive (2026-08-24): mechanical replacement for the
  // user's 31 manual pushes. Enabled by default; see drive.ts for guardrails.
  let drive: DriveHandle | undefined
  if (config.enableDrive !== false) {
    drive = applyDrive(ctx, service, {
      ...config.driveDelayMs !== undefined ? { delayMs: config.driveDelayMs } : {},
      ...config.driveMaxConsecutiveSimple !== undefined
        ? { maxConsecutiveSimple: config.driveMaxConsecutiveSimple }
        : {},
      ...config.driveDeadline !== undefined ? { deadline: config.driveDeadline } : {},
      ...config.driveWakeIntervalMs !== undefined
        ? { wakeIntervalMs: config.driveWakeIntervalMs }
        : {},
      ...config.driveAutoResume !== undefined ? { autoResume: config.driveAutoResume } : {},
    })
  }

  // One-shot runtime deadline control (方案 B, 2026-09-17): the MODEL arms
  // the mining deadline from inside a session — the user never edits config
  // files or restarts with --patch. Calling this tool also marks the session
  // as a mining session (factor_* prefix), arming the drive for it.
  ctx.tools.register(defineTool({
    name: 'factor_drive_deadline',
    description: '设置/清除/查询「连续挖掘死线」（会话级，跨重启持久，运行时状态，无需重启）。'
      + '当用户要求"连续挖掘到某时刻/挖到 HH:MM/通宵挖到明早 X 点"时：把用户口述的时刻换算成 ISO 8601（本地时区，如 2026-09-18T08:00）调用 set——死线只作用于当前会话（并行挖掘会话互不影响）。'
      + '到点自动注入一次收官指令（汇总战果、不开新试验）后该会话解除自动推进、交还用户。'
      + '死线跨重启持久：宿主重启后仍然有效；重启时已过期的死线自动取消并静默交还用户（不补收官注）。'
      + '收官/取消后，用户在本会话发一条真实消息即恢复常规自动推进。'
      + '提前收工用 clear（清除全部死线与收官标记）；不带参数调用=查询当前状态。'
      + '用户明确说出的时刻才是死线来源，不要自行推测。',
    parameters: {
      deadline: { type: 'string', description: '死线时刻，ISO 8601 本地时区（如 2026-09-18T08:00）。设置或替换当前会话的死线。' },
      wakeIntervalMs: { type: 'number', description: '可选：巡检间隔毫秒（周期性检查停摆会话并重新点火），缺省 300000（5 分钟）。' },
      clear: { type: 'boolean', description: 'true=清除全部死线与收官标记，恢复常规自动推进。' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      // 双实例修复（2026-10-07）：本工具可能由预置行实例提供（enableDrive:
      // false——防双注入器的归属翻转），而注入器由全局行实例持有。死线
      // 状态是文件态：无句柄时直写 ~/.dsh/factor-drive-state.json，持有
      // 注入器的实例在 driveNow/扫描入口回读盘同步接管（不再误报不可用）。
      const dl = drive ?? fileDeadlineApi()
      if (args.clear === true) {
        dl.clearDeadline()
        return { ok: true, cleared: true, status: drive?.status() ?? {
          note: '本实例未持注入器——死线状态已直写盘，由全局注入器实例接管',
          record: fileDeadlineApi().statusFor(
            (exec as { agent?: { id?: unknown } } | undefined)?.agent?.id) ?? null,
        } } as unknown as JsonValue
      }
      if (typeof args.deadline === 'string' && args.deadline !== '') {
        const sessionId = (exec as { agent?: { id?: unknown } } | undefined)?.agent?.id
        const r = dl.setDeadline(args.deadline, sessionId, args.wakeIntervalMs)
        return { ...r, ...(drive === undefined ? { via: 'file' } : {}), status: drive?.status() ?? null } as unknown as JsonValue
      }
      return { ok: true, status: drive?.status() ?? {
        note: '本实例未持注入器——查询直读盘',
        record: fileDeadlineApi().statusFor(
          (exec as { agent?: { id?: unknown } } | undefined)?.agent?.id) ?? null,
      } } as unknown as JsonValue
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_status',
    description: '报告因子挖掘服务概况：已配置的数据环境、Python 桥、用户状态目录；响应内嵌引擎停走指令（loop——state=running 表示必须继续挖掘内循环，strategy 为引擎机械选定的下一步方向类型）与本线最近 5 条试验投影。',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).status({ lane: laneOfExec(exec) }))
    },
  }))

  // Dual-line ledger switching (2026-09-17): the model binds THIS session to
  // one configured state root — every subsequent factor_* call routes there.
  // Calling this tool also marks the session as a mining session (factor_*
  // prefix arms the drive injector for it).
  ctx.tools.register(defineTool({
    name: 'factor_root_use',
    description: '切换/查询本会话使用的因子账本（多账本部署：个股线与 ETF 线各自独立的试验账、入册账与多重检验计数池——两线的试验计数互不串账）。'
      + '当用户要求"挖 ETF 线/切到 etf 账本/双线分开挖"时调用本工具绑定；一个会话同一时刻只属于一个账本（对齐并行线隔离纪律），'
      + '切换即重绑（后续所有 factor_* 调用走新账本）。不带参数调用=列出可用账本与当前绑定。绑定后请按需 factor_load_env 该账本配置的环境。',
    parameters: {
      root: { type: 'string', description: '目标账本键（见无参调用返回的 roots 列表，如 "etf"；"default"=默认个股账本）。' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const roots = service.listRoots?.() ?? [{ key: 'default', stateRoot: '(single-root provider)' }]
      const sessionId = (exec as { agent?: { id?: unknown } } | undefined)?.agent?.id
      if (typeof args.root === 'string' && args.root !== '') {
        if (!roots.some((r) => r.key === args.root)) {
          return { ok: false, error: `未知账本 "${args.root}"`, availableRoots: roots } as unknown as JsonValue
        }
        if (sessionId !== undefined) bindSessionRoot(sessionId, args.root)
        const st = await svcFor(exec).status({ lane: laneOfExec(exec) })
        return { ok: true, boundRoot: args.root, roots, status: st } as unknown as JsonValue
      }
      return { ok: true, roots, currentRoot: rootOfExec(exec) ?? 'default' } as unknown as JsonValue
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_data_probe',
    description: '探查用户数据文件（parquet/csv/通配符路径）并返回其列名与建议的开高低收/成交量（OHLCV）、符号、日期列映射。探查只读不写——不会落任何配置。',
    parameters: {
      path: { type: 'string', required: true, description: 'Absolute path to the user data file.' },
      layout: { type: 'string', description: 'Optional layout hint: long, wide, per_symbol, multiindex.' },
      dateFormat: { type: 'string', description: 'Optional strftime date format hint.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).dataProbe({ path: args.path, ...args.layout !== undefined ? { layout: args.layout } : {}, ...args.dateFormat !== undefined ? { dateFormat: args.dateFormat } : {} }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_config_write',
    description: '把用户确认过的数据配置 JSON 写入状态根目录（省略 path 时按约定写 stateRoot/data-config.json，写入立即生效）。环境 id 用数据自身的名字（如 etf / stock_smallcap，不要用 primary）。结构：{"version":1,"environments":{"<id>":{"label","source":{"type":"parquet","path":"<绝对路径>"},"layout":"long","mapping":{"symbol","date","open","high","low","close","volume","amount"},"constraints"}},"library":{"path":"known_factors.py"}?}。未声明的字段会被拒绝并附一份 schema 示例——读错误信息改，不要盲猜格式；不确定时先用 factor_config_validate 验证。',
    parameters: {
      config: { type: 'json', required: true, description: 'Complete data configuration object (a JSON string is also accepted).' },
      path: { type: 'string', description: 'Optional config file path; defaults to the state-root convention data-config.json.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).dataConfigWrite(jsonRecord(args.config, 'config'), args.path))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_config_validate',
    description: '验证一份数据配置的合法性，不写入任何文件。',
    parameters: {
      config: { type: 'json', required: true, description: 'Data configuration object to validate (a JSON string is also accepted).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).dataConfigValidate(jsonRecord(args.config, 'config')))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_load_env',
    description: '加载（或复用已加载的）一个已配置数据环境，并报告其维度（日期数/标的数）。',
    parameters: {
      envId: { type: 'string', description: 'Environment id from factor_config_write; defaults to primary.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).dataLoad({ ...args.envId !== undefined ? { envId: args.envId } : {} }))
    },
  }))

  const stage = {
    type: 'string' as const,
    description: 'development (free) | selection (semi-consumable) | test (one-shot lock).',
    enum: ['development', 'selection', 'test'] as const,
  }

  ctx.tools.register(defineTool({
    name: 'factor_check_causality',
    description: '对一份 factor(env) Python 因子源码跑噪声扰动因果检验——必须先过本检验才能 factor_evaluate。检验会执行 factor(env) 共 8 次。第一稿就要写向量化代码（rolling/ewm/广播，不要写逐元素循环）：引擎在面板小切片上对每次提交做冒烟预检（快速试运行测耗时），预计超出算力预算的实现会被硬拒——不消耗试验计数，错误信息附实测耗时数字与重写指引；被拒后等 1-3 分钟原样重交同一份源码即可。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python source defining def factor(env) -> np.ndarray.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).checkCausality({ envId: args.envId ?? 'primary', source: args.source }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_evaluate',
    description: '在选定数据区上评估一份因子源码，返回完整诊断对象（IC 均值、IC_IR 信息比率、列置换检验、beta 暴露、年度分解、衰减、Top-N 分组）。默认异步：返回 {job_id, status}——把 job_id 传回本工具轮询取结果（running/queued=未完成；返回完整诊断=完成）。若响应已带 verdict，那是评估缓存命中——结果终局，无需再轮询。在途作业期间重交完全相同的参数会自动并入同一作业（dedup: true，不重复计算）。第一稿就写向量化代码：可编译加速的循环实现会被引擎自动加速（等价性严格验证），被拒的慢实现会被自动重写（仅严格等价变换）；冒烟预检被拒不消耗试验计数——等 1-3 分钟原样重交同一份源码。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', description: 'Python source defining def factor(env) -> np.ndarray. OMIT when polling a job (job_id only).' },
      stage,
      horizon: { type: 'string', description: 'Hypothesis horizon: an integer from the environment menu (e.g. "10"), or "scan" to profile every menu horizon (each an independent full diagnosis). Defaults to the main horizon. Changing horizon = a new bet; accounted per (source, horizon).' },
      job_id: { type: 'string', description: 'POLL: pass the job_id returned by a previous call to fetch its result (works for any async job: evaluate/composite/batch/audit).' },
      async: { type: 'boolean', description: 'Escape hatch: pass async:false to force a blocking synchronous evaluation (default is async-by-default with job_id polling).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).evaluate({
        envId: args.envId ?? 'primary',
        source: args.source ?? '',
        ...args.stage !== undefined ? { stage: args.stage } : {},
        ...args.horizon !== undefined ? { horizon: args.horizon } : {},
        ...args.job_id !== undefined ? { job_id: args.job_id } : {},
        ...args.async !== undefined ? { async: args.async } : {},
        lane: laneOfExec(exec),
        ...journalLaneOfConfig(config) !== undefined ? { journal_lane: journalLaneOfConfig(config)! } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_evaluate_composite',
    description: '评估一个组合因子：相对各成分因子逐层拆解增量贡献（先看组合相对成分等权基准的增量，再看每去掉一个成分后的变化）。建议大面板传 async: true 走后台作业（返回 {job_id, status}）；把 job_id 传回本工具轮询取结果。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', description: 'Composite Python factor source. OMIT when polling a job.' },
      ingredients: { type: 'json', description: 'Map of ingredient name -> Python factor source. OMIT when polling a job.' },
      job_id: { type: 'string', description: 'POLL: fetch the result of a previously submitted async job.' },
      async: { type: 'boolean', description: 'Submit as a background job (returns job_id) instead of blocking synchronously.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).evaluateComposite({
        envId: args.envId ?? 'primary',
        source: args.source ?? '',
        // 轮询（job_id only）不带 ingredients——无条件 jsonRecord 会在
        // undefined 上直接 throw，工具自己教的轮询方式必炸（R24 复审 P0）
        ...args.ingredients !== undefined
          ? { ingredients: jsonRecord(args.ingredients, 'ingredients') as Record<string, string> }
          : {},
        ...args.job_id !== undefined ? { job_id: args.job_id } : {},
        ...args.async !== undefined ? { async: args.async } : {},
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_evaluate_batch',
    description: '批量评估一组因子源码，批内做跨因子多重检验校正（按有效独立试验数 N_eff 做 Sidak 收缩）。sources 是 JSON 对象：因子名 -> Python 源码，每个源码定义 `def factor(env)`，如 {"f1": "def factor(env):\\n    ...", "f2": "..."}。factor_random_generate 返回的 sources 已符合该契约——原样传入即可。建议大面板传 async: true 走后台作业；把 job_id 传回本工具轮询取结果。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      sources: { type: 'json', description: 'Map of factor name -> Python source, each defining def factor(env). Example: {"f1": "def factor(env):\\n    import pandas as pd\\n    ..."}. OMIT when polling a job.' },
      horizon: { type: 'string', description: 'Declared horizon for the whole batch: an integer from the environment menu (scan is NOT supported for batch). Defaults to the main horizon. Changing horizon = a new bet; accounted per (source, horizon).' },
      job_id: { type: 'string', description: 'POLL: fetch the result of a previously submitted async job.' },
      async: { type: 'boolean', description: 'Submit as a background job (returns job_id) instead of blocking synchronously.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).evaluateBatch({
        envId: args.envId ?? 'primary',
        ...args.sources !== undefined
          ? { sources: jsonRecord(args.sources, 'sources') as Record<string, string> }
          : {},
        ...args.horizon !== undefined ? { horizon: args.horizon } : {},
        ...args.job_id !== undefined ? { job_id: args.job_id } : {},
        ...args.async !== undefined ? { async: args.async } : {},
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_walk_forward',
    description: '对单因子做时间顺序的前推走查稳健性检验（按时间切成有序块逐块评估，看表现是否只在个别块成立）。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python factor source.' },
      n_folds: { type: 'integer', description: 'Number of ordered blocks (default 5).' },
      t0_date: { type: 'string', description: 'Inclusive start date (default DEV_END).' },
      t1_date: { type: 'string', description: 'Exclusive end date (default end).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).walkForward({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.n_folds !== undefined ? { n_folds: args.n_folds } : {},
        ...args.t0_date !== undefined ? { t0_date: args.t0_date } : {},
        ...args.t1_date !== undefined ? { t1_date: args.t1_date } : {},
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_noise_test',
    description: '过拟合硬门：在 M 个合成纯噪声世界（按每标的真实波动率匹配的高斯随机游走行情；点时序可见性掩码与交易日历保持真实）上直接跑该因子。真 Alpha 按构造不可能预测噪声——若跨噪声世界出现系统性 IC（|z| >= 3），说明因子公式在拟合评估口径的伪象，提交入册时会被无条件拒绝。返回完整 IC_IR 分布（均值/标准差/分位数/z），不只给结论。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python factor source defining def factor(env).' },
      m: { type: 'integer', description: 'Number of noise worlds (default 100, cap 300).' },
      seed: { type: 'integer', description: 'Base seed; defaults to one derived from the environment fingerprint (reproducible).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).noiseTest({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.m !== undefined ? { m: args.m } : {},
        ...args.seed !== undefined ? { seed: args.seed } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_day_perm_test',
    description: '真实数据上的精确时间对齐零检验：两侧边际都保持真实（因子截面与收益截面、全部市场结构原样），只随机重配两者的时间线——它是 evaluate 内置列置换检验的时间维孪生。p_two 小（alignment_dependent=true）= 因子表现依赖精确的时间对齐——既可能是真短线因子也可能是择时性过拟合，样本内无法区分；p_two 大 = 持久倾斜结构主导（列置换已认证的合法截面形态）。提交入册时只报告不拒绝：门的方向由已知好因子与已证伪因子的标定决定。返回观测统计量对完整置换零分布（分位数、p_upper/p_lower/p_two、百分位）。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python factor source defining def factor(env).' },
      m: { type: 'integer', description: 'Number of random pairings (default 200, cap 500).' },
      seed: { type: 'integer', description: 'Base seed; defaults to one derived from the environment fingerprint (reproducible).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).dayPermTest({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.m !== undefined ? { m: args.m } : {},
        ...args.seed !== undefined ? { seed: args.seed } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_audit',
    description: '对因子源码跑独立双实现审计（第二套实现复核第一套的数字，防实现层 bug）。审计内嵌一次完整评估，大面板建议传 async: true 走后台作业；把 job_id 传回本工具轮询取结果。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', description: 'Python factor source. OMIT when polling a job.' },
      job_id: { type: 'string', description: 'POLL: fetch the result of a previously submitted async job.' },
      async: { type: 'boolean', description: 'Submit as a background job (returns job_id) instead of blocking synchronously.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).audit({
        envId: args.envId ?? 'primary',
        source: args.source ?? '',
        ...args.job_id !== undefined ? { job_id: args.job_id } : {},
        ...args.async !== undefined ? { async: args.async } : {},
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_random_generate',
    description: "随机因子种子生成。mode=explore：生成 n 个随机因子做轻量 IC 扫描，返回 top_k 份源码走标准管线（幸存者只是「值得验证的选择」不是结论——下任何判断前先跑 causality/evaluate/evaluate_batch）。mode=null-calibration：构建并持久化本池的经验零分布 IC_IR 地形（用于「新因子要超过零分布 p95 才有意思」的门槛）。mode=collision-card：抽一张随机概念刺激卡（内容盲采样自你的推理日志词表+文献台账；零算力消耗）。连接、变形、弃权都行——弃权免费，随时再抽；若某个连接长成了机制假设，把 card_id 写进 trail 条目的 collision_card 字段。top/top_tail 桶按新颖度选种（排名分 + 0.3×(1−与本次批内及上次选中种子的最大|ρ|)）——同族次优让位给独立候选；novelty_lambda=0 恢复纯分数排序。",
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      mode: { type: 'string', description: 'explore (default), null-calibration, or collision-card.', enum: ['explore', 'null-calibration', 'collision-card'] },
      n: { type: 'integer', description: 'Number of random factors to generate (default 50).' },
      seed: { type: 'integer', description: 'Reproducibility seed (default 42; collision-card: omit for runtime entropy — seed_used is returned and logged).' },
      top_k: { type: 'integer', description: 'explore only: top sources by |IC_IR| (default 5).' },
      k: { type: 'integer', description: 'collision-card only: concept count, 1-3 (default 2).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).randomGenerate({
        envId: args.envId ?? 'primary',
        ...args.mode !== undefined ? { mode: args.mode } : {},
        ...args.n !== undefined ? { n: args.n } : {},
        ...args.seed !== undefined ? { seed: args.seed } : {},
        ...args.top_k !== undefined ? { top_k: args.top_k } : {},
        ...args.k !== undefined ? { k: args.k } : {},
        // R17 第三犯（2026-09-29 ETF 夜：explore 幸存者哈希全记 default 线，
        // origin=random 判据跨线失真）：lane 必须转发——TS schema 声明 +
        // handler 转发缺一即静默丢参（双层契约铁律）
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_operators',
    description: '查看或配置随机种子生成器的生效算子集。action=get 返回当前生效集；action=set 向状态根写入用户覆盖 {disable?: string[], windows?: number[], delays?: number[], humps?: number[], disable_templates?: string[], template_share?: number}。',
    parameters: {
      action: { type: 'string', description: 'get (default) or set.', enum: ['get', 'set'] },
      override: { type: 'json', description: 'set only: {disable?, windows?, delays?} override object.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const req: Record<string, unknown> = {}
      if (args.action !== undefined) req.action = args.action
      if (args.override !== undefined) req.override = jsonRecord(args.override, 'override')
      return json(svcFor(exec).operators(req as unknown as OperatorsRequest))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_null_landscape',
    description: '查询已持久化的随机零分布 IC_IR 地形（本池难度标定）。新因子只有超过零分布 p95 才算有意思。',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).nullLandscape({}))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_state_reset',
    description: '按范围重置插件状态——用 shell 删状态目录的安全替代品。scope：mining（试验账/已证伪路径/搜索路径/挖掘状态）| landscape（零分布地形/算子集）| registry（已入册候选）| config（data-config.json，回到冷启动）| all。每个被删文件先备份到 stateRoot/backups/<时间戳>/。test_lock.json 永不重置（test 区一次性纪律锁不能被工具撤销——只能手动删）。',
    parameters: {
      scope: { type: 'string', description: 'mining | landscape | registry | config | all (default mining).', enum: ['mining', 'landscape', 'registry', 'config', 'all'] },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const req: Record<string, unknown> = {}
      if (args.scope !== undefined) req.scope = args.scope
      return json(svcFor(exec).stateReset(req as unknown as StateResetRequest))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_trail_summary',
    description: '挖掘历史聚合视图：引擎试验账（自动记录的硬事实——每次评估的判定与红旗，模型瞒不了）+ 叙事试验账 + 挖掘状态 + 记忆池统计。恢复会话或决定下一个假设前先读这个。',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).trailSummary({ lane: laneOfExec(exec) }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_export_report',
    description: '为一个已评估的因子导出可分享的 markdown 结果卡（判定、关键数字、红旗、零分布对比、指纹、因子源码）。全部从引擎试验账拼装、零重算；前提是该因子已评估过。写入 stateRoot/reports/<时间戳>_<名称>.md 并返回内容。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python factor source (must match a prior factor_evaluate).' },
      name: { type: 'string', description: 'Factor name for the report title and filename.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).exportReport({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.name !== undefined ? { name: args.name } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_query_library',
    description: '查询用户提供的已知因子库。未配置是合法状态，返回 library_not_configured。',
    parameters: {
      query: { type: 'string', required: true, description: 'Hypothesis or keyword text.' },
      top_k: { type: 'integer', description: 'Maximum hits (default 5).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).queryLibrary({ query: args.query, ...args.top_k !== undefined ? { top_k: args.top_k } : {} }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_query_paths',
    description: "提议新假设前先查已试过的路径。layer=trail_similarity：候选因子源码对全部已试构造的结构相似预检（MinHash 近似）——它是查重不是新颖性审判：只报相似度事实，不写任何账，也不替你做决定（自提机制假设的工作流：烧掉一次评估前先跑这个）。",
    parameters: {
      layer: { type: 'string', required: true, enum: ['explored', 'search_paths', 'trail_similarity'], description: 'explored (falsified concrete explorations), search_paths (process-level variants), or trail_similarity (structural similarity pre-check).' },
      query: { type: 'string', required: true, description: 'Hypothesis or keyword text (unused by trail_similarity; pass a placeholder).' },
      source: { type: 'string', description: 'trail_similarity only: candidate factor source code.' },
      top_k: { type: 'integer', description: 'Maximum hits (default 5).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).queryPaths({ layer: args.layer, query: args.query, ...args.source !== undefined ? { source: args.source } : {}, ...args.top_k !== undefined ? { top_k: args.top_k } : {} }))
    },
  }))

  const record = (toolName: string, description: string, layer: 'trail' | 'explored' | 'search_paths') => {
    ctx.tools.register(defineTool({
      name: toolName,
      description,
      parameters: {
        entry: { type: 'json', required: true, description: 'One record object written to the user state directory.' },
      },
      output: { schema: { type: 'json' }, render: JSON_RENDER },
      async execute(args, exec) {
        // lane 来自会话身份（基础设施事实），entry 里模型自填的 lane
        // 由 Python 侧覆盖——归属不可被模型声明
        const lane = laneOfExec(exec)
        if (layer === 'trail') return json(svcFor(exec).appendTrail(jsonRecord(args.entry, 'entry'), lane))
        if (layer === 'explored') return json(svcFor(exec).appendExplored(jsonRecord(args.entry, 'entry'), lane))
        return json(svcFor(exec).appendSearchPath(jsonRecord(args.entry, 'entry'), lane))
      },
    }))
  }
  record('factor_record_trail', '追加一条完整的探索叙事条目（轮次、信号、评估、归因、下一步假设）。', 'trail')
  record('factor_record_explored', '追加一条已证伪的具体探索（探索内容、维度、证据、根因、关键词）。', 'explored')
  record('factor_record_search_path', '追加一条过程级搜索路径条目（方向、变体、结果、关键词）。', 'search_paths')

  ctx.tools.register(defineTool({
    name: 'factor_registry_get',
    description: '读取当前用户因子入册账（已通过全部准入门的因子清单）。',
    parameters: {
      envId: { type: 'string', description: 'Environment id for contextual metadata.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).registryGet(args.envId))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_registry_submit',
    description: '对一个候选因子跑准入门判定并提交入册。要点：把完整的 factor_evaluate 结果作为 diagnosis **原样**传入——桥会校验其中的回执（回执在场但关键数字被改——包括 deflated_train 充分统计量——会被拒绝；p 值重算也以引擎试验账里记录的统计量为准），执行铁律（同一份源码不能换名重复入册；同一个名字不能提交两次——重复提交会被拒），并拒绝带红旗的结果。之后想改描述用 factor_registry_update，不要重新提交。提交时引擎自动跑四道门：噪声世界检验（|z|>=3 拒绝）、日期置换时间零检验（只报告不拒绝）、声明参数的平坦性检验（悬崖形态拒绝——最小步长下出现符号翻转/塌缩/退化）、尾部通道的价差噪声门（不可判定 => 尾部通道事务中止）。必须把源码里**全部**可调数字字面量声明进 flatness_params——声明的值若不在源码中出现，事务中止。注意：提交是一个长事务（多道门顺序执行，慢因子可能要几分钟）——这是正常现象，不要中途重试。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python factor source.' },
      name: { type: 'string', description: 'Candidate name.' },
      signal: { type: 'string', description: 'Human-readable precise factor definition.' },
      diagnosis: { type: 'json', description: 'The complete factor_evaluate diagnosis object, passed through verbatim from the evaluate call you are registering. Required — submissions without it are rejected.' },
      flatness_params: { type: 'json', description: 'Parameter declaration for the flatness check: [{name, value, step}] — every tunable numeric literal of the source (window lengths, thresholds, weights; NOT structural constants like 252 annualization). value must appear as a numeric literal in the source (mismatch aborts); step is the minimal meaningful step (window 10 -> 1, weight 0.65 -> 0.05). Omit entirely for genuinely parameter-free factors.' },
      admit_basis: { type: 'string', description: 'Admission track: "ic" (default, the IC_IR chain) or "tail" (the tail-spread chain for factors whose top-K group return is strong even with mediocre full IC — requires the auto-computed tail block from factor_evaluate; gates: top-N placebo z>=3, spread noise-gate |z|<3, selection-Jaccard N_eff deflation with cross-track Sidak alpha).', enum: ['ic', 'tail'] },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).registrySubmit({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.name !== undefined ? { name: args.name } : {},
        ...args.signal !== undefined ? { signal: args.signal } : {},
        ...args.diagnosis !== undefined ? { diagnosis: jsonRecord(args.diagnosis, 'diagnosis') as JsonRecord } : {},
        ...args.flatness_params !== undefined ? { flatness_params: args.flatness_params } : {},
        ...args.admit_basis !== undefined ? { admit_basis: args.admit_basis } : {},
        // lane：submit 响应内嵌按线计算的 loop 指令（pending/升级阶梯）——
        // 不盖印则回落 default 线，并行会话拿到别人的意图层指令（R24 复审）
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_registry_update',
    description: '更新一个已入册条目的描述性字段。只允许 `signal`（定义文本）和 `note`（追加备注）——源码/诊断/数字/判定是铁律领地（新源码 = 经 factor_registry_submit 的新因子；编造数字会被拒绝）。用本工具修粗糙的描述，不要换名重新提交（铁律会拦）。',
    parameters: {
      name: { type: 'string', required: true, description: 'Name of the registry entry to update.' },
      signal: { type: 'string', description: 'New human-readable factor definition (replaces the old one).' },
      note: { type: 'string', description: 'Remark appended to the entry (timestamped, kept as history).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).registryUpdate({
        name: args.name,
        ...args.signal !== undefined ? { signal: args.signal } : {},
        ...args.note !== undefined ? { note: args.note } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_standby_view',
    description: '候补线派生视图（只读）：开发段表现强但差一点没过准入门的存量因子——三条复活通道（分层再定价/规则化组合/前向孵化）的供给面。复活由引擎机械触发，模型没有申诉通道。行带分层标签（mechanism=推理日志登记过的机制层假设；其余为 mechanical 机械生成层）。',
    parameters: {
      min_abs_sr: { type: 'number', description: 'Minimum |sr_hat| for standby membership (default 0.75; will be recalibrated per-stratum after the shadow period).' },
      top_n: { type: 'integer', description: 'Max rows returned (default 30).' },
      family_dedup: { type: 'boolean', description: 'Collapse same-family entries (|rho|>=0.6) keeping the best (default true).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).standbyView({
        ...args.min_abs_sr !== undefined ? { min_abs_sr: args.min_abs_sr } : {},
        ...args.top_n !== undefined ? { top_n: args.top_n } : {},
        ...args.family_dedup !== undefined ? { family_dedup: args.family_dedup } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_standby_combine',
    description: '复活通道·规则化组合：在候补线上做规则化组合——两两正交（|ρ|<0.3）按分数取前 k，等权符号对齐。组合规则本身作为自由度=1 的机制层假设预注册；组合体走标准 evaluate_composite 管线（返回异步作业——用 factor_evaluate 的 job_id 轮询）。你只负责触发；成员由引擎选。',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      k: { type: 'integer', description: 'Combination size (default 4; rule needs >=3 orthogonal members).' },
      ortho_th: { type: 'number', description: 'Pairwise |rho| ceiling (default 0.3).' },
      min_abs_sr: { type: 'number', description: 'Standby membership floor (default 0.75).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).standbyCombine({
        envId: args.envId ?? 'primary',
        ...args.k !== undefined ? { k: args.k } : {},
        ...args.ortho_th !== undefined ? { ortho_th: args.ortho_th } : {},
        ...args.min_abs_sr !== undefined ? { min_abs_sr: args.min_abs_sr } : {},
        // 1a 全量审计补缺（2026-09-29）：combine 内部派发 evaluate_composite
        // 异步作业——lane 不转发则作业错记 default 线（活动记账/线级
        // async 豁免/_async_job_lanes 三处跨线失真）
        lane: laneOfExec(exec),
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_incubate_enqueue',
    description: '复活通道·前向孵化（最终裁决）：冻结选定的候补因子（按源码哈希）并开启 120 个交易日的前向孵化窗。选定 cohort 在统计上免费——开发段上的选择不会污染新数据；你只付出槽位和等待时间。复活线在新窗上的判据约为 IC_IR 0.28（按 cohort 规模校正）。入队即冻结源码；改动源码 = 新候选重新排队。',
    parameters: {
      hashes: { type: 'json', required: true, description: 'List of standby-line hashes (prefixes OK, from factor_standby_view). Max 50.' },
      window_days: { type: 'integer', description: 'Fresh-window length in trading days (default 120).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const hs: string[] = Array.isArray(args.hashes)
        ? args.hashes.filter((x): x is string => typeof x === 'string')
        : []
      return json(svcFor(exec).incubateEnqueue({
        hashes: hs,
        ...args.window_days !== undefined ? { window_days: args.window_days } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_incubate_status',
    description: '列出孵化 cohort 清单（状态/窗口）。新窗攒够交易日后，用 factor_incubate_judge 执行判决。',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).incubateStatus({}))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_incubate_judge',
    description: '对新窗做 cohort 一次性判决（需要环境已加载 cohort 起算日之后的新数据；窗口未满返回 not_ready——判决不被消耗）。幸存者可带 incubated_cohort 提交入册，豁免开发段的多重检验校正判据（其余门全部照旧）。',
    parameters: {
      cohort_id: { type: 'string', required: true, description: 'Cohort id from factor_incubate_status.' },
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).incubateJudge({
        cohort_id: args.cohort_id,
        envId: args.envId ?? 'primary',
      }))
    },
  }))

  // ---- 推理日志（构造面 v7 2026-09-12：双层记忆的解释层，引擎只当机械仓库管理员）----
  ctx.tools.register(defineTool({
    name: 'factor_journal_read',
    description: '读本线的推理日志（渐进披露：先看便宜的全局，按需下钻）。level 0 = 索引（每个活条目一行 + 墓志尾——墓志=已结条目的一行摘要，便宜），level 1 = 活区全文（会话开场装这个），level 2 = 按 id 取单条全文（活区或归档）。推理日志是引擎事实账之上的解释层：数字必须引用试验事实（source_hash/试验号），不许编造。',
    parameters: {
      level: { type: 'integer', description: '0=INDEX, 1=live full text, 2=one entry by entry_id (default 0).' },
      entry_id: { type: 'string', description: 'Entry id for level 2 (find ids in the INDEX).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const lane = laneOfExec(exec)
      return json(svcFor(exec).journalRead({
        ...args.level !== undefined ? { level: args.level } : {},
        ...args.entry_id !== undefined ? { entry_id: args.entry_id } : {},
        ...lane !== undefined ? { lane } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_journal_append',
    description: '向推理日志活区追加条目块（一次可多块）。首行约定：`## <id> <status> <标题>`（出生态 status：open | registered | active | lesson；id 终身不变）。`registered`（评估前预期登记）必须带 `ref:<source_hash>` 行——因子评估完成后，引擎会返回一个镜像，把你的预期与实测并置，你用 factor_journal_update 结算它。',
    parameters: {
      content: { type: 'string', required: true, description: 'Markdown entry block(s). Each starts with `## <id> <status> <title>`; a registered entry states the expectation in free text (no template).' },
      source: { type: 'string', description: 'The factor source code — REQUIRED for registered entries: the engine hashes it into the ref line (you cannot know source_hash before evaluation). Pass the exact same source string you will submit to factor_evaluate.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const lane = laneOfExec(exec)
      return json(svcFor(exec).journalAppend({
        content: args.content,
        ...args.source !== undefined ? { source: args.source } : {},
        ...lane !== undefined ? { lane } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_journal_update',
    description: '推进一个日志条目的状态：open→registered→adjudicated→resolved|stale，active→retired（adjudicated 由引擎镜像自动设置；你用 resolved/stale + 一句「实测教了什么」的 note 完成结算）。同状态调用只追加 note（信念校准更新走这个）。',
    parameters: {
      id: { type: 'string', required: true, description: 'Entry id (from INDEX).' },
      status: { type: 'string', required: true, enum: ['open', 'registered', 'adjudicated', 'resolved', 'stale', 'active', 'retired', 'lesson'], description: 'Target status.' },
      note: { type: 'string', description: 'Optional note (≤300 chars) — resolution cause / calibration update. Becomes the epitaph tail when the entry is later archived.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const lane = laneOfExec(exec)
      return json(svcFor(exec).journalUpdate({
        id: args.id, status: args.status,
        ...args.note !== undefined ? { note: args.note } : {},
        ...lane !== undefined ? { lane } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_journal_distill',
    description: '推理日志的弧末蒸馏（换向前调用——loop 义务会提醒）。你提供蒸馏后的新活区全文（前言 + 用自己的话保下来的条目）；引擎先把旧活区快照进 raw/（审计——预测史不可悄悄改写），归档被丢弃的已结条目（resolved/stale/retired/lesson）并生成墓志行，被丢弃的活条目（open/registered/adjudicated/active）会被自动保留并告警。',
    parameters: {
      new_live: { type: 'string', required: true, description: 'Distilled live area full text. Keep: current beliefs (with confidence + falsification condition + trial refs), open hypotheses, live lessons. Drop: narrative, resolved entries (auto-archived).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      const lane = laneOfExec(exec)
      return json(svcFor(exec).journalDistill({
        new_live: args.new_live,
        ...lane !== undefined ? { lane } : {},
        ...journalLaneOfConfig(config) !== undefined ? { journal_lane: journalLaneOfConfig(config)! } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_journal_stats',
    description: '推理日志遥测：各状态活条目计数、登记率（有预登记预期的评估占比）、蒸馏次数、活区大小对预算。登记率是推理协议的合规指标。',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      const lane = laneOfExec(exec)
      return json(svcFor(exec).journalStats({ ...lane !== undefined ? { lane } : {} }))
    },
  }))

  if (config.enableArxivSearch) {
    ctx.tools.register(defineTool({
      name: 'factor_arxiv_search',
      description: '在 arxiv 检索方法论论文（默认 q-fin 类目；结果对文献台账去重——已见过的论文带 seen_before 并把席位让给新论文，已标枯竭的论文被排除）。返回 {query, results[], fresh, ledger}。loop.strategy 里给了引擎轮转种子查询时优先用它；实际用到的 arxiv_id 引用在 trail 条目的 papers 字段里。',
      parameters: {
        query: { type: 'string', required: true, description: 'Methodology query.' },
        max_results: { type: 'integer', description: 'Maximum results (default 10, cap 25).' },
        category: { type: 'string', description: 'Optional explicit arxiv category (e.g. q-fin.ST). Omit for the default q-fin OR-chain filter.' },
        start: { type: 'integer', description: 'Pagination offset (0-based) for deep walks of the same query instead of re-hitting the top-N.' },
      },
      output: { schema: { type: 'json' }, render: JSON_RENDER },
      async execute(args, exec) {
        const svc = svcFor(exec)
        if (svc.arxivSearch === undefined) throw new Error('arxiv search is not provided by the mounted factor-mining service')
        return json(svc.arxivSearch({
          query: args.query,
          ...args.max_results !== undefined ? { max_results: args.max_results } : {},
          ...args.category !== undefined ? { category: args.category } : {},
          ...args.start !== undefined ? { start: args.start } : {},
        }))
      },
    }))
  }
}
