/**
 * Local Service Provider for the factor-mining capability seam.  It owns one
 * persistent Python JSON-RPC bridge process and translates the service
 * envelope into bridge methods.  User data and user state never enter the
 * TypeScript process; the Python package reads/writes them directly.
 * @module @deepseek-ai/dsh-factor-mining-python
 */

import { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import { FactorMiningService } from '@deepseek-ai/dsh-factor-mining'
import type {
  ArxivSearchRequest,
  ArxivSearchResult,
  AppendResult,
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
  dataConfigPath?: string
  startupTimeoutMs: number
  requestTimeoutMs: number
  graceMs: number
}

function resolveConfig(raw: Config): ResolvedConfig {
  return {
    pythonExecutable: raw.pythonExecutable ?? 'python',
    bridgeModule: raw.bridgeModule ?? 'dsh_factor_mining.bridge',
    bridgeCwd: raw.bridgeCwd ?? process.cwd(),
    stateRoot: raw.stateRoot ?? `${process.cwd()}/.factor-mining`,
    ...raw.dataConfigPath !== undefined ? { dataConfigPath: raw.dataConfigPath } : {},
    startupTimeoutMs: raw.startupTimeoutMs ?? 30_000,
    requestTimeoutMs: raw.requestTimeoutMs ?? 360_000,
    graceMs: raw.graceMs ?? 5_000,
  }
}

/**
 * Python subprocess provider for `ctx.factorMining`.
 */
export class FactorMiningPythonService extends FactorMiningService {
  static inject = ['subprocess']

  readonly client: JsonRpcClient

  constructor(ctx: Context, config: Config) {
    super(ctx)
    const resolved = resolveConfig(config)
    const subprocess = ctx.subprocess as SubprocessRuntime
    this.client = new JsonRpcClient(subprocess, {
      pythonExecutable: resolved.pythonExecutable,
      bridgeModule: resolved.bridgeModule,
      bridgeCwd: resolved.bridgeCwd,
      stateRoot: resolved.stateRoot,
      ...resolved.dataConfigPath !== undefined ? { dataConfigPath: resolved.dataConfigPath } : {},
      startupTimeoutMs: resolved.startupTimeoutMs,
      requestTimeoutMs: resolved.requestTimeoutMs,
      graceMs: resolved.graceMs,
    })
    ctx.effect(() => () => {
      void this.client.close()
    })
  }

  async status(): Promise<FactorMiningStatus> {
    return await this.client.request('status') as FactorMiningStatus
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

  async appendTrail(entry: Record<string, unknown>): Promise<AppendResult> {
    return await this.client.request('paths.append', { layer: 'trail', entry }) as AppendResult
  }

  async appendExplored(entry: Record<string, unknown>): Promise<AppendResult> {
    return await this.client.request('paths.append', { layer: 'explored', entry }) as AppendResult
  }

  async appendSearchPath(entry: Record<string, unknown>): Promise<AppendResult> {
    return await this.client.request('paths.append', { layer: 'search_paths', entry }) as AppendResult
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

  async arxivSearch(request: ArxivSearchRequest): Promise<ArxivSearchResult> {
    return await this.client.request('arxiv.search', request as unknown as Record<string, unknown>) as ArxivSearchResult
  }
}

export default FactorMiningPythonService
