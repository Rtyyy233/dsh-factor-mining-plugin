/**
 * Service Definition for the `ctx.factorMining` capability seam: a
 * persistent Python factor-mining engine behind a small typed request
 * envelope.  The Python provider owns data loading, evaluation, audit, user
 * factor libraries, and user state; this seam deliberately pins only the
 * control-flow envelope and leaves diagnostic payloads as lossless JSON.
 * @module @deepseek-ai/dsh-factor-mining
 */

import { Context, Service } from '@deepseek-ai/cordis'
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
  EvaluateRequest,
  FactorDiagnosis,
  FactorMiningStatus,
  FactorSourceRequest,
  LibraryQueryRequest,
  LibraryQueryResult,
  NullLandscapeRequest,
  NullLandscapeResult,
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
} from './types.ts'

export type * from './types.ts'

declare module '@deepseek-ai/cordis' {
  interface Context {
    factorMining: FactorMiningService
  }
}

/**
 * Factor-mining service.  One provider implementation is mounted per
 * context; a second registration throws (Cordis' duplicate-service rule).
 */
export abstract class FactorMiningService extends Service {
  constructor(ctx: Context) {
    super(ctx, 'factorMining')
  }

  /** Whole-service status and configured environments. */
  abstract status(): Promise<FactorMiningStatus>

  /** Probe one user data file and suggest a column mapping. */
  abstract dataProbe(request: DataProbeRequest): Promise<DataProbeReport>

  /** Validate a data configuration without writing it. */
  abstract dataConfigValidate(config: Record<string, unknown>): Promise<DataConfigReport>

  /** Write a user-confirmed data configuration to the state root. */
  abstract dataConfigWrite(config: Record<string, unknown>, path?: string): Promise<DataConfigWriteResult>

  /** Load (or reuse the cached) data environment and report its dimensions. */
  abstract dataLoad(request: DataLoadRequest): Promise<DataLoadReport>

  /** Noise-perturbation causality check for one factor source. */
  abstract checkCausality(request: FactorSourceRequest): Promise<CausalityVerdict>

  /** Evaluate one factor source in development/selection/test. */
  abstract evaluate(request: EvaluateRequest): Promise<FactorDiagnosis>

  /** Onion-style composite evaluation. */
  abstract evaluateComposite(request: CompositeRequest): Promise<CompositeDiagnosis>

  /** Batch evaluation with multiple-testing deflation. */
  abstract evaluateBatch(request: BatchRequest): Promise<BatchDiagnosis>

  /** Time-ordered walk-forward block robustness. */
  abstract walkForward(request: WalkForwardRequest): Promise<WalkForwardDiagnosis>

  /** Independent two-implementation audit. */
  abstract audit(request: FactorSourceRequest): Promise<AuditReport>

  /** Random factor seed generation (explore top-k sources, or null landscape calibration). */
  abstract randomGenerate(request: RandomGenerateRequest): Promise<RandomGenerateResult>

  /** View or configure the effective operator set for the seed generator. */
  abstract operators(request: OperatorsRequest): Promise<OperatorSetResult>

  /** Query the persisted random null landscape (pool difficulty calibration). */
  abstract nullLandscape(request: NullLandscapeRequest): Promise<NullLandscapeResult>

  /** Reset plugin state by scope (files are backed up to stateRoot/backups/ first; test_lock is never reset). */
  abstract stateReset(request: StateResetRequest): Promise<StateResetResult>

  /** Aggregate trail summary: engine trail (hard facts, auto-recorded) + agent trail + mining state. */
  abstract trailSummary(request: TrailSummaryRequest): Promise<TrailSummaryResult>

  /** Export a shareable markdown result card for one factor (assembled from the engine trail). */
  abstract exportReport(request: ExportReportRequest): Promise<ExportReportResult>

  /** Query the user factor library (empty when unconfigured). */
  abstract queryLibrary(request: LibraryQueryRequest): Promise<LibraryQueryResult>

  /** Query user explored/search path state. */
  abstract queryPaths(request: PathQueryRequest): Promise<PathQueryResult>

  /** Append one trail entry. */
  abstract appendTrail(entry: Record<string, unknown>): Promise<AppendResult>

  /** Append one falsified-exploration entry. */
  abstract appendExplored(entry: Record<string, unknown>): Promise<AppendResult>

  /** Append one process-level search-path entry. */
  abstract appendSearchPath(entry: Record<string, unknown>): Promise<AppendResult>

  /** Read the user registry. */
  abstract registryGet(envId?: string): Promise<RegistryGetResult>

  /** Evaluate and submit one candidate to the user registry. */
  abstract registrySubmit(request: RegistrySubmitRequest): Promise<RegistrySubmitResult>

  /** Update descriptive fields (signal/note) of one registry entry. Iron-rule fields rejected. */
  abstract registryUpdate(request: RegistryUpdateRequest): Promise<RegistryUpdateResult>

  /** Optional arxiv methodology search. */
  abstract arxivSearch?(request: ArxivSearchRequest): Promise<ArxivSearchResult>
}
