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
 *
 * @module dsh-tool-factor-mining/lane
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

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

// ---------------------------------------------------------------------------
// 绑定持久化（2026-09-19 用户指令：挖掘不停止——重启后路由必须恢复）。
// 落盘 ~/.dsh/factor-session-bindings.json（host 级文件，会话→root 键值表，
// 工具包自持——不经 bridge RPC，省 seam/provider 两包改动）。失败容忍：
// 读失败=空表起步（行为退回旧版）；写失败=仅内存生效。
// ---------------------------------------------------------------------------
const BINDINGS_FILE = path.join(os.homedir(), '.dsh', 'factor-session-bindings.json')

function loadPersistedBindings(): Map<string, string> {
  const out = new Map<string, string>()
  try {
    const parsed = JSON.parse(fs.readFileSync(BINDINGS_FILE, 'utf-8'))
    if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
      for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof k === 'string' && typeof v === 'string') out.set(k, v)
      }
    }
  } catch { /* 缺失/损坏 = 空表起步 */ }
  return out
}

try {
  for (const [k, v] of loadPersistedBindings()) sessionRootBindings.set(k, v)
} catch { /* 种子失败 = 空表起步 */ }

function persistBindings(): void {
  try {
    fs.mkdirSync(path.dirname(BINDINGS_FILE), { recursive: true })
    const obj: Record<string, string> = {}
    for (const [k, v] of sessionRootBindings) {
      if (typeof k === 'string') obj[k] = v
    }
    const tmp = `${BINDINGS_FILE}.tmp`
    fs.writeFileSync(tmp, JSON.stringify(obj), 'utf-8')
    fs.renameSync(tmp, BINDINGS_FILE)
  } catch { /* 写失败 = 仅内存生效 */ }
}

/** 已知挖掘会话名单（落盘绑定的键集）——drive 用它重启后恢复扫描视野。 */
export function boundSessionIds(): string[] {
  const out: string[] = []
  for (const k of sessionRootBindings.keys()) {
    if (typeof k === 'string') out.push(k)
  }
  return out
}

export function bindSessionRoot(sessionId: unknown, rootKey: string): void {
  if (sessionRootBindings.size >= MAX_BOUND_SESSIONS && !sessionRootBindings.has(sessionId)) {
    const oldest = sessionRootBindings.keys().next().value
    if (oldest !== undefined) sessionRootBindings.delete(oldest)
  }
  sessionRootBindings.set(sessionId, rootKey)
  persistBindings()
}

export function rootOfSession(sessionId: unknown): string | undefined {
  return sessionRootBindings.get(sessionId)
}

/** Tool-exec flavor: the agent id IS the session id (same as laneOfExec). */
export function rootOfExec(exec: unknown): string | undefined {
  const id = (exec as ToolExecLike | undefined)?.agent?.id
  return id === undefined ? undefined : rootOfSession(id)
}
