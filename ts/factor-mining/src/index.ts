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
  JournalAppendRequest,
  JournalAppendResult,
  JournalDistillRequest,
  JournalDistillResult,
  JournalReadRequest,
  JournalReadResult,
  JournalStatsRequest,
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
  JournalStatsResult,
  JournalUpdateRequest,
  JournalUpdateResult,
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

  /** Whole-service status and configured environments. `request.lane` scopes
   *  the embedded loop directive to that parallel lane (2026-08-31 方案 A). */
  abstract status(request?: StatusRequest): Promise<FactorMiningStatus>

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

  /** Overfitting hard gate: the factor's direct performance on random-noise worlds. */
  abstract noiseTest(request: NoiseTestRequest): Promise<NoiseTestResult>

  /** Exact temporal-alignment null on real data (report-only diagnostic). */
  abstract dayPermTest(request: DayPermTestRequest): Promise<DayPermTestResult>

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

  /** Append one trail entry. `lane` is the session identity stamped by the
   *  tool layer (parallel-line isolation); it overrides any lane field the
   *  model put inside the entry. */
  abstract appendTrail(entry: Record<string, unknown>, lane?: string): Promise<AppendResult>

  /** Append one falsified-exploration entry (facts layer: reads stay global;
   *  the lane tag is provenance only). */
  abstract appendExplored(entry: Record<string, unknown>, lane?: string): Promise<AppendResult>

  /** Append one process-level search-path entry. */
  abstract appendSearchPath(entry: Record<string, unknown>, lane?: string): Promise<AppendResult>

  /** Read the user registry. */
  abstract registryGet(envId?: string): Promise<RegistryGetResult>

  /** Evaluate and submit one candidate to the user registry. */
  abstract registrySubmit(request: RegistrySubmitRequest): Promise<RegistrySubmitResult>

  /** Update descriptive fields (signal/note) of one registry entry. Iron-rule fields rejected. */
  abstract registryUpdate(request: RegistryUpdateRequest): Promise<RegistryUpdateResult>

  /** Reasoning journal (interpretation layer over the engine fact trail,
   *  per parallel lane): progressive-disclosure read — 0=INDEX, 1=live,
   *  2=one entry by id from live/arcs. */
  abstract journalRead(request: JournalReadRequest): Promise<JournalReadResult>

  /** Append entry block(s) to the lane journal live area. First-line
   *  convention `## <id> <status> <title>`; `registered` requires a
   *  `ref:<source_hash>` line (the evaluate mirror joins on it). */
  abstract journalAppend(request: JournalAppendRequest): Promise<JournalAppendResult>

  /** Status transition / note (open→registered→adjudicated→resolved|stale,
   *  active→retired; a same-status call appends the note). */
  abstract journalUpdate(request: JournalUpdateRequest): Promise<JournalUpdateResult>

  /** Arc-boundary distillation: the engine snapshots the old live into raw/
   *  (audit, anti-gaslight), archives dropped terminal entries with epitaphs,
   *  and auto-keeps dropped active entries. */
  abstract journalDistill(request: JournalDistillRequest): Promise<JournalDistillResult>

  /** Standby-line derived view: near-miss survivors (rescue channels' sourcing
   *  surface — re-entry is mechanically triggered, no agent appeal). */
  abstract standbyView(request: StandbyViewRequest): Promise<StandbyViewResult>

  /** Channel-3: rule-based combination over the standby line (rule registered
   *  as a dof=1 mechanism-layer hypothesis; composite evaluated via the
   *  standard pipeline — returns an async job like evaluate_composite). */
  abstract standbyCombine(request: StandbyCombineRequest): Promise<StandbyCombineResult>

  /** Channel-5: freeze sources and start the incubation window for a cohort. */
  abstract incubateEnqueue(request: IncubateEnqueueRequest): Promise<IncubateEnqueueResult>

  /** Channel-5: cohort listing (progress/judgment go through incubateJudge). */
  abstract incubateStatus(request: IncubateStatusRequest): Promise<IncubateStatusResult>

  /** Channel-5: one-shot cohort judgment on the fresh window (needs new data). */
  abstract incubateJudge(request: IncubateJudgeRequest): Promise<IncubateJudgeResult>

  /** Journal telemetry: entry counts, registration rate, distill stats. */
  abstract journalStats(request: JournalStatsRequest): Promise<JournalStatsResult>

  /** Optional arxiv methodology search. */
  abstract arxivSearch?(request: ArxivSearchRequest): Promise<ArxivSearchResult>

  /** Multi-ledger routing (2026-09-17): return the sub-service bound to the
   *  given root key (its own bridge process and state root — trail/registry/
   *  deflation pool are fully separate). Providers that only mount one state
   *  root leave this unimplemented; callers MUST degrade to `this` then.
   *  The returned object satisfies this interface structurally but is NOT
   *  registered on the context (duplicate-service rule). */
  abstract forRoot?(rootKey: string): FactorMiningService

  /** Configured ledger list for multi-root deployments: the default root
   *  under key "default" plus one entry per extraRoots config key. Single-root
   *  providers return [{ key: 'default', stateRoot }]. */
  abstract listRoots?(): Array<{ key: string; stateRoot: string }>
}
