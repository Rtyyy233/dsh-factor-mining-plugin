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
import { applyDrive, type DriveHandle } from './drive.ts'
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
  /** Max consecutive `continue` injections before yielding (default 5). */
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
  const service = ctx.factorMining as FactorMiningService
  // Dual-line routing (2026-09-17): one session, one ledger. A session that
  // called factor_root_use sticks to that root's bridge; unbound sessions
  // (and providers without forRoot) use the default root.
  const svcFor = (exec: unknown): FactorMiningService => {
    const key = rootOfExec(exec)
    if (key === undefined || service.forRoot === undefined) return service
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
    })
  }

  // One-shot runtime deadline control (方案 B, 2026-09-17): the MODEL arms
  // the mining deadline from inside a session — the user never edits config
  // files or restarts with --patch. Calling this tool also marks the session
  // as a mining session (factor_* prefix), arming the drive for it.
  ctx.tools.register(defineTool({
    name: 'factor_drive_deadline',
    description: '设置/清除/查询「连续挖掘死线」（一次性运行时状态，无需重启，重启即失）。'
      + '当用户要求"连续挖掘到某时刻/挖到 HH:MM/通宵挖到明早 X 点"时：把用户口述的时刻换算成 ISO 8601（本地时区，如 2026-09-18T08:00）调用 set。'
      + '死线武装后自动推进器的「连续简单注入 ≤5 次」上限让位，由死线接管；到点自动注入一次收官指令（汇总战果、不开新试验）后解除自动推进，会话交还用户。'
      + '提前收工用 clear；不带参数调用=查询当前状态。用户明确说出的时刻才是死线来源，不要自行推测。',
    parameters: {
      deadline: { type: 'string', description: '死线时刻，ISO 8601 本地时区（如 2026-09-18T08:00）。设置或替换当前死线。' },
      wakeIntervalMs: { type: 'number', description: '可选：兜底巡检间隔毫秒（停摆会话重新点火），缺省 300000（5 分钟）。' },
      clear: { type: 'boolean', description: 'true=清除死线，恢复常规自动推进（连续简单上限重新生效）。' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      if (drive === undefined) {
        return { ok: false, error: '自动推进未启用（enableDrive=false），死线不可用' }
      }
      if (args.clear === true) {
        drive.clearDeadline()
        return { ok: true, cleared: true, status: drive.status() } as unknown as JsonValue
      }
      if (typeof args.deadline === 'string' && args.deadline !== '') {
        const r = drive.setDeadline(args.deadline, args.wakeIntervalMs)
        return { ...r, status: drive.status() } as unknown as JsonValue
      }
      return { ok: true, status: drive.status() } as unknown as JsonValue
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_status',
    description: 'Report the factor-mining service, configured data environments, Python bridge, and user state directory.',
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
    description: '切换/查询本会话使用的因子账本（多账本双线部署：stock 与 etf 各自独立的 trail/registry/多重检验池）。'
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
    description: 'Probe a user data file (parquet/csv/glob) and return its columns plus suggested OHLCV/symbol/date mappings. Probe never writes a configuration.',
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
    description: 'Write a user-confirmed data configuration JSON to the factor-mining state root (convention path stateRoot/data-config.json when path is omitted; the write takes effect immediately). Env ids use the data\'s own name (etf / stock_smallcap, NOT primary). Structure: {"version":1,"environments":{"<id>":{"label","source":{"type":"parquet","path":"<abs path>"},"layout":"long","mapping":{"symbol","date","open","high","low","close","volume","amount"},"constraints"}},"library":{"path":"known_factors.py"}?}. Unknown fields are REJECTED with a schema example — read the error, never blind-guess formats. Validate first with factor_config_validate when unsure.',
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
    description: 'Validate a data configuration without writing it.',
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
    description: 'Load (or reuse) a configured data environment and report its dimensions.',
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
    description: 'Run the noise-perturbation causality test for a factor(env) Python source. Must be causal before factor_evaluate. Runs factor(env) x8 — write vectorized code from the first draft (rolling/ewm/broadcast): the engine smoke-tests every submission on a small panel slice and hard-rejects (zero trial) implementations projected to exceed the worker budget, with measured numbers and rewrite guidance in the error.',
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
    description: 'Evaluate a factor source on the selected region and return the full diagnosis object (IC, IC_IR, column-perm, beta, yearly decomposition, decay, top-N). ASYNC BY DEFAULT (2026-08-31): returns {job_id, status} — poll by passing job_id back to this tool (running/queued = not done; full diagnosis = done). A response that already carries a verdict is an eval-cache HIT — final, no polling. Re-submitting identical in-flight params auto-joins the same job (dedup: true). Vectorize from the first draft (2026-09-01): the engine auto-accelerates compilable loop implementations (numba, strict-equivalence verified) and auto-rewrites rejected slow ones (strict equivalence only); a smoke-gate rejection costs zero trials — resubmit the SAME source after 1-3 min.',
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
    description: 'Evaluate a composite factor with onion L0/L1 diagnostics against ingredient sources. Pass async: true to run as a background job (recommended on large panels — returns {job_id, status}); poll by passing job_id back to this tool.',
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
    description: 'Evaluate a batch of factor sources with cross-factor multiple-testing deflation (N_eff Sidak). sources is a JSON OBJECT mapping factor name -> Python source, each source defining `def factor(env)`: e.g. {"f1": "def factor(env):\\n    ...", "f2": "..."}. Sources returned by factor_random_generate already match this contract — pass them verbatim. Pass async: true to run as a background job (recommended on large panels); poll by passing job_id back to this tool.',
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
    description: 'Time-ordered walk-forward block robustness for one factor source.',
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
    description: 'Overfitting hard gate (2026-08-24): the factor\'s direct performance on M synthetic random-noise worlds (per-asset vol-matched Gaussian random-walk OHLCV; PIT mask/calendar preserved). A genuine alpha cannot predict noise by construction — systematic IC across noise worlds (|z| >= 3) means the factor formula is fitting an evaluation artifact and is unconditionally rejected at submit. Returns the full IC_IR distribution (mean/std/quantiles/z), not just a verdict.',
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
    description: 'Exact temporal-alignment null on REAL data (2026-08-25): keeps both marginals real (factor cross-sections AND return cross-sections, all market structure preserved) and randomly re-pairs their timelines — the temporal twin of the in-evaluate column permutation. Small p_two (alignment_dependent=true) = the factor\'s performance depends on precise time alignment — the union of genuine short-horizon factors and timing-specific overfitting, indistinguishable in-sample; large p_two = persistent-tilt structure dominates (a legitimate cross-sectional form certified by column-perm). REPORT-ONLY at submit (no rejection): gate direction is set by calibration on known-good vs falsified factors. Returns observed statistic vs the full permutation null (quantiles, p_upper/p_lower/p_two, percentile).',
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
    description: 'Run the independent two-implementation audit for a factor source. Pass async: true to run as a background job (recommended on large panels — audit embeds a full evaluate); poll by passing job_id back to this tool.',
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
    description: 'Random factor seed generation. mode=explore: generate n random factors, light-IC scan, return top_k sources for the standard pipeline (survivors are a SELECTION, not a conclusion — run causality/evaluate/evaluate_batch before claiming anything). mode=null-calibration: build the empirical null IC_IR landscape of this pool and persist it. NOTE (2026-09-13 P0a): the top/top_tail buckets are novelty-selected (rank + 0.3·(1−max|ρ|) vs this batch and the last selected seeds) — same-family runners-up yield their seats to independent candidates; novelty_lambda=0 restores pure argmax.',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      mode: { type: 'string', description: 'explore (default) or null-calibration.', enum: ['explore', 'null-calibration'] },
      n: { type: 'integer', description: 'Number of random factors to generate (default 50).' },
      seed: { type: 'integer', description: 'Reproducibility seed (default 42).' },
      top_k: { type: 'integer', description: 'explore only: top sources by |IC_IR| (default 5).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).randomGenerate({
        envId: args.envId ?? 'primary',
        ...args.mode !== undefined ? { mode: args.mode } : {},
        ...args.n !== undefined ? { n: args.n } : {},
        ...args.seed !== undefined ? { seed: args.seed } : {},
        ...args.top_k !== undefined ? { top_k: args.top_k } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_operators',
    description: 'View or configure the effective operator set for the random seed generator. action=get returns the active set; action=set writes a user override {disable?: string[], windows?: number[], delays?: number[], humps?: number[], disable_templates?: string[], template_share?: number} to the user state root.',
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
    description: 'Query the persisted random-null IC_IR landscape (pool difficulty calibration). A new factor is only interesting above the null p95.',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).nullLandscape({}))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_state_reset',
    description: 'Reset plugin state by scope — the SAFE replacement for shell-deleting the state directory. scope: mining (trail/explored/search_paths/mining_state) | landscape (null_landscape/operator_set) | registry (accepted candidates) | config (data-config.json, back to cold start) | all. Every removed file is backed up to stateRoot/backups/<timestamp>/ first. test_lock.json is NEVER reset (the one-shot test discipline cannot be un-consumed by a tool).',
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
    description: 'Aggregate mining-history view: engine trail (auto-recorded hard facts — every evaluation with verdict/red-flags, cannot be hidden by the agent) + agent narrative trail + termination state + memory-pool stats. Read this first when resuming a session or deciding the next hypothesis.',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).trailSummary({ lane: laneOfExec(exec) }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_export_report',
    description: 'Export a shareable markdown result card for one evaluated factor (verdict, key numbers, red flags, null-landscape comparison, fingerprints, factor source). Assembled from the engine trail with zero recomputation; the factor must have been evaluated first. Writes stateRoot/reports/<ts>_<name>.md and returns the content.',
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
    description: 'Query the user-provided known-factor library. An unconfigured library is legal and returns library_not_configured.',
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
    description: 'Query already explored or searched paths before proposing a new hypothesis.',
    parameters: {
      layer: { type: 'string', required: true, enum: ['explored', 'search_paths'], description: 'explored (falsified concrete explorations) or search_paths (process-level variants).' },
      query: { type: 'string', required: true, description: 'Hypothesis or keyword text.' },
      top_k: { type: 'integer', description: 'Maximum hits (default 5).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args, exec) {
      return json(svcFor(exec).queryPaths({ layer: args.layer, query: args.query, ...args.top_k !== undefined ? { top_k: args.top_k } : {} }))
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
  record('factor_record_trail', 'Append one complete exploration-trail entry (round, signal, evaluation, attribution, next_hypothesis).', 'trail')
  record('factor_record_explored', 'Append one falsified concrete exploration (exploration, dimension, evidence, root_cause, keywords).', 'explored')
  record('factor_record_search_path', 'Append one process-level search-path entry (direction, variant, result, keywords).', 'search_paths')

  ctx.tools.register(defineTool({
    name: 'factor_registry_get',
    description: 'Read the current user factor registry.',
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
    description: 'Evaluate one candidate against acceptance criteria and submit it to the user registry. IMPORTANT: pass the FULL factor_evaluate result as `diagnosis` VERBATIM — the bridge verifies the receipt inside it (a receipt present with tampered key numbers — including the deflated_train sufficient statistics — is REJECTED; the p-recompute also prefers the engine-side stats recorded in the trail), applies the iron rule (same source cannot be re-registered under a new name, and the same NAME cannot be submitted twice — duplicate submissions are rejected) and refuses red-flagged results. To fix a description later, use factor_registry_update instead of re-submitting. At submit the engine runs four autonomous gates: noise worlds (|z|>=3 rejects), day-permutation temporal null (report-only), parameter-flatness over declared params (cliff signatures REJECT — sign flip / collapse / degeneration at minimal step), and the spread noise gate for the tail track (undeterminable => transaction abort on the tail track). Declare ALL tunable numeric literals of the source in flatness_params — a declared value not present in the source aborts the transaction. NOTE: submit is a long transaction (several gates run sequentially; slow factors can take minutes) — this is normal, do not retry mid-flight.',
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
    description: 'Update DESCRIPTIVE fields of one registered entry. Only `signal` (definition text) and `note` (appended remark) are allowed — source/diagnosis/numbers/verdict are iron-rule territory (new source = new factor via factor_registry_submit; fabricated numbers are rejected). Use this to fix a sloppy description instead of re-submitting under a new name (the iron rule blocks that).',
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
    description: 'Standby-line derived view (read-only): dev-strong survivors that missed the bar — the sourcing surface for rescue channels (① stratified re-pricing / ③ rule-based combination / ⑤ forward incubation). Re-entry is mechanically triggered by the engine; there is NO appeal path. Rows carry stratum tags (mechanism = journal-registered).',
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
    description: 'Channel-3 rescue: rule-based combination over the standby line — mutually orthogonal (|rho|<0.3) top-k by score, equal-weight sign-aligned. The RULE is registered as a dof=1 mechanism-layer hypothesis; the composite runs the standard evaluate_composite pipeline (returns an async job — poll with factor_evaluate job_id). You only trigger; the engine picks members.',
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
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_incubate_enqueue',
    description: 'Channel-5 rescue (the final arbiter): freeze the chosen standby factors (source hashes) and start the 120-trading-day forward incubation window. Choosing the cohort is FREE statistically — selection on dev does not contaminate fresh data; you pay only the slot and the waiting time. Rescue line ≈ IC_IR 0.28 on the fresh window (cohort-corrected). Sources are frozen at enqueue; modifications mean a new candidate.',
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
    description: 'Channel-5: list incubation cohorts (status/window). Judgment runs via factor_incubate_judge once the fresh window has enough new trading days.',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(_args, exec) {
      return json(svcFor(exec).incubateStatus({}))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_incubate_judge',
    description: 'Channel-5: one-shot cohort judgment on the fresh window (needs the environment loaded with new data past the cohort start; not_ready if the window is not full — judging is not consumed). Survivors may be submitted with incubated_cohort to waive the dev deflated-p criterion (all other gates unchanged).',
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
    description: 'Read YOUR lane reasoning journal (progressive disclosure). level 0 = INDEX (one line per live entry + epitaph tail — cheap), level 1 = full live area (load this at session start), level 2 = one entry by id from live or arcs. The journal is the interpretation layer over the engine fact trail: numbers must cite trial facts (source_hash / trial ids), never invent them.',
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
    description: 'Append entry block(s) to the lane reasoning journal live area. First line convention: `## <id> <status> <title>` (statuses at birth: open | registered | active | lesson; ids are permanent). `registered` (pre-evaluation expectation) MUST include a `ref:<source_hash>` line — after the factor is evaluated, the engine returns a mirror juxtaposing your stated expectation against the measured outcome, and you settle it with factor_journal_update. Multiple blocks allowed in one call.',
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
    description: 'Advance one journal entry: open→registered→adjudicated→resolved|stale, active→retired (adjudicated is set by the engine mirror; you settle with resolved/stale + a note stating what the outcome taught). A same-status call just appends the note (belief calibration updates use this).',
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
    description: 'Arc-boundary distillation of the lane journal (called before switching direction — the loop obligation reminds you). You provide the distilled new live text (preamble + surviving entries in your own words); the engine first snapshots the old live into raw/ (audit — prediction history cannot be silently rewritten), archives dropped terminal entries (resolved/stale/retired/lesson) with epitaphs, and AUTO-KEEPS dropped active entries (open/registered/adjudicated/active) with a warning.',
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
    description: 'Journal telemetry: live entry counts by status, registration rate (share of evaluations that had a registered expectation), distill count, live size vs cap. Registration rate is the compliance metric of the reasoning protocol.',
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
      description: 'Search arxiv for methodology papers (q-fin categories by default; results are deduplicated against the paper ledger — seen papers carry seen_before and yield slots to fresh ones, exhausted papers are excluded). Returns {query, results[], fresh, ledger}. Use the engine seed query from loop.strategy when present; cite the arxiv_ids you actually used in the trail entry\'s papers field.',
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
