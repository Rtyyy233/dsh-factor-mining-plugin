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
import { applyDrive } from './drive.ts'

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
}

export const Config: z<Config> = z.object({
  enableArxivSearch: z.boolean().default(false),
  enableDrive: z.boolean().default(true),
  driveDelayMs: z.number().default(60_000),
  driveMaxConsecutiveSimple: z.number().default(5),
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

  // Turn-boundary auto-drive (2026-08-24): mechanical replacement for the
  // user's 31 manual pushes. Enabled by default; see drive.ts for guardrails.
  if (config.enableDrive !== false) {
    applyDrive(ctx, service, {
      ...config.driveDelayMs !== undefined ? { delayMs: config.driveDelayMs } : {},
      ...config.driveMaxConsecutiveSimple !== undefined
        ? { maxConsecutiveSimple: config.driveMaxConsecutiveSimple }
        : {},
    })
  }

  ctx.tools.register(defineTool({
    name: 'factor_status',
    description: 'Report the factor-mining service, configured data environments, Python bridge, and user state directory.',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute() {
      return json(service.status())
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
    async execute(args) {
      return json(service.dataProbe({ path: args.path, ...args.layout !== undefined ? { layout: args.layout } : {}, ...args.dateFormat !== undefined ? { dateFormat: args.dateFormat } : {} }))
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
    async execute(args) {
      return json(service.dataConfigWrite(jsonRecord(args.config, 'config'), args.path))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_config_validate',
    description: 'Validate a data configuration without writing it.',
    parameters: {
      config: { type: 'json', required: true, description: 'Data configuration object to validate (a JSON string is also accepted).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.dataConfigValidate(jsonRecord(args.config, 'config')))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_load_env',
    description: 'Load (or reuse) a configured data environment and report its dimensions.',
    parameters: {
      envId: { type: 'string', description: 'Environment id from factor_config_write; defaults to primary.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.dataLoad({ ...args.envId !== undefined ? { envId: args.envId } : {} }))
    },
  }))

  const stage = {
    type: 'string' as const,
    description: 'development (free) | selection (semi-consumable) | test (one-shot lock).',
    enum: ['development', 'selection', 'test'] as const,
  }

  ctx.tools.register(defineTool({
    name: 'factor_check_causality',
    description: 'Run the noise-perturbation causality test for a factor(env) Python source. Must be causal before factor_evaluate.',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python source defining def factor(env) -> np.ndarray.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.checkCausality({ envId: args.envId ?? 'primary', source: args.source }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_evaluate',
    description: 'Evaluate a factor source on the selected region and return the full diagnosis object (IC, IC_IR, column-perm, beta, yearly decomposition, decay, top-N).',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python source defining def factor(env) -> np.ndarray.' },
      stage,
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.evaluate({ envId: args.envId ?? 'primary', source: args.source, ...args.stage !== undefined ? { stage: args.stage } : {} }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_evaluate_composite',
    description: 'Evaluate a composite factor with onion L0/L1 diagnostics against ingredient sources.',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Composite Python factor source.' },
      ingredients: { type: 'json', required: true, description: 'Map of ingredient name -> Python factor source.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.evaluateComposite({ envId: args.envId ?? 'primary', source: args.source, ingredients: jsonRecord(args.ingredients, 'ingredients') as Record<string, string> }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_evaluate_batch',
    description: 'Evaluate a batch of factor sources with cross-factor multiple-testing deflation (N_eff Sidak). sources is a JSON OBJECT mapping factor name -> Python source, each source defining `def factor(env)`: e.g. {"f1": "def factor(env):\\n    ...", "f2": "..."}. Sources returned by factor_random_generate already match this contract — pass them verbatim.',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      sources: { type: 'json', required: true, description: 'Map of factor name -> Python source, each defining def factor(env). Example: {"f1": "def factor(env):\\n    import pandas as pd\\n    ..."}' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.evaluateBatch({ envId: args.envId ?? 'primary', sources: jsonRecord(args.sources, 'sources') as Record<string, string> }))
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
    async execute(args) {
      return json(service.walkForward({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.n_folds !== undefined ? { n_folds: args.n_folds } : {},
        ...args.t0_date !== undefined ? { t0_date: args.t0_date } : {},
        ...args.t1_date !== undefined ? { t1_date: args.t1_date } : {},
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
    async execute(args) {
      return json(service.noiseTest({
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
    async execute(args) {
      return json(service.dayPermTest({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.m !== undefined ? { m: args.m } : {},
        ...args.seed !== undefined ? { seed: args.seed } : {},
      }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_audit',
    description: 'Run the independent two-implementation audit for a factor source.',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      source: { type: 'string', required: true, description: 'Python factor source.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.audit({ envId: args.envId ?? 'primary', source: args.source }))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_random_generate',
    description: 'Random factor seed generation. mode=explore: generate n random factors, light-IC scan, return top_k sources for the standard pipeline (survivors are a SELECTION, not a conclusion — run causality/evaluate/evaluate_batch before claiming anything). mode=null-calibration: build the empirical null IC_IR landscape of this pool and persist it.',
    parameters: {
      envId: { type: 'string', description: 'Environment id; defaults to primary.' },
      mode: { type: 'string', description: 'explore (default) or null-calibration.', enum: ['explore', 'null-calibration'] },
      n: { type: 'integer', description: 'Number of random factors to generate (default 50).' },
      seed: { type: 'integer', description: 'Reproducibility seed (default 42).' },
      top_k: { type: 'integer', description: 'explore only: top sources by |IC_IR| (default 5).' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      return json(service.randomGenerate({
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
    description: 'View or configure the effective operator set for the random seed generator. action=get returns the active set; action=set writes a user override {disable?: string[], windows?: number[], delays?: number[]} to the user state root.',
    parameters: {
      action: { type: 'string', description: 'get (default) or set.', enum: ['get', 'set'] },
      override: { type: 'json', description: 'set only: {disable?, windows?, delays?} override object.' },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      const req: Record<string, unknown> = {}
      if (args.action !== undefined) req.action = args.action
      if (args.override !== undefined) req.override = jsonRecord(args.override, 'override')
      return json(service.operators(req as unknown as OperatorsRequest))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_null_landscape',
    description: 'Query the persisted random-null IC_IR landscape (pool difficulty calibration). A new factor is only interesting above the null p95.',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute() {
      return json(service.nullLandscape({}))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_state_reset',
    description: 'Reset plugin state by scope — the SAFE replacement for shell-deleting the state directory. scope: mining (trail/explored/search_paths/mining_state) | landscape (null_landscape/operator_set) | registry (accepted candidates) | config (data-config.json, back to cold start) | all. Every removed file is backed up to stateRoot/backups/<timestamp>/ first. test_lock.json is NEVER reset (the one-shot test discipline cannot be un-consumed by a tool).',
    parameters: {
      scope: { type: 'string', description: 'mining | landscape | registry | config | all (default mining).', enum: ['mining', 'landscape', 'registry', 'config', 'all'] },
    },
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute(args) {
      const req: Record<string, unknown> = {}
      if (args.scope !== undefined) req.scope = args.scope
      return json(service.stateReset(req as unknown as StateResetRequest))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_trail_summary',
    description: 'Aggregate mining-history view: engine trail (auto-recorded hard facts — every evaluation with verdict/red-flags, cannot be hidden by the agent) + agent narrative trail + termination state + memory-pool stats. Read this first when resuming a session or deciding the next hypothesis.',
    parameters: {},
    output: { schema: { type: 'json' }, render: JSON_RENDER },
    async execute() {
      return json(service.trailSummary({}))
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
    async execute(args) {
      return json(service.exportReport({
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
    async execute(args) {
      return json(service.queryLibrary({ query: args.query, ...args.top_k !== undefined ? { top_k: args.top_k } : {} }))
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
    async execute(args) {
      return json(service.queryPaths({ layer: args.layer, query: args.query, ...args.top_k !== undefined ? { top_k: args.top_k } : {} }))
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
      async execute(args) {
        if (layer === 'trail') return json(service.appendTrail(jsonRecord(args.entry, 'entry')))
        if (layer === 'explored') return json(service.appendExplored(jsonRecord(args.entry, 'entry')))
        return json(service.appendSearchPath(jsonRecord(args.entry, 'entry')))
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
    async execute(args) {
      return json(service.registryGet(args.envId))
    },
  }))

  ctx.tools.register(defineTool({
    name: 'factor_registry_submit',
    description: 'Evaluate one candidate against acceptance criteria and submit it to the user registry. IMPORTANT: pass the FULL factor_evaluate result as `diagnosis` — the bridge verifies a receipt inside it (fabricated numbers are rejected/downgraded), applies the iron rule (same source cannot be re-registered under a new name, and the same NAME cannot be submitted twice — duplicate submissions are rejected) and refuses red-flagged results. To fix a description later, use factor_registry_update instead of re-submitting. At submit the engine runs three overfitting gates autonomously: noise worlds (|z|>=3 rejects), day-permutation temporal null (report-only), and parameter-flatness over declared params (report-only). Declare ALL tunable numeric literals of the source in flatness_params — a declared value not present in the source aborts the transaction.',
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
    async execute(args) {
      return json(service.registrySubmit({
        envId: args.envId ?? 'primary',
        source: args.source,
        ...args.name !== undefined ? { name: args.name } : {},
        ...args.signal !== undefined ? { signal: args.signal } : {},
        ...args.diagnosis !== undefined ? { diagnosis: jsonRecord(args.diagnosis, 'diagnosis') as JsonRecord } : {},
        ...args.flatness_params !== undefined ? { flatness_params: args.flatness_params } : {},
        ...args.admit_basis !== undefined ? { admit_basis: args.admit_basis } : {},
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
    async execute(args) {
      return json(service.registryUpdate({
        name: args.name,
        ...args.signal !== undefined ? { signal: args.signal } : {},
        ...args.note !== undefined ? { note: args.note } : {},
      }))
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
      async execute(args) {
        if (service.arxivSearch === undefined) throw new Error('arxiv search is not provided by the mounted factor-mining service')
        return json(service.arxivSearch({
          query: args.query,
          ...args.max_results !== undefined ? { max_results: args.max_results } : {},
          ...args.category !== undefined ? { category: args.category } : {},
          ...args.start !== undefined ? { start: args.start } : {},
        }))
      },
    }))
  }
}
