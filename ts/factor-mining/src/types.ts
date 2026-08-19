/**
 * Vocabulary for the factor-mining capability seam.  Diagnostic payloads are
 * deliberately kept as lossless JSON because the Python engine owns their
 * evolving detailed shape; the seam pins only the envelope fields a caller
 * needs for control flow.
 * @module dsh-factor-mining/types
 */

/** Lossless JSON value accepted and returned across the Python bridge. */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue | undefined }

/** Record that may carry arbitrary JSON fields (Python owns the full shape). */
export type JsonRecord = { [key: string]: JsonValue | undefined }

/** Common identity of one configured data environment. */
export interface EnvironmentDescriptor extends JsonRecord {
  id: string
  label: string
  kind: 'panel' | 'minute_features'
  layout: 'long' | 'wide' | 'per_symbol' | 'multiindex'
  configured: boolean
}

/** Whole-service status surfaced by `factor_status`. */
export interface FactorMiningStatus extends JsonRecord {
  ready: boolean
  version?: string
  schemaVersion?: number
  stateRoot?: string
  dataConfigPath?: string | null
  dataConfigured?: boolean
  environments?: EnvironmentDescriptor[]
  libraryConfigured?: boolean
  loadedEnvironments?: string[]
}

/** Result of probing one user data file (report + suggested column mapping). */
export interface DataProbeReport extends JsonRecord {
  ok?: boolean
  path?: string
  format?: string
  rows?: number
  columns?: string[]
  suggested_mapping?: JsonRecord
  /** Ambiguity facts (ambiguous / missing required columns, date-format hints).
   * The AGENT turns these into ask_user_question prompts per SKILL.md — the probe
   * itself never blocks on user input. */
  ambiguous?: JsonRecord
  missing_required?: string[]
}

/** Result of validating a data configuration without writing it. */
export interface DataConfigReport extends JsonRecord {
  ok?: boolean
  errors?: { environment?: string; error?: string }[]
  environments?: string[]
}

/** Result of writing a user data configuration. */
export interface DataConfigWriteResult extends JsonRecord {
  ok?: boolean
  path?: string
}

/** Report of a loaded data environment's dimensions. */
export interface DataLoadReport extends JsonRecord {
  ok?: boolean
  envId?: string
  kind?: 'panel' | 'minute_features'
  T?: number
  N?: number
  dates?: [string, string]
  symbols?: number
  rows?: number
  columns?: string[]
  hasAmount?: boolean
  hasListed?: boolean
}

/** Causality verdict from the noise-perturbation / NaN-truncation checks. */
export interface CausalityVerdict extends JsonRecord {
  verdict?: 'causal' | 'FUTURE_LEAK'
  causal?: boolean
  leak_t0?: number | null
  note?: string
}

/** Column-permutation null-test result. */
export interface ColumnPermResult extends JsonRecord {
  z?: number | null
  p?: number | null
  null_mean?: number | null
}

/** Diagnostic envelope for one factor evaluation (development/selection/test). */
export interface FactorDiagnosis extends JsonRecord {
  ic_mean_train?: number | null
  ic_ir_train?: number | null
  ic_n_train?: number
  beta_exposure?: number | null
  column_perm_train?: ColumnPermResult
  yearly_consistency_train?: JsonRecord
  rolling_ic_stability_train?: JsonRecord
  decay_train?: JsonRecord
  topn?: JsonRecord | null
}

/** Onion-style composite evaluation result. */
export interface CompositeDiagnosis extends JsonRecord {
  composite?: FactorDiagnosis
  parts?: JsonRecord
  onion?: JsonRecord
  diagnosis?: JsonRecord
}

/** Batch evaluation with multiple-testing deflation. */
export interface BatchDiagnosis extends JsonRecord {
  factors?: JsonRecord
  batch?: JsonRecord
}

/** Time-ordered walk-forward block robustness. */
export interface WalkForwardDiagnosis extends JsonRecord {
  n_folds?: number
  per_fold?: JsonValue[]
  fold_consistency?: number
  overall_ic_mean?: number
  sign?: number
}

/** Independent two-implementation audit report. */
export interface AuditReport extends JsonRecord {
  verdict?: 'PASS' | 'FAIL'
  discrepancies?: string[]
  causality?: string
  audit_ic_mean_train?: number | null
  audit_topn_net?: number | null
  audit_colperm_z?: number | null
  audit_beta?: number | null
}

/** Result of a user library query (empty hits when unconfigured). */
export interface LibraryQueryResult extends JsonRecord {
  configured?: boolean
  hits?: JsonValue[]
  note?: string
}

/** Result of querying explored/search-path state. */
export interface PathQueryResult extends JsonRecord {
  layer?: string
  hits?: JsonValue[]
}

/** Result of appending a trail/explored/search-path entry. */
export interface AppendResult extends JsonRecord {
  kind?: string
  index?: number
  path?: string
}

/** Registry contents read from user state. */
export interface RegistryGetResult extends JsonRecord {
  registry?: JsonValue[]
}

/** Result of submitting one candidate to the user registry. */
export interface RegistrySubmitResult extends JsonRecord {
  accepted?: boolean
  reason?: string
  entry?: JsonRecord
}

/** Arxiv methodology-search result. */
export interface ArxivSearchResult extends JsonRecord {
  entries?: JsonValue[]
}

/** Request shape for probing one user data file. */
export interface DataProbeRequest {
  path: string
  layout?: string
  dateFormat?: string
}

/** Request shape for loading a configured data environment. */
export interface DataLoadRequest {
  envId?: string
}

/** Request carrying one user factor source snippet. */
export interface FactorSourceRequest {
  envId?: string
  source: string
}

/** Development-stage selector. */
export type EvaluationStage = 'development' | 'selection' | 'test'

/** Request shape for factor evaluation. */
export interface EvaluateRequest extends FactorSourceRequest {
  stage?: EvaluationStage
}

/** Request shape for composite evaluation. */
export interface CompositeRequest extends FactorSourceRequest {
  ingredients?: Record<string, string>
}

/** Request shape for batch evaluation. */
export interface BatchRequest {
  envId?: string
  sources: Record<string, string>
}

/** Request shape for walk-forward evaluation. */
export interface WalkForwardRequest extends FactorSourceRequest {
  n_folds?: number
  t0_date?: string
  t1_date?: string
}

/** Library query request. */
export interface LibraryQueryRequest {
  query: string
  top_k?: number
}

/** Path-query layer names. */
export type PathLayer = 'explored' | 'search_paths'

/** Path query request. */
export interface PathQueryRequest {
  layer: PathLayer
  query: string
  top_k?: number
}

/** Registry submit request. */
export interface RegistrySubmitRequest extends FactorSourceRequest {
  name?: string
  signal?: string
  /** Full factor_evaluate diagnosis object. Required by the bridge: receipt verification,
   * iron-rule duplicate check and red_flags gate all run against it. Pass the evaluate
   * result verbatim — fabricated numbers are downgraded to verified:false. */
  diagnosis?: JsonRecord
}

/** Registry update request — descriptive fields only (signal/note).
 * source/diagnosis/numbers/verdict are iron-rule territory: changing the source means
 * a NEW factor (go through registry_submit); fabricating numbers is rejected outright. */
export interface RegistryUpdateRequest {
  name: string
  signal?: string
  note?: string
}

/** Result of updating one registry entry. */
export interface RegistryUpdateResult extends JsonRecord {
  ok?: boolean
  changed?: string[]
}

/** Arxiv methodology-search request. */
export interface ArxivSearchRequest {
  query: string
  max_results?: number
  category?: string
}

/** Random-factor generation request (seed generator). */
export interface RandomGenerateRequest {
  envId?: string
  /** explore (default): generate + light-IC scan + top_k sources; null-calibration: empirical null landscape. */
  mode?: 'explore' | 'null-calibration'
  /** number of random factors to generate (default 50). */
  n?: number
  /** reproducibility seed (default 42). */
  seed?: number
  /** explore only: return top_k sources by |IC_IR| (default 5). */
  top_k?: number
}

/** One light-IC diagnostic over a generated random factor. */
export interface LightIcDiagnosis extends JsonRecord {
  ic_mean?: number | null
  ic_ir?: number | null
  n?: number
  error?: string
}

/** One surviving random factor in explore mode. */
export interface RandomFactorCandidate extends JsonRecord {
  index?: number
  expression?: string
  light_ic?: LightIcDiagnosis
  source?: string
  note?: string
}

/** Random generation result (explore or null-calibration). */
export interface RandomGenerateResult extends JsonRecord {
  mode?: string
  n?: number
  seed?: number
  top?: RandomFactorCandidate[]
  abs_ic_ir_distribution?: JsonRecord
  null_hint?: string
  n_generated?: number
  n_valid?: number
  ic_ir?: JsonRecord
  interpretation?: string
}

/** Operator-set get/set request. */
export interface OperatorsRequest {
  /** get (default): effective set; set: write override {disable?, windows?, delays?}. */
  action?: 'get' | 'set'
  override?: JsonRecord
}

/** Effective operator set. */
export interface OperatorSetResult extends JsonRecord {
  ops?: JsonRecord
  windows?: number[]
  delays?: number[]
  disabled?: string[]
}

/** Null-landscape query request (no parameters; the landscape is pool-level). */
export interface NullLandscapeRequest {
  envId?: string
}

/** Null-landscape query result. */
export interface NullLandscapeResult extends JsonRecord {
  calibrated?: boolean
  hint?: string
  ic_ir?: JsonRecord
  interpretation?: string
}

/** State-reset scope selector. */
export type StateResetScope = 'mining' | 'landscape' | 'registry' | 'config' | 'all'

/** State-reset request (files are backed up to stateRoot/backups/ before deletion). */
export interface StateResetRequest {
  scope?: StateResetScope
}

/** State-reset result. */
export interface StateResetResult extends JsonRecord {
  ok?: boolean
  scope?: string
  removed?: string[]
  backup?: string | null
  note?: string
}

/** Trail aggregate summary request (no parameters; merges engine + agent trails). */
export interface TrailSummaryRequest {
  envId?: string
}

/** Trail aggregate summary result. */
export interface TrailSummaryResult extends JsonRecord {
  generated_at?: string
  mining?: JsonRecord
  termination?: JsonRecord
  evaluations?: JsonRecord
  agent_rounds?: number
  explored_count?: number
  red_flagged?: JsonValue[]
  pool?: JsonRecord | null
  last_engine_trail?: JsonValue[]
}

/** Result-card export request (assembles from the engine trail; zero recompute). */
export interface ExportReportRequest {
  envId?: string
  source: string
  name?: string
}

/** Result-card export result (markdown written under stateRoot/reports/). */
export interface ExportReportResult extends JsonRecord {
  ok?: boolean
  path?: string
  content?: string
  error?: string
}
