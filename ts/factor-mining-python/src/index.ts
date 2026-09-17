/**
 * Local Service Provider for the factor-mining capability seam.  It owns one
 * persistent Python JSON-RPC bridge process per configured state root and
 * translates the service envelope into bridge methods.  User data and user
 * state never enter the TypeScript process; the Python package reads/writes
 * them directly.
 *
 * Multi-ledger support (2026-09-17): `extraRoots` mounts additional bridge
 * processes (one per state root — trail/registry/deflation pools stay fully
 * separate, mirroring the ZCode direct-drive dual-line deployment). The
 * default `stateRoot` keeps its public `client` field for backward
 * compatibility; extra roots are reachable via `forRoot(key)`.
 * @module @deepseek-ai/dsh-factor-mining-python
 */

import { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { FactorMiningService } from '@deepseek-ai/dsh-factor-mining'
import type {
  ArxivSearchRequest,
  ArxivSearchResult,
  AppendResult,
  JournalAppendRequest,
  JournalAppendResult,
  JournalDistillRequest,
  JournalDistillResult,
  JournalReadRequest,
  JournalReadResult,
  JournalStatsRequest,
  JournalStatsResult,
  StandbyViewRequest,
  StandbyViewResult,
  StandbyCombineRequest,
  StandbyCombineResult,
  IncubateEnqueueRequest,
  IncubateEnqueueResult,
  IncubateStatusRequest,
  IncubateStatusResult,
  IncubateJudgeRequest,
  IncubateJudgeResult,
  JournalUpdateRequest,
  JournalUpdateResult,
  AuditReport,
  BatchDiagnosis,
  BatchRequest,
  CausalityVerdict,
  CompositeDiagnosis,
  CompositeRequest,
  DataConfigReport,
  DataConfigWriteResult,
  DataLoadReport,
  DataLoadRequest,
  DataProbeReport,
  DataProbeRequest,
  DayPermTestRequest,
  DayPermTestResult,
  EvaluateRequest,
  FactorDiagnosis,
  FactorMiningStatus,
  FactorSourceRequest,
  LibraryQueryRequest,
  LibraryQueryResult,
  NullLandscapeRequest,
  NullLandscapeResult,
  NoiseTestRequest,
  NoiseTestResult,
  OperatorsRequest,
  OperatorSetResult,
  PathQueryRequest,
  PathQueryResult,
  RandomGenerateRequest,
  RandomGenerateResult,
  RegistryGetResult,
  RegistrySubmitRequest,
  RegistrySubmitResult,
  RegistryUpdateRequest,
  RegistryUpdateResult,
  StateResetRequest,
  StateResetResult,
  StatusRequest,
  TrailSummaryRequest,
  TrailSummaryResult,
  ExportReportRequest,
  ExportReportResult,
  WalkForwardDiagnosis,
  WalkForwardRequest,
} from '@deepseek-ai/dsh-factor-mining'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import { JsonRpcClient } from './client.ts'

/** Plugin config; every field has a deployment-visible default. */
export interface Config {
  pythonExecutable?: string
  bridgeModule?: string
  bridgeCwd?: string
  stateRoot?: string
  /** Additional ledgers, one bridge process each (2026-09-17 dual-line):
   *  key → absolute state-root path. Callers route via forRoot(key). */
  extraRoots?: Record<string, string>
  dataConfigPath?: string
  startupTimeoutMs?: number
  requestTimeoutMs?: number
  graceMs?: number
}

/** Schemastery configuration schema. */
export const Config: z<Config> = z.object({
  pythonExecutable: z.string().default('python'),
  bridgeModule: z.string().default('dsh_factor_mining.bridge'),
  bridgeCwd: z.string(),
  stateRoot: z.string(),
  extraRoots: z.any(),
  dataConfigPath: z.string(),
  startupTimeoutMs: z.number().default(30_000),
  // Per-request timeout must EXCEED the bridge worker timeout (300s default) plus
  // headroom, or a legitimately long computation gets killed client-side before the
  // worker's own timeout error can ever surface. 360s = 300s worker + 60s margin.
  requestTimeoutMs: z.number().default(360_000),
  graceMs: z.number().default(5_000),
})

type ResolvedConfig = {
  pythonExecutable: string
  bridgeModule: string
  bridgeCwd: string
  stateRoot: string
  extraRoots: Record<string, string>
  dataConfigPath?: string
  startupTimeoutMs: number
  requestTimeoutMs: number
  graceMs: number
}

function resolveConfig(raw: Config): ResolvedConfig {
  const extra: Record<string, string> = {}
  if (raw.extraRoots !== null && typeof raw.extraRoots === 'object') {
    for (const [k, v] of Object.entries(raw.extraRoots)) {
      if (typeof v === 'string' && v !== '') extra[k] = v
    }
  }
  return {
    pythonExecutable: raw.pythonExecutable ?? 'python',
    bridgeModule: raw.bridgeModule ?? 'dsh_factor_mining.bridge',
    bridgeCwd: raw.bridgeCwd ?? process.cwd(),
    stateRoot: raw.stateRoot ?? `${process.cwd()}/.factor-mining`,
    extraRoots: extra,
    ...raw.dataConfigPath !== undefined ? { dataConfigPath: raw.dataConfigPath } : {},
    startupTimeoutMs: raw.startupTimeoutMs ?? 30_000,
    requestTimeoutMs: raw.requestTimeoutMs ?? 360_000,
    graceMs: raw.graceMs ?? 5_000,
  }
}

/**
 * One bridge-backed ledger: the full service surface as thin forwards onto a
 * single JsonRpcClient. Not a cordis Service (never context-registered) —
 * the provider composes one per state root and exposes them via forRoot().
 */
class BridgeBackend {
  constructor(readonly client: JsonRpcClient) {}

  async status(request?: StatusRequest): Promise<FactorMiningStatus> {
    return await this.client.request('status', (request ?? {}) as unknown as Record<string, unknown>) as FactorMiningStatus
  }

  async dataProbe(request: DataProbeRequest): Promise<DataProbeReport> {
    return await this.client.request('data.probe', request as unknown as Record<string, unknown>) as DataProbeReport
  }

  async dataConfigValidate(config: Record<string, unknown>): Promise<DataConfigReport> {
    return await this.client.request('config.validate', { config }) as DataConfigReport
  }

  async dataConfigWrite(config: Record<string, unknown>, path?: string): Promise<DataConfigWriteResult> {
    return await this.client.request('config.save', { config, path }) as DataConfigWriteResult
  }

  async dataLoad(request: DataLoadRequest): Promise<DataLoadReport> {
    return await this.client.request('data.load', request as unknown as Record<string, unknown>) as DataLoadReport
  }

  async checkCausality(request: FactorSourceRequest): Promise<CausalityVerdict> {
    return await this.client.request('factor.check_causality', request as unknown as Record<string, unknown>) as CausalityVerdict
  }

  async evaluate(request: EvaluateRequest): Promise<FactorDiagnosis> {
    return await this.client.request('factor.evaluate', request as unknown as Record<string, unknown>) as FactorDiagnosis
  }

  async evaluateComposite(request: CompositeRequest): Promise<CompositeDiagnosis> {
    return await this.client.request('factor.evaluate_composite', request as unknown as Record<string, unknown>) as CompositeDiagnosis
  }

  async evaluateBatch(request: BatchRequest): Promise<BatchDiagnosis> {
    return await this.client.request('factor.evaluate_batch', request as unknown as Record<string, unknown>) as BatchDiagnosis
  }

  async walkForward(request: WalkForwardRequest): Promise<WalkForwardDiagnosis> {
    return await this.client.request('factor.walk_forward', request as unknown as Record<string, unknown>) as WalkForwardDiagnosis
  }

  async noiseTest(request: NoiseTestRequest): Promise<NoiseTestResult> {
    return await this.client.request('factor.noise_test', request as unknown as Record<string, unknown>) as NoiseTestResult
  }

  async dayPermTest(request: DayPermTestRequest): Promise<DayPermTestResult> {
    return await this.client.request('factor.day_perm_test', request as unknown as Record<string, unknown>) as DayPermTestResult
  }

  async audit(request: FactorSourceRequest): Promise<AuditReport> {
    return await this.client.request('factor.audit', request as unknown as Record<string, unknown>) as AuditReport
  }

  async randomGenerate(request: RandomGenerateRequest): Promise<RandomGenerateResult> {
    return await this.client.request('factor.random_generate', request as unknown as Record<string, unknown>) as RandomGenerateResult
  }

  async operators(request: OperatorsRequest): Promise<OperatorSetResult> {
    return await this.client.request('factor.operators', request as unknown as Record<string, unknown>) as OperatorSetResult
  }

  async nullLandscape(request: NullLandscapeRequest): Promise<NullLandscapeResult> {
    return await this.client.request('factor.null_landscape', request as unknown as Record<string, unknown>) as NullLandscapeResult
  }

  async stateReset(request: StateResetRequest): Promise<StateResetResult> {
    return await this.client.request('state.reset', request as unknown as Record<string, unknown>) as StateResetResult
  }

  async trailSummary(request: TrailSummaryRequest): Promise<TrailSummaryResult> {
    return await this.client.request('state.trail_summary', request as unknown as Record<string, unknown>) as TrailSummaryResult
  }

  async exportReport(request: ExportReportRequest): Promise<ExportReportResult> {
    return await this.client.request('report.export', request as unknown as Record<string, unknown>) as ExportReportResult
  }

  async queryLibrary(request: LibraryQueryRequest): Promise<LibraryQueryResult> {
    return await this.client.request('library.query', request as unknown as Record<string, unknown>) as LibraryQueryResult
  }

  async queryPaths(request: PathQueryRequest): Promise<PathQueryResult> {
    return await this.client.request('paths.query', request as unknown as Record<string, unknown>) as PathQueryResult
  }

  async appendTrail(entry: Record<string, unknown>, lane?: string): Promise<AppendResult> {
    return await this.client.request('paths.append', { layer: 'trail', entry, ...lane !== undefined ? { lane } : {} }) as AppendResult
  }

  async appendExplored(entry: Record<string, unknown>, lane?: string): Promise<AppendResult> {
    return await this.client.request('paths.append', { layer: 'explored', entry, ...lane !== undefined ? { lane } : {} }) as AppendResult
  }

  async appendSearchPath(entry: Record<string, unknown>, lane?: string): Promise<AppendResult> {
    return await this.client.request('paths.append', { layer: 'search_paths', entry, ...lane !== undefined ? { lane } : {} }) as AppendResult
  }

  async registryGet(envId?: string): Promise<RegistryGetResult> {
    return await this.client.request('registry.get', { envId }) as RegistryGetResult
  }

  async registrySubmit(request: RegistrySubmitRequest): Promise<RegistrySubmitResult> {
    return await this.client.request('registry.submit', request as unknown as Record<string, unknown>) as RegistrySubmitResult
  }

  async registryUpdate(request: RegistryUpdateRequest): Promise<RegistryUpdateResult> {
    return await this.client.request('registry.update', request as unknown as Record<string, unknown>) as RegistryUpdateResult
  }

  async journalRead(request: JournalReadRequest): Promise<JournalReadResult> {
    return await this.client.request('journal.read', request as unknown as Record<string, unknown>) as JournalReadResult
  }

  async journalAppend(request: JournalAppendRequest): Promise<JournalAppendResult> {
    return await this.client.request('journal.append', request as unknown as Record<string, unknown>) as JournalAppendResult
  }

  async journalUpdate(request: JournalUpdateRequest): Promise<JournalUpdateResult> {
    return await this.client.request('journal.update', request as unknown as Record<string, unknown>) as JournalUpdateResult
  }

  async journalDistill(request: JournalDistillRequest): Promise<JournalDistillResult> {
    return await this.client.request('journal.distill', request as unknown as Record<string, unknown>) as JournalDistillResult
  }

  async journalStats(request: JournalStatsRequest): Promise<JournalStatsResult> {
    return await this.client.request('journal.stats', request as unknown as Record<string, unknown>) as JournalStatsResult
  }

  async standbyView(request: StandbyViewRequest): Promise<StandbyViewResult> {
    return await this.client.request('standby.view', request as unknown as Record<string, unknown>) as StandbyViewResult
  }

  async standbyCombine(request: StandbyCombineRequest): Promise<StandbyCombineResult> {
    return await this.client.request('standby.combine', request as unknown as Record<string, unknown>) as StandbyCombineResult
  }

  async incubateEnqueue(request: IncubateEnqueueRequest): Promise<IncubateEnqueueResult> {
    return await this.client.request('incubate.enqueue', request as unknown as Record<string, unknown>) as IncubateEnqueueResult
  }

  async incubateStatus(request: IncubateStatusRequest): Promise<IncubateStatusResult> {
    return await this.client.request('incubate.status', request as unknown as Record<string, unknown>) as IncubateStatusResult
  }

  async incubateJudge(request: IncubateJudgeRequest): Promise<IncubateJudgeResult> {
    return await this.client.request('incubate.judge', request as unknown as Record<string, unknown>) as IncubateJudgeResult
  }

  async arxivSearch(request: ArxivSearchRequest): Promise<ArxivSearchResult> {
    return await this.client.request('arxiv.search', request as unknown as Record<string, unknown>) as ArxivSearchResult
  }
}

/**
 * Python subprocess provider for `ctx.factorMining`.
 */
export class FactorMiningPythonService extends FactorMiningService {
  static inject = ['subprocess']

  readonly client: JsonRpcClient

  private readonly backends = new Map<string, BridgeBackend>()
  private readonly roots = new Map<string, string>()

  constructor(ctx: Context, config: Config) {
    super(ctx)
    const resolved = resolveConfig(config)
    const subprocess = ctx.subprocess as SubprocessRuntime
    const makeClient = (stateRoot: string): JsonRpcClient => new JsonRpcClient(subprocess, {
      pythonExecutable: resolved.pythonExecutable,
      bridgeModule: resolved.bridgeModule,
      bridgeCwd: resolved.bridgeCwd,
      stateRoot,
      ...resolved.dataConfigPath !== undefined ? { dataConfigPath: resolved.dataConfigPath } : {},
      startupTimeoutMs: resolved.startupTimeoutMs,
      requestTimeoutMs: resolved.requestTimeoutMs,
      graceMs: resolved.graceMs,
    })
    this.client = makeClient(resolved.stateRoot)
    this.backends.set('default', new BridgeBackend(this.client))
    this.roots.set('default', resolved.stateRoot)
    for (const [key, root] of Object.entries(resolved.extraRoots)) {
      const c = makeClient(root)
      this.backends.set(key, new BridgeBackend(c))
      this.roots.set(key, root)
      ctx.effect(() => () => {
        void c.close()
      })
    }
    ctx.effect(() => () => {
      void this.client.close()
    })
  }

  private readonly d = (): BridgeBackend => this.backends.get('default')!

  /** Multi-ledger routing (2026-09-17): sub-service view for one root key. */
  forRoot(rootKey: string): FactorMiningService {
    const b = this.backends.get(rootKey)
    if (b === undefined) {
      throw new Error(`未知账本 root="${rootKey}"（已配置: ${[...this.backends.keys()].join(', ')}）`)
    }
    return b as unknown as FactorMiningService
  }

  listRoots(): Array<{ key: string; stateRoot: string }> {
    return [...this.roots.entries()].map(([key, stateRoot]) => ({ key, stateRoot }))
  }

  async status(request?: StatusRequest): Promise<FactorMiningStatus> {
    return await this.d().status(request)
  }

  async dataProbe(request: DataProbeRequest): Promise<DataProbeReport> {
    return await this.d().dataProbe(request)
  }

  async dataConfigValidate(config: Record<string, unknown>): Promise<DataConfigReport> {
    return await this.d().dataConfigValidate(config)
  }

  async dataConfigWrite(config: Record<string, unknown>, path?: string): Promise<DataConfigWriteResult> {
    return await this.d().dataConfigWrite(config, path)
  }

  async dataLoad(request: DataLoadRequest): Promise<DataLoadReport> {
    return await this.d().dataLoad(request)
  }

  async checkCausality(request: FactorSourceRequest): Promise<CausalityVerdict> {
    return await this.d().checkCausality(request)
  }

  async evaluate(request: EvaluateRequest): Promise<FactorDiagnosis> {
    return await this.d().evaluate(request)
  }

  async evaluateComposite(request: CompositeRequest): Promise<CompositeDiagnosis> {
    return await this.d().evaluateComposite(request)
  }

  async evaluateBatch(request: BatchRequest): Promise<BatchDiagnosis> {
    return await this.d().evaluateBatch(request)
  }

  async walkForward(request: WalkForwardRequest): Promise<WalkForwardDiagnosis> {
    return await this.d().walkForward(request)
  }

  async noiseTest(request: NoiseTestRequest): Promise<NoiseTestResult> {
    return await this.d().noiseTest(request)
  }

  async dayPermTest(request: DayPermTestRequest): Promise<DayPermTestResult> {
    return await this.d().dayPermTest(request)
  }

  async audit(request: FactorSourceRequest): Promise<AuditReport> {
    return await this.d().audit(request)
  }

  async randomGenerate(request: RandomGenerateRequest): Promise<RandomGenerateResult> {
    return await this.d().randomGenerate(request)
  }

  async operators(request: OperatorsRequest): Promise<OperatorSetResult> {
    return await this.d().operators(request)
  }

  async nullLandscape(request: NullLandscapeRequest): Promise<NullLandscapeResult> {
    return await this.d().nullLandscape(request)
  }

  async stateReset(request: StateResetRequest): Promise<StateResetResult> {
    return await this.d().stateReset(request)
  }

  async trailSummary(request: TrailSummaryRequest): Promise<TrailSummaryResult> {
    return await this.d().trailSummary(request)
  }

  async exportReport(request: ExportReportRequest): Promise<ExportReportResult> {
    return await this.d().exportReport(request)
  }

  async queryLibrary(request: LibraryQueryRequest): Promise<LibraryQueryResult> {
    return await this.d().queryLibrary(request)
  }

  async queryPaths(request: PathQueryRequest): Promise<PathQueryResult> {
    return await this.d().queryPaths(request)
  }

  async appendTrail(entry: Record<string, unknown>, lane?: string): Promise<AppendResult> {
    return await this.d().appendTrail(entry, lane)
  }

  async appendExplored(entry: Record<string, unknown>, lane?: string): Promise<AppendResult> {
    return await this.d().appendExplored(entry, lane)
  }

  async appendSearchPath(entry: Record<string, unknown>, lane?: string): Promise<AppendResult> {
    return await this.d().appendSearchPath(entry, lane)
  }

  async registryGet(envId?: string): Promise<RegistryGetResult> {
    return await this.d().registryGet(envId)
  }

  async registrySubmit(request: RegistrySubmitRequest): Promise<RegistrySubmitResult> {
    return await this.d().registrySubmit(request)
  }

  async registryUpdate(request: RegistryUpdateRequest): Promise<RegistryUpdateResult> {
    return await this.d().registryUpdate(request)
  }

  async journalRead(request: JournalReadRequest): Promise<JournalReadResult> {
    return await this.d().journalRead(request)
  }

  async journalAppend(request: JournalAppendRequest): Promise<JournalAppendResult> {
    return await this.d().journalAppend(request)
  }

  async journalUpdate(request: JournalUpdateRequest): Promise<JournalUpdateResult> {
    return await this.d().journalUpdate(request)
  }

  async journalDistill(request: JournalDistillRequest): Promise<JournalDistillResult> {
    return await this.d().journalDistill(request)
  }

  async journalStats(request: JournalStatsRequest): Promise<JournalStatsResult> {
    return await this.d().journalStats(request)
  }

  async standbyView(request: StandbyViewRequest): Promise<StandbyViewResult> {
    return await this.d().standbyView(request)
  }

  async standbyCombine(request: StandbyCombineRequest): Promise<StandbyCombineResult> {
    return await this.d().standbyCombine(request)
  }

  async incubateEnqueue(request: IncubateEnqueueRequest): Promise<IncubateEnqueueResult> {
    return await this.d().incubateEnqueue(request)
  }

  async incubateStatus(request: IncubateStatusRequest): Promise<IncubateStatusResult> {
    return await this.d().incubateStatus(request)
  }

  async incubateJudge(request: IncubateJudgeRequest): Promise<IncubateJudgeResult> {
    return await this.d().incubateJudge(request)
  }

  async arxivSearch(request: ArxivSearchRequest): Promise<ArxivSearchResult> {
    return await this.d().arxivSearch(request)
  }
}

export default FactorMiningPythonService
