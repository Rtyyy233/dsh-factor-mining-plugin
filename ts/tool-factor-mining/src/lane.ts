/**
 * Parallel-lane identity for the shared-bridge deployment (2026-08-31 方案 A).
 *
 * One dsh web host owns ONE persistent Python bridge process serving every
 * session's `factor_*` calls. The engine's intent-layer state (pending
 * hypothesis obligation, family chain, direction budget) used to assume a
 * single narrator, so parallel sessions hijacked each other's "next step".
 * Lane = session-stable agent id (Agent.id is the SessionId shared with the
 * session), stamped onto lane-sensitive requests by the tool layer; the
 * Python side keeps pricing counters global (multi-testing must count every
 * lane's trials).
 *
 * Identity comes from infrastructure only — a lane field the model writes
 * inside an entry is overridden, never trusted.
 * @module dsh-tool-factor-mining/lane
 */

/** Shape of the second `execute` argument (ToolRunContext extends
 * ToolExecution which carries the calling agent); host vocabulary, hence
 * the local structural type. */
interface ToolExecLike {
  agent?: { id?: unknown }
}

/**
 * Sanitize a session id into a lane id (`[A-Za-z0-9_-]`, ≤48 chars — dsh
 * session ids are UUIDs). Returns undefined when no agent identity is
 * available (e.g. replay/tooling contexts): the bridge then falls back to
 * its process default, which is correct for single-lane usage.
 */
export function laneOfExec(exec: unknown): string | undefined {
  const agent = (exec as ToolExecLike | undefined)?.agent
  const id = agent?.id
  if (typeof id !== 'string' || id.length === 0) return undefined
  const sanitized = id.replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 48).replace(/^-+|-+$/g, '')
  return sanitized.length > 0 ? sanitized : undefined
}

/** Same sanitation for a raw session id (drive injector path). */
export function laneOfSession(sessionId: unknown): string | undefined {
  if (typeof sessionId !== 'string' || sessionId.length === 0) return undefined
  const sanitized = sessionId.replace(/[^A-Za-z0-9_-]+/g, '-').slice(0, 48).replace(/^-+|-+$/g, '')
  return sanitized.length > 0 ? sanitized : undefined
}

/**
 * Stable journal line for the shared-bridge DSH deployment (2026-09-12).
 * Session lanes are UUIDs (intent-layer isolation), which would give every
 * conversation a fresh reasoning journal. When the host config sets
 * `journalLane`, the tool layer stamps it onto journal calls and evaluations
 * — one journal line per stateRoot, cross-session belief inheritance.
 * Identity comes from infrastructure config only; a model-written
 * journal_lane field is never forwarded.
 */
export function journalLaneOfConfig(config: unknown): string | undefined {
  const raw = (config as { journalLane?: unknown } | undefined)?.journalLane
  if (typeof raw !== 'string' || raw.length === 0) return undefined
  const sanitized = raw.replace(/[^A-Za-z0-9_.-]+/g, '-').slice(0, 48).replace(/^-+|-+$/g, '')
  return sanitized.length > 0 ? sanitized : undefined
}

/**
 * Session → ledger (root key) binding (2026-09-17 dual-line): one session,
 * one ledger — mirroring the direct-drive discipline of one lane per line.
 * The model establishes the binding explicitly via the factor_root_use tool
 * ("switch to the ETF ledger"); every subsequent factor_* call from that
 * session is routed to that root's bridge. Without a binding, calls go to
 * the default root. Bounded like the drive bookkeeping to avoid unbounded
 * growth over a long host run.
 */
const MAX_BOUND_SESSIONS = 500
const sessionRootBindings = new Map<unknown, string>()

export function bindSessionRoot(sessionId: unknown, rootKey: string): void {
  if (sessionRootBindings.size >= MAX_BOUND_SESSIONS && !sessionRootBindings.has(sessionId)) {
    const oldest = sessionRootBindings.keys().next().value
    if (oldest !== undefined) sessionRootBindings.delete(oldest)
  }
  sessionRootBindings.set(sessionId, rootKey)
}

export function rootOfSession(sessionId: unknown): string | undefined {
  return sessionRootBindings.get(sessionId)
}

/** Tool-exec flavor: the agent id IS the session id (same as laneOfExec). */
export function rootOfExec(exec: unknown): string | undefined {
  const id = (exec as ToolExecLike | undefined)?.agent?.id
  return id === undefined ? undefined : rootOfSession(id)
}
