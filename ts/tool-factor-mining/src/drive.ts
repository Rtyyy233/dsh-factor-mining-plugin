/**
 * Turn-boundary auto-drive injector — the mechanical replacement for the
 * user's 31 manual pushes (session.jsonl forensics, 2026-08-24).
 *
 * When a session that has used `factor_*` tools ends a turn with reason
 * `completed`, and the engine's loop directive still says `running` with a
 * strategy attached, we wait `delayMs` (user decision: 1 minute, giving the
 * human a window to speak first) and then queue a user-role followup
 * carrying the strategy directive, prefixed `[引擎推进]` (auditable
 * provenance) and sourced `{kind:'plugin'}` (honest log identity).
 *
 * Guardrails (user decisions 2026-08-24; 0.1.22 永续化):
 * - the former "max N consecutive simple pushes then yield to the human"
 *   cap is DELETED (it starved undated day mining — the 6th push onward
 *   the injector went silent forever); pushes are uncapped and the
 *   anti-repeat guardrail lives engine-side (same debt pushed 3× without
 *   settlement → escalate to the user AND keep pushing);
 * - a real user message cancels any pending injection and resets the
 *   simple-push counter (and, if the session was wrapped-up/cancelled,
 *   re-takes over — normal auto-drive resumes); a busy agent (new turn
 *   already running or inbox non-empty) is never injected into — **except
 *   past the wedge threshold** (2026-09-19 ETF incident): zero session
 *   events for `wedgeAfterMs` while `running` means the stream died
 *   mid-turn; the drive then cancels the dead turn (keepInbox) and
 *   re-drives on convergence, and a stale parked inbox no longer blocks
 *   injection (idle wake always opens a turn).
 * - same strategy key is never injected twice in a row (engine keys embed
 *   the monotonically growing trial count; `query` keys on accepted count).
 * - active-miner gate (2026-10-16 旁观注入事故): a ledger binding
 *   (factor_root_use) is ROUTING only — it says which bridge a session's
 *   factor_* calls hit, forever. Injection targeting is a separate claim:
 *   at most ONE driven session per root (the "active miner"). A session
 *   acquires the claim by a write-class factor_* tool call (trial pipeline,
 *   registry/journal mutation — reading tools never claim), by arming a
 *   mining deadline, or by preempting a quiet/absent incumbent while the
 *   user is present in it; it keeps the claim alive with any factor_* tool
 *   activity and loses it after CLAIM_TTL_MS of silence, a blocked
 *   (archived) turn, or RESUME_FAIL_DROP unreachable sweeps. The restart
 *   seed is the persisted claim map (~/.dsh/factor-drive-state.json
 *   `miners`) — NOT the session-root bindings file (the 10-16 incident:
 *   19 historical bindings re-seeded the sweep, resurrecting and injecting
 *   16 bystander conversations in one pass).
 *
 * Host-service access (2026-08-24 boot-fatal postmortem): `ctx.agents` /
 * `ctx.sessions` are cordis SERVICES — touching them from a context whose
 * fiber did not declare them throws "cannot get property without inject",
 * and a throw from a timer callback escalates to a fatal plugin load
 * failure. Two hard rules therefore:
 *   1. all agent/session access lives inside `ctx.inject([...], scope)`
 *      — the scope runs only when the host actually provides the services
 *      and disposes with the plugin;
 *   2. every timer/event callback body is wrapped — drive failures log
 *      and degrade, never kill the host.
 *
 * Event/session APIs carry no type declarations in this package (host
 * vocabulary), hence the `any` casts; the goal-round-driver package is the
 * reference implementation of this pattern.
 * @module dsh-tool-factor-mining/drive
 */

import type { Context } from '@deepseek-ai/cordis'
// eslint-disable-next-line @typescript-eslint/consistent-type-imports
import type { FactorMiningService } from '@deepseek-ai/dsh-factor-mining'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { laneOfSession, rootOfSession } from './lane.ts'

/** Injector configuration; every field has a deployment-visible default. */
export interface DriveConfig {
  /** Quiet window between turn end and injection (default 60s). */
  delayMs?: number
  /** Diagnostic cap for the consecutive-`continue` counter (default 5).
   *  Since 0.1.22 this no longer stops injection (perpetual mining; the
   *  anti-repeat guardrail — same debt pushed 3× without settlement →
   *  escalate to the user AND keep pushing — lives engine-side in
   *  primitives.py). */
  maxConsecutiveSimple?: number
  /** Mining deadline (ISO 8601, e.g. "2026-09-18T08:00"). While armed the
   *  deadline REPLACES the consecutive-simple cap as the stop condition; on
   *  crossing, one wrap-up directive is injected and the drive disarms for
   *  that session (a real human message still works — only auto-injection
   *  stops). 方案 B (2026-09-17): continuous mining until a wall-clock. */
  deadline?: string
  /** Fallback sweep interval (ms). Periodically re-runs the injection check
   *  for tracked sessions so a stalled one (no turn/end events reaching us)
   *  still gets re-ignited. **Default ON (5 min) since 2026-09-19**（用户
   *  指令：挖掘不停止）——治理间隔窗的静默会话唯一复活通道是本扫描，
   *  原默认 0（关）的前提"无死线=有人值守"被治理静默窗打破；0 仍可
   *  显式关闭。 */
  wakeIntervalMs?: number
  /** Wedged-turn recovery threshold (ms, default 30 min; 0 disables). A turn
   *  whose stream died mid-generation leaves the agent `running` forever —
   *  both injection paths skip it (event path needs turn/end, sweep respects
   *  busy agents), so the session starves silently (ETF incident 2026-09-19
   *  22:52: 零事件 37min，扫描每 5min 到场却永久跳过). Past this many ms
   *  with zero session events while running, the drive cancels the dead turn
   *  (keepInbox) and re-drives on convergence; a stale parked inbox no longer
   *  blocks injection either (idle wake always opens a turn boundary). */
  wedgeAfterMs?: number
  /** Re-materialize persisted-but-unopened sessions before injecting
   *  (default ON, 2026-10-02 桌面送达事故). On the desktop host a session
   *  that is not open in the UI has no live agent — `agents.get` returns
   *  undefined and every injection path died at the first gate (10-02 实录：
   *  app 重启后两支挖掘会话 14h 零注入，扫描每 5min 撞门). With this on,
   *  the agent-missing path asks the host's sessionController to resume the
   *  session (the same path the UI takes when the user opens a conversation:
   *  preset composition included, concurrent resumes deduped, subagent and
   *  archived sessions rejected) and then runs the standard guard chain.
   *  Throttled to one attempt per wake interval per session. Turn off with
   *  driveAutoResume: false. Host-down (process closed) is still out of
   *  reach by definition. */
  autoResume?: boolean
}

/** Runtime control surface for the deadline (session-level, persisted —
 * 2026-10-02 总任务书 B/C): the model sets/inspects the one-shot mining
 * deadline from inside a session via the factor_drive_deadline tool — the
 * user never edits config files. A deadline is session-scoped ("this
 * conversation mines until HH:MM"; two parallel mining sessions do not
 * share a deadline) and SURVIVES HOST RESTARTS via
 * ~/.dsh/factor-drive-state.json. On restart an already-expired deadline is
 * auto-cancelled (no wrap-up is back-filled, no auto-drive resumes — the
 * session is silently handed back to the user, fileLog
 * `deadline-cancelled-on-restart`). A real human message re-takes over the
 * session (resumes normal auto-drive). */
export interface DriveHandle {
  /** Arm/replace THIS session's deadline (ISO 8601). A fresh deadline
   *  re-arms the session even if it was already wrapped up. Optionally set
   *  the stall-revival sweep interval; defaults to 5 min when unset. */
  setDeadline(iso: string, sessionId: unknown, wakeMs?: number): { ok: boolean; error?: string }
  /** Disarm ALL deadlines and clear ALL wrap-up state (early call-off). */
  clearDeadline(): void
  status(): Record<string, unknown>
}

/** Strategy payload from the engine loop directive (Python owns the shape). */
interface Strategy {
  type: string
  why: string
  key: string
  directive: string
}

/** Simple (momentum-preserving) push types counted by the diagnostic consecutive counter (no longer a cap since 0.1.22). */
const SIMPLE_TYPES = new Set(['continue'])

/** 活跃矿工保活窗口（2026-10-16 矿工门）：持有者超过此时长无任何
 *  factor_* 工具调用即让出矿工身份（root 回到无主，下一位写入者接管）。
 *  被注入器驱动的矿工每轮试验都有工具调用，天然续命；只有真正停摆/被
 *  抢占后沉默的会话才会过期。 */
const CLAIM_TTL_MS = 12 * 3600_000
/** 用户在场窗口：挑战者会话要抢占一个仍在保活期内的现任矿工，必须在
 *  近期内有真实用户消息（用户明确在场换矿工=换会话挖），或设置死线
 *  （显式挖掘意图）。纯模型活动不足以打断现任——否则任何偶然写入的
 *  旁观对话都会抢走驱动权。 */
const PRESENCE_MS = 6 * 3600_000
/** 连续多少轮扫描联系不上会话（agent 缺失且重物化失败/不可用）后放弃
 *  其矿工身份——归档/已删除会话不再每 5 分钟白试一轮（10-16 实录：
 *  resume-fail "session not found" 每 5min 重试无限循环）。 */
const RESUME_FAIL_DROP = 3

/** 观察/设置类工具（2026-10-16 矿工门）：这些调用不取得矿工身份——
 *  在任意对话里查状态/看 trail/读 journal/配环境是合法的旁观行为，
 *  不得因此被注入器接管。清单外的 factor_* 一律视为挖掘写类（缺省
 *  从严取得身份：新挖掘工具漏登清单只会让矿工晚一拍用旧工具续命，
 *  不会让挖掘静默停摆）。 */
const READ_TOOLS = new Set([
  'factor_status', 'factor_root_use', 'factor_drive_deadline',
  'factor_data_probe', 'factor_config_validate', 'factor_config_write',
  'factor_load_env', 'factor_trail_summary', 'factor_export_report',
  'factor_query_library', 'factor_query_paths', 'factor_registry_get',
  'factor_standby_view', 'factor_incubate_status', 'factor_operators',
  'factor_journal_read', 'factor_journal_stats', 'factor_arxiv_search',
])

/** One driven session per root (the active miner). Persisted in
 * ~/.dsh/factor-drive-state.json `miners` — the cross-instance truth the
 * restart seed and the tool-side deadline API both converge on. */
interface MinerClaim {
  sessionId: string
  lastFactorCallAt: number
}

interface DriveState {
  timer: ReturnType<typeof setTimeout> | undefined
  lastKey: string | undefined
  consecutiveSimple: number
  /** Last session/event of any type for this session (ms). Zero events while
   *  `running` is the wedge signature — a live turn emits tool/call, steering
   *  or chunk events continuously. Undefined until the first observation
   *  (restart-safe: no cancel storms on fresh state). */
  lastEventTs: number | undefined
  /** 0b (2026-09-29 delivery-channel hardening): consecutive sweeps where
   *  `agents.get` returned undefined (host recycled the session?) — the
   *  earliest gate used to return silently, the direct blind spot of the
   *  09-29 incident (rotate/deadline injections landing 4h19m late). */
  agentMissing: number
  /** 0b warn throttle (per-session 30min). */
  lastAgentMissingWarnTs: number | undefined
  /** 0c: throttle timestamp of the deadline-undelivered warning. */
  lastDeadlineWarnTs: number | undefined
  /** 0d: when the last injection (strategy directive or deadline wrap-up)
   *  landed — lastKey alone cannot tell "just pushed" from "hours ago". */
  lastStrategyTs: number | undefined
  /** Auto-resume throttle: last re-materialization attempt (ms). One try
   *  per wake interval per session — no resume storms against a host that
   *  keeps rejecting the session (2026-10-02 桌面送达修复). */
  lastResumeAttemptTs: number | undefined
  /** 上下文恢复（2026-10-07）：宿主压缩器刚结束一次压缩（compaction/end）
   *  且尚未随注入送达恢复指令——下一次注入正文前置恢复步骤后清除。
   *  压缩摘要不懂 journal/trail 纪律，模型不知道自己丢了什么工作记忆。 */
  needsContextRecovery: boolean | undefined
  /** 上下文占用遥测（观察期用）：最近一次 assistant/message 的 usage 与
   *  request/context 的窗口大小——注入日志行带 ctx=占用/窗口。 */
  lastInputTokens: number | undefined
  lastCacheReadTokens: number | undefined
  lastCtxWindow: number | undefined
}

/** Short session label for logs/status (ids are UUIDs; tail is enough). */
function idTail(id: unknown): string {
  return typeof id === 'string' ? id.slice(-12) : String(id)
}

function renderThrown(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

/**
 * Host-independent log sink (2026-10-02 桌面盲区修复): ctx.logger goes to
 * host stdout/stderr, which the dshweb wrapper captures on web but the
 * desktop Electron host drops entirely (empty logs dir) — every injector
 * alarm was invisible exactly where mining now runs. This mirror appends
 * the load-bearing drive events to ~/.dsh/factor-drive.log so both hosts
 * share one forensics channel. Fire-and-forget: logging must never throw.
 */
const DRIVE_LOG = path.join(os.homedir(), '.dsh', 'factor-drive.log')
const DRIVE_LOG_MAX = 1_000_000
let driveLogOk = true
/** Boot/instrumentation trace shared with index.ts (same sink). */
export function driveTrace(line: string): void {
  fileLog(line)
}

function fileLog(line: string): void {
  if (!driveLogOk) return
  try {
    try {
      const size = fs.statSync(DRIVE_LOG).size
      if (size > DRIVE_LOG_MAX) fs.renameSync(DRIVE_LOG, `${DRIVE_LOG}.1`)
    } catch { /* first write or stat race — proceed */ }
    fs.appendFileSync(DRIVE_LOG,
      `${new Date().toISOString()} ${line}\n`, 'utf-8')
  } catch {
    driveLogOk = false // read-only home etc.: disable silently, never throw
  }
}

// ---------------------------------------------------------------------------
// 会话级死线状态（模块层，2026-10-07 双实例修复）：桌面部署里工具由预置行
// 实例提供（enableDrive:false——防双注入器的 10-02 归属翻转），而注入器由
// 全局行实例持有。factor_drive_deadline 此前读本实例的 drive 句柄判能力，
// 预置行实例永远报"自动推进未启用"。修复=死线状态本就是文件态
// （~/.dsh/factor-drive-state.json）：工具侧无句柄时直写文件，注入器侧在
// driveNow/扫描入口回读盘同步——文件成为跨实例唯一真源。
// ---------------------------------------------------------------------------
export interface SessionDeadline {
  iso: string; epochMs: number; label: string; setAtMs: number; setBy: string
}
export interface SessionDriveRecord {
  deadline?: SessionDeadline
  finished?: boolean
  finishedHow?: 'wrap-up' | 'restart-cancel' | 'unheld'
}
const DRIVE_STATE_FILE = path.join(os.homedir(), '.dsh', 'factor-drive-state.json')

/** Full on-disk shape (version 2, 2026-10-16 矿工门): per-session deadline
 *  records plus the per-root active-miner claim map. Version-1 files (no
 *  miners) load with an empty claim map — the upgrade path starts cold and
 *  each line's miner re-arms on its next mining write. */
interface DriveFileShape {
  sessions: Record<string, SessionDriveRecord>
  miners: Record<string, MinerClaim>
}

function readDriveFile(): DriveFileShape {
  const empty: DriveFileShape = { sessions: {}, miners: {} }
  try {
    const parsed = JSON.parse(fs.readFileSync(DRIVE_STATE_FILE, 'utf-8'))
    if (parsed === null || typeof parsed !== 'object') return empty
    const out: DriveFileShape = { sessions: {}, miners: {} }
    const sessions = (parsed as { sessions?: Record<string, unknown> })?.sessions
    if (sessions !== null && typeof sessions === 'object') {
      for (const [id, raw] of Object.entries(sessions)) {
        if (raw === null || typeof raw !== 'object') continue
        out.sessions[id] = raw as SessionDriveRecord
      }
    }
    const miners = (parsed as { miners?: Record<string, unknown> })?.miners
    if (miners !== null && typeof miners === 'object') {
      for (const [root, raw] of Object.entries(miners)) {
        if (raw === null || typeof raw !== 'object') continue
        const mc = raw as Partial<MinerClaim>
        if (typeof mc.sessionId === 'string' && typeof mc.lastFactorCallAt === 'number') {
          out.miners[root] = { sessionId: mc.sessionId, lastFactorCallAt: mc.lastFactorCallAt }
        }
      }
    }
    return out
  } catch { /* 缺失/损坏 = 空表 */ return empty }
}

function writeDriveFile(
  sessions: Record<string, SessionDriveRecord>,
  miners: Record<string, MinerClaim>,
): void {
  try {
    fs.mkdirSync(path.dirname(DRIVE_STATE_FILE), { recursive: true })
    const tmp = `${DRIVE_STATE_FILE}.tmp`
    fs.writeFileSync(tmp, JSON.stringify({ version: 2, sessions, miners }, null, 1), 'utf-8')
    fs.renameSync(tmp, DRIVE_STATE_FILE)
  } catch { /* 写失败=仅内存生效 */ }
}

/** 注入器侧盘同步：文件里出现内存没有/不一致的死线或矿工名册（其他
 * 实例的工具直写盘武装的）→ 采纳进内存。finished/wrap-up 以写者为准
 * （写者落盘时已带）。矿工名册：文件侧会话与内存不同才采纳（同会话
 * 时内存的 lastFactorCallAt 更新鲜，保留）。返回是否发生变化（变了由
 * 调用方回写，保持文件与内存收敛）。 */
function syncDeadlinesFromDisk(
  records: Map<string, SessionDriveRecord>,
  miners: Map<string, MinerClaim>,
): boolean {
  let changed = false
  let file: DriveFileShape
  try { file = readDriveFile() } catch { return false }
  for (const [id, frec] of Object.entries(file.sessions)) {
    const dl = frec?.deadline
    if (dl === null || typeof dl !== 'object' || typeof dl.epochMs !== 'number') continue
    const mem = records.get(id)
    if (mem === undefined) {
      records.set(id, frec)
      changed = true
    } else if (mem.deadline?.epochMs !== dl.epochMs
        || Boolean(mem.finished) !== Boolean(frec.finished)) {
      mem.deadline = dl
      mem.finished = frec.finished
      mem.finishedHow = frec.finishedHow
      changed = true
    }
  }
  for (const [root, fc] of Object.entries(file.miners)) {
    const mem = miners.get(root)
    if (mem === undefined || mem.sessionId !== fc.sessionId) {
      miners.set(root, fc)
      changed = true
    }
  }
  return changed
}

/** 工具侧文件直写句柄（无注入器实例用；语义对齐 DriveHandle 的
 *  setDeadline/clearDeadline/status——只在文件上操作，由持有注入器的
 *  实例在 driveNow/扫描入口同步接管）。 */
export interface FileDeadlineApi {
  setDeadline(iso: string, sessionId: unknown,
    wake?: number): { ok: boolean; error?: string }
  clearDeadline(): void
  statusFor(sessionId: unknown): Record<string, unknown> | undefined
}
export function fileDeadlineApi(): FileDeadlineApi {
  return {
    setDeadline(iso: string, sessionId: unknown,
                wake?: number): { ok: boolean; error?: string } {
      const parsed = Date.parse(iso)
      if (Number.isNaN(parsed)) {
        return { ok: false, error: `无法解析时刻: ${iso}（需要 ISO 8601，如 2026-10-07T08:00）` }
      }
      void wake
      const dl: SessionDeadline = { iso, epochMs: parsed, label: iso,
                                    setAtMs: Date.now(), setBy: 'model' }
      const file = readDriveFile()
      if (typeof sessionId === 'string' && sessionId.length > 0) {
        const rec = file.sessions[sessionId] ?? {}
        rec.deadline = dl
        rec.finished = false
        delete rec.finishedHow
        file.sessions[sessionId] = rec
        // 死线=显式挖掘意图（2026-10-16 矿工门）：同时把调用方会话登记为
        // 其账本的活跃矿工（写入名册，注入器实例 syncFromDisk 采纳）。
        const root = rootOfSession(sessionId)
        if (root !== undefined) {
          file.miners[root] = { sessionId, lastFactorCallAt: Date.now() }
          fileLog(`miner-claim-via-file root=${root} ${idTail(sessionId)} (deadline)`)
        }
        fileLog(`deadline-armed-via-file ${idTail(sessionId)} ${iso}`)
      } else {
        for (const rec of Object.values(file.sessions)) {
          rec.deadline = dl
          rec.finished = false
          delete rec.finishedHow
        }
        fileLog(`deadline-armed-via-file all-sessions ${iso}`)
      }
      writeDriveFile(file.sessions, file.miners)
      return { ok: true }
    },
    clearDeadline(): void {
      const file = readDriveFile()
      for (const rec of Object.values(file.sessions)) {
        delete rec.deadline
        rec.finished = false
        delete rec.finishedHow
      }
      writeDriveFile(file.sessions, file.miners)
      fileLog('deadline-cleared-via-file')
    },
    statusFor(sessionId: unknown): Record<string, unknown> | undefined {
      if (typeof sessionId !== 'string') return undefined
      return readDriveFile().sessions[sessionId] as Record<string, unknown> | undefined
    },
  }
}


export function applyDrive(
  ctx: Context,
  service: FactorMiningService,
  config: DriveConfig,
): DriveHandle {
  driveTrace(`applyDrive-entry autoResume=${config.autoResume !== false}`)
  const delayMs = config.delayMs ?? 60_000
  const maxSimple = config.maxConsecutiveSimple ?? 5
  // 2026-09-19 楔死恢复（ETF 事故）：30min 无事件默认值远高于合法静默
  // （单次工具调用上限 900s；连续 decode 30min ≈ 85k token 不可能）。
  const wedgeMs = config.wedgeAfterMs ?? 1_800_000
  // 2026-10-02 桌面送达事故修复：默认开（见 DriveConfig.autoResume 注释）。
  const autoResume = config.autoResume !== false
  let resumeOkCount = 0
  // ---- 会话级死线生命周期（2026-10-02 总任务书 B/C，D1=会话级拍板）----
  // 落盘 ~/.dsh/factor-drive-state.json（模块层 readDriveFile/writeDriveFile，
  // 2026-10-07 双实例修复——文件是跨实例唯一真源，工具侧无句柄时直写盘，
  // 本实例在 driveNow/扫描入口 syncDeadlinesFromDisk 回读）。
  // 重启加载（乙方案）：死线未来→重新武装；已过期→标 finished
  // (restart-cancel)+fileLog，此后一律静默（不注入不收官不推进），用户
  // 真实消息=重新接管授权。到点收官防双发：收官即落盘，重启后已
  // finished 的会话不再发第二次收官注。
  const driveRecords = new Map<string, SessionDriveRecord>()
  /** 活跃矿工名册（2026-10-16 矿工门）：root → 持有会话。注入目标集 =
   *  名册值集；绑定表（lane.ts）只管路由，与本名册解耦。 */
  const holders = new Map<string, MinerClaim>()
  const recOf = (id: unknown): SessionDriveRecord | undefined =>
    (typeof id === 'string' ? driveRecords.get(id) : undefined)
  /** Bounded bookkeeping（对齐 states 的 MAX_TRACKED 纪律）：记录只在设置
   *  新死线时增长，长宿主运行不无界。裁剪跳过仍有未到点死线的记录——
   *  静默解除一个武装死线比留一条旧记录糟糕。 */
  const MAX_DRIVE_RECORDS = 500
  const pruneDriveRecords = (): void => {
    while (driveRecords.size > MAX_DRIVE_RECORDS) {
      const victim = [...driveRecords.entries()].find(([, r]) =>
        r.deadline === undefined || r.deadline.epochMs <= Date.now())
      if (victim === undefined) break // 全是武装死线——不裁（上限是软界）
      driveRecords.delete(victim[0])
    }
  }
  const saveDriveState = (): void => {
    const sessions: Record<string, SessionDriveRecord> = {}
    for (const [k, v] of driveRecords) sessions[k] = v
    const miners: Record<string, MinerClaim> = {}
    for (const [k, v] of holders) miners[k] = v
    writeDriveFile(sessions, miners)
  }
  /** 盘同步（入口统一调用）：别的实例直写盘武装的死线/矿工名册采纳进内存。 */
  const syncFromDisk = (): void => {
    try {
      if (syncDeadlinesFromDisk(driveRecords, holders)) saveDriveState()
    } catch { /* 同步失败=继续用内存态 */ }
  }
  try {
    const boot = readDriveFile()
    let dirty = false
    if (boot !== null && typeof boot === 'object') {
      const nowMs = Date.now()
      for (const [id, raw] of Object.entries(boot.sessions)) {
        if (raw === null || typeof raw !== 'object') continue
        const rec = raw as SessionDriveRecord
        const dl = rec.deadline
        if (dl !== null && typeof dl === 'object' && typeof dl.epochMs === 'number') {
          if (dl.epochMs <= nowMs && !rec.finished) {
            // 乙方案：重启时已过期——不补收官注，标记取消并静默交还用户
            rec.finished = true
            rec.finishedHow = 'restart-cancel'
            dirty = true
            fileLog(`deadline-cancelled-on-restart ${idTail(id)} ${dl.iso}`)
            ctx.logger.info(`factor-mining drive: 会话 ${idTail(id)} 死线 `
              + `${dl.iso} 在停机期间已过期——自动取消（不补收官），静默交还用户`)
          }
          driveRecords.set(id, rec)
        } else if (rec.finished) {
          driveRecords.set(id, rec) // 已收官（wrap-up）防双发：保持 finished
        }
      }
      // 矿工名册加载（2026-10-16 矿工门）：绑定校验（持有者已改绑别的账本
      // = 失效）+ 保活校验（超 CLAIM_TTL_MS 无工具调用 = 过期让位）。
      // 注意：重启种子=本名册，不再从绑定表全量灌入（10-16 旁观注入事故）。
      for (const [root, mc] of Object.entries(boot.miners)) {
        if (rootOfSession(mc.sessionId) !== root) {
          fileLog(`miner-claim-invalid-on-load root=${root} ${idTail(mc.sessionId)}（已改绑）`)
          continue
        }
        if (nowMs - mc.lastFactorCallAt > CLAIM_TTL_MS) {
          fileLog(`miner-claim-expired-on-load root=${root} ${idTail(mc.sessionId)}`)
          continue
        }
        holders.set(root, mc)
      }
    }
    if (dirty) saveDriveState()
    fileLog(`drive-state-loaded sessions=${driveRecords.size} miners=${holders.size}`
      + ` cancelled=${[...driveRecords.values()].filter((r) => r.finishedHow === 'restart-cancel').length}`)
  } catch { /* 缺失/损坏 = 空表起步 */ }
  // 静态配置死线（driveDeadline）＝会话被纳入追踪时的种子模板（宿主级
  // 初始武装的会话级化）；无法解析只告警一次不解体。
  const configDeadline: SessionDeadline | undefined = (() => {
    if (config.deadline === undefined) return undefined
    const parsed = Date.parse(config.deadline)
    if (Number.isNaN(parsed)) {
      ctx.logger.warn(`factor-mining drive: driveDeadline 无法解析 (${config.deadline})——死线不生效`)
      return undefined
    }
    ctx.logger.info(`factor-mining drive: 配置死线模板 ${config.deadline}`
      + '（会话纳入追踪时武装；到点注入收官后解除自动推进）')
    return { iso: config.deadline, epochMs: parsed, label: config.deadline,
             setAtMs: Date.now(), setBy: 'config' }
  })()
  let wakeMs = config.wakeIntervalMs ?? 300_000
  const states = new Map<unknown, DriveState>()
  const timers = new Set<ReturnType<typeof setTimeout>>()
  /** 用户在场时钟（2026-10-16 矿工门）：sessionId → 最近真实用户消息
   *  时刻。抢占在任矿工的资格依据；重启不持久（在场是现场信号）。 */
  const userPresence = new Map<unknown, number>()
  let sweepDisposer: (() => void) | undefined
  // Assigned once the inject scope arms; a no-op until then (drive inert).
  let ensureSweep: () => void = () => {}

  // Bounded bookkeeping (2026-08-25 review; 2026-10-16 矿工门改造): states
  // churns one entry per session that ever held a claim; both maps below are
  // capped FIFO (insertion order preserved, evicting the oldest is O(1)).
  const MAX_TRACKED = 500
  const trimStates = (): void => {
    while (states.size > MAX_TRACKED) {
      const oldest = states.keys().next().value
      if (oldest === undefined) break
      cancelPending(oldest)
      states.delete(oldest)
    }
    while (userPresence.size > MAX_TRACKED) {
      const oldest = userPresence.keys().next().value
      if (oldest === undefined) break
      userPresence.delete(oldest)
    }
  }

  const stateFor = (id: unknown): DriveState => {
    let s = states.get(id)
    if (s === undefined) {
      s = { timer: undefined, lastKey: undefined, consecutiveSimple: 0,
            lastEventTs: undefined, agentMissing: 0,
            lastAgentMissingWarnTs: undefined, lastDeadlineWarnTs: undefined,
            lastStrategyTs: undefined, lastResumeAttemptTs: undefined }
      states.set(id, s)
    }
    return s
  }

  // ---- 矿工门（2026-10-16 旁观注入事故）------------------------------------
  // 注入目标集 = holders 的值集（每 root 至多一个在任矿工）。绑定表只管
  // 工具路由；能否被注入只看名册。取得：写类工具调用/死线武装/在任者
  // 失效或静默时的接管；保持：任意 factor_* 工具调用续活；失去：静默超
  // CLAIM_TTL_MS / 回合被宿主拒（归档）/ 连续 RESUME_FAIL_DROP 轮联系
  // 不上 / 被更具资格者抢占。
  const noteUserPresence = (id: unknown): void => {
    userPresence.set(id, Date.now())
    trimStates()
  }
  const presentRecently = (id: unknown): boolean => {
    const ts = userPresence.get(id)
    return ts !== undefined && Date.now() - ts <= PRESENCE_MS
  }
  const holderRootOf = (id: unknown): string | undefined => {
    for (const [root, claim] of holders) {
      if (claim.sessionId === id) return root
    }
    return undefined
  }
  /** sid 是否某 root 的在任矿工（且名册未因改绑而失效）。 */
  const isHolder = (id: unknown): boolean => {
    const root = holderRootOf(id)
    return root !== undefined && rootOfSession(id) === root
  }
  let lastClaimPersistTs = 0
  /** 在任矿工的工具调用续活（节流落盘：内存时刻始终新鲜，盘上至多滞后
   *  60s——对 12h TTL 无影响）。 */
  const refreshClaimKeepalive = (root: string): void => {
    const claim = holders.get(root)
    if (claim === undefined) return
    claim.lastFactorCallAt = Date.now()
    const now = Date.now()
    if (now - lastClaimPersistTs >= 60_000) {
      lastClaimPersistTs = now
      saveDriveState()
    }
  }
  let lastRejectLogTs = 0
  /** 取得/抢占 root 的矿工身份。返回是否成功；失败=维持现状（在任者
   *  仍被驱动，挑战者保持旁观）。 */
  const claimRootFor = (id: unknown, root: string,
    via: 'write' | 'deadline'): boolean => {
    if (typeof id !== 'string' || id.length === 0) return false
    const now = Date.now()
    const cur = holders.get(root)
    if (cur !== undefined && cur.sessionId === id) {
      refreshClaimKeepalive(root)
      return true
    }
    if (cur !== undefined) {
      const curLive = rootOfSession(cur.sessionId) === root
        && now - cur.lastFactorCallAt <= CLAIM_TTL_MS
      if (curLive && via !== 'deadline' && !presentRecently(id)) {
        // 在任者仍在挖且挑战者无用户在场——拒绝抢占（30min 节流日志）
        if (now - lastRejectLogTs >= 1_800_000) {
          lastRejectLogTs = now
          fileLog(`miner-claim-rejected root=${root} ${idTail(id)}`
            + ` (holder ${idTail(cur.sessionId)} live, challenger absent)`)
        }
        return false
      }
    }
    holders.set(root, { sessionId: id, lastFactorCallAt: now })
    trimStates()
    if (configDeadline !== undefined && !driveRecords.has(id)) {
      driveRecords.set(id, { deadline: configDeadline })
      pruneDriveRecords()
      fileLog(`deadline-seed ${idTail(id)} ${configDeadline.iso}`)
    }
    saveDriveState()
    fileLog(`miner-claim root=${root} ${idTail(id)} via=${via}`
      + `${cur !== undefined ? ` (preempt ${idTail(cur.sessionId)})` : ''}`)
    return true
  }
  /** 让出矿工身份（归档/联系不上/名称失效——非抢占路径）。 */
  const dropClaimFor = (id: unknown, why: string): void => {
    const root = holderRootOf(id)
    if (root === undefined) return
    holders.delete(root)
    saveDriveState()
    fileLog(`miner-claim-dropped root=${root} ${idTail(id)} (${why})`)
  }

  const cancelPending = (id: unknown): void => {
    const s = states.get(id)
    if (s?.timer !== undefined) {
      clearTimeout(s.timer)
      timers.delete(s.timer)
      s.timer = undefined
    }
  }

  // 2026-10-02 桌面送达修复：重物化用的宿主服务句柄。sessionController 由
  // web-app 层提供（桌面/web 宿主同款）——fiber 检查要求动态服务必须经
  // inject 声明后才能访问（ctx.get 直取会抛 "cannot get property without
  // inject"），故用嵌套动态 inject 取句柄；无该服务的宿主（无 web-app 的
  // 裁剪部署）回调永不触发，重物化保持禁用=旧行为，优雅降级。
  let sessionController: { resolveAgent(sessionId: unknown): Promise<any> } | undefined
  try {
    ;(ctx as unknown as Record<string, any>).inject(
      ['sessionController'],
      (sc: Record<string, any>) => {
        sessionController = sc.sessionController
        fileLog('sessionController-acquired')
      })
  } catch { /* hosts without dynamic inject: auto-resume stays disabled */ }

  // Scoped dynamic inject: the scope runs once the host provides the agent
  // and session services, and unwinds with this plugin. Without them the
  // drive simply never arms — tools keep working (graceful degradation).
  ;(ctx as unknown as Record<string, any>).inject(
    ['agents', 'sessions'],
    (scoped: Record<string, any>) => {
      // Review 诊断：确认 scoped 上下文可用 + session/event 能否到达
      driveTrace('inject-scope-armed')
      ctx.logger.info('factor-mining drive: inject scope armed'
        + ` (has agents: ${typeof scoped.agents === 'object'},`
        + ` has sessions: ${typeof scoped.sessions === 'object'})`)
      async function driveNow(sessionId: unknown): Promise<void> {
        // 双实例盘同步（2026-10-07）：别的实例（预置行工具直写盘）武装的
        // 死线在这里被本注入器采纳——事件路径先于扫描周期也能按时收官
        syncFromDisk()
        const state = states.get(sessionId)
        if (state === undefined) return
        state.timer = undefined
        // B/C 合流总规则（引擎 loop 活 ∧ 插件未收官 → 自动推进；任一停
        // → 静默）的插件侧半边：已收官/已取消（restart-cancel）的会话对
        // 注入器一律静默——不注入、不收官、不重物化（乙方案）。
        const rec = recOf(sessionId)
        if (rec?.finished) return
        // 矿工门（2026-10-16 旁观注入事故）：只有本会话是其绑定账本的
        // 在任矿工才继续——不注入、不重物化、不收官。绑定≠矿工：历史上
        // 声明过账本的对话永久保留工具路由，但每 root 只有名册上的一个
        // 会话被驱动（16 旁观会话一轮扫描全量复活的教训）。
        const heldRoot = holderRootOf(sessionId)
        const held = heldRoot !== undefined && rootOfSession(sessionId) === heldRoot
        if (!held) {
          ctx.logger.info(`factor-mining drive: 会话 ${idTail(sessionId)} 非在任矿工`
            + '——不注入（矿工门；名册变更见 factor-drive.log miner-claim）')
          return
        }
        try {
          let agent = scoped.agents?.get?.(sessionId)
          if (agent === undefined && autoResume) {
            // 2026-10-02 桌面送达事故修复：桌面宿主上未在 UI 打开的会话没有
            // 活的 agent（agents.get=undefined）——此前扫描每 5min 撞在这第
            // 一道门上早退，两支挖掘会话 14h 零注入。经宿主的
            // sessionController.resolveAgent 重物化（与用户点开会话同一条
            // 路：preset 组合含工具面、并发去重、子会话/归档会话拒绝），
            // 成功后走标准守卫链。节流：每会话每 wake 间隔至多试一次。
            const nowMs = Date.now()
            if (state.lastResumeAttemptTs === undefined
                || nowMs - state.lastResumeAttemptTs >= wakeMs) {
              state.lastResumeAttemptTs = nowMs
              try {
                const sc = sessionController
                if (sc !== undefined && typeof sc.resolveAgent === 'function') {
                  ctx.logger.info(`factor-mining drive: 会话 ${idTail(sessionId)} `
                    + 'agent 不在——经 sessionController 重物化')
                  const found: any = await sc.resolveAgent(sessionId)
                  if (found !== null && typeof found === 'object' && found.error !== undefined) {
                    fileLog(`resume-fail ${idTail(sessionId)}: ${renderThrown(found.error)}`)
                    ctx.logger.warn(`factor-mining drive: 会话 ${idTail(sessionId)} `
                      + `重物化被拒: ${renderThrown(found.error)}`)
                  } else {
                    agent = found?.agent ?? undefined
                    if (agent !== undefined) {
                      resumeOkCount += 1
                      fileLog(`resume-ok ${idTail(sessionId)} (#${resumeOkCount})`)
                      ctx.logger.info(`factor-mining drive: 会话 ${idTail(sessionId)} `
                        + '重物化成功——继续标准注入链')
                    }
                  }
                }
              } catch (e) {
                fileLog(`resume-error ${idTail(sessionId)}: ${renderThrown(e)}`)
                ctx.logger.warn(`factor-mining drive: 会话 ${idTail(sessionId)} `
                  + `重物化异常: ${renderThrown(e)}`)
              }
            }
          }
          if (agent === undefined) {
            // 0b（2026-09-29 送达通道加固）：最早一道门此前零日志静默
            // return——09-29 悬案（bk 冻结 rotate-timeout:1、收官注迟到
            // 4h19m）的直接盲区。宿主回收会话后 agents.get 返回
            // undefined，事件/扫描两条送达腿都到此为止。计数 + 30min
            // 节流告警："agent 缺失告警有/无"成为判别宿主回收 vs
            // interval 死亡的一锤定音信号。
            state.agentMissing += 1
            // 矿工门（2026-10-16）：连续 RESUME_FAIL_DROP 轮联系不上
            // （归档/已删除会话 resolveAgent 永久拒绝）→ 让出矿工身份，
            // 不再每轮白试（实录：resume-fail "not found" 每 5min 无限
            // 重试）。重新写入工具调用可再取得身份。
            if (state.agentMissing >= RESUME_FAIL_DROP) {
              dropClaimFor(sessionId, `resume-fail x${state.agentMissing}`)
              state.agentMissing = 0
              state.lastAgentMissingWarnTs = undefined
              return
            }
            const nowMs = Date.now()
            if (state.lastAgentMissingWarnTs === undefined
                || nowMs - state.lastAgentMissingWarnTs >= 1_800_000) {
              state.lastAgentMissingWarnTs = nowMs
              ctx.logger.warn(`factor-mining drive: 会话 ${idTail(sessionId)} `
                + 'agent 不在（宿主已回收?）——扫描无法送达'
                + `（累计缺失 ${state.agentMissing} 次）`)
              fileLog(`agent-missing ${idTail(sessionId)} count=${state.agentMissing}`)
            }
            return
          }
          if (state.agentMissing > 0) {
            // 边沿触发（防常驻噪音）：取回成功 = 缺失段闭合
            ctx.logger.info(`factor-mining drive: 会话 ${idTail(sessionId)} `
              + `agent 已取回（此前缺失 ${state.agentMissing} 次）——送达恢复`)
            state.agentMissing = 0
            state.lastAgentMissingWarnTs = undefined
          }
          // 2026-09-19 楔死恢复（ETF 事故诊断）：流死于生成中途时 agent 永远
          // `running`（AgentStatus 仅 idle|running，无流级超时），事件路径等
          // 不到 turn/end、扫描路径尊重忙碌 agent——两会话双哑。楔死签名 =
          // running 且零事件超阈。处置：cancel 中止死回合（keepInbox 保住
          // 未启动的排队工作），whenIdle 收敛后重走本函数走标准注入复活。
          if (wedgeMs > 0 && state.lastEventTs === undefined
              && agent.status === 'running') {
            state.lastEventTs = Date.now() // 首次观察到 running：起钟（重启安全）
          }
          const stale = wedgeMs > 0 && state.lastEventTs !== undefined
            && Date.now() - state.lastEventTs > wedgeMs
          if (agent.status === 'running') {
            if (!stale) return
            const quietMin = Math.round((Date.now() - (state.lastEventTs ?? Date.now())) / 60_000)
            ctx.logger.warn(`factor-mining drive: 会话楔死 ${quietMin}min 零事件仍 running`
              + '——cancel 中止死回合（keepInbox），收敛后重推复活')
            fileLog(`wedge-cancel ${idTail(sessionId)} ${quietMin}min`)
            try {
              agent.cancel({ kind: 'hook', reason: 'factor-mining wedge recovery: dead stream' },
                { keepInbox: true })
            } catch (e) {
              ctx.logger.warn(`factor-mining drive: 楔死 cancel 失败: ${renderThrown(e)}`)
            }
            state.lastEventTs = Date.now() // 防风暴：每 wedgeMs 至多 cancel 一次
            const sid = sessionId
            try {
              void agent.whenIdle?.().then(() => driveNow(sid)).catch(() => {})
            } catch { /* whenIdle 不可用则等下一轮扫描（≤5min）兜底 */ }
            return
          }
          const inbox = agent.inbox
          // 陈旧 parked inbox 不再永久拦截：idle 上的新唤醒必开回合边界
          // （cancel-convergence wake latch 语义），穿透注入可复活会话。
          if (!stale && inbox !== undefined
              && ((Array.isArray(inbox.nextTurn) && inbox.nextTurn.length > 0)
              || (Array.isArray(inbox.nextStep) && inbox.nextStep.length > 0))) {
            return
          }
          // 死线收官（会话级，跨重启持久）：checked after the idle / inbox
          // guards (a busy agent must not be interrupted) but before the
          // engine status query — wrap-up does not depend on the loop state.
          // One-shot per session（收官即落盘——重启后不二次收官）。
          if (rec?.deadline !== undefined && Date.now() >= rec.deadline.epochMs) {
            const deadlineLabel = rec.deadline.label
            try {
              await scoped.sessions?.flush?.(agent.session)
            } catch { /* checkpoint failure is non-fatal for the injection */ }
            // 注入去引擎标识、用户口吻——模型对"引擎消息"打折（吸收不
            // 执行），对用户指令不打折。溯源保留在宿主日志与
            // factor-drive.log，不进模型上下文。
            const text = '挖掘死线已到（'
              + `${deadlineLabel}）。请立即收官，不要再发起新试验：`
              + '汇总本会话的试验数、入册因子数、近失因子（差一点过线者）'
              + '与账本状态，写一份收官战报后结束。'
            let message: any
            try {
              message = {
                id: (globalThis.crypto?.randomUUID?.()
                      ?? `drive-${Date.now()}-${Math.random().toString(36).slice(2)}`),
                role: 'user',
                content: [{ type: 'text', text }],
                source: { kind: 'user' },
              }
            } catch (e) {
              ctx.logger.warn(`factor-mining drive: 收官消息构造失败: ${renderThrown(e)}`)
              return
            }
            agent.followup(message)
            rec.finished = true
            rec.finishedHow = 'wrap-up'
            saveDriveState()
            state.lastKey = 'deadline'
            state.lastStrategyTs = Date.now()
            fileLog(`deadline-wrap ${idTail(sessionId)} ${deadlineLabel}`)
            ctx.logger.info(`factor-mining drive: 死线收官已注入 msg.id=${message.id}`
              + '——本会话自动推进解除，交还用户')
            return
          }
          let loop: any
          try {
            // 2026-08-31 方案 A：status 按本会话的 lane 查——注入器推的
            // 必须是该会话自己的 pending/族链/预算，不是别的并行线的
            // 2026-09-17 双线：会话绑定过账本(factor_root_use)则查该
            // 账本的 loop——stock 会话与 etf 会话各推各的引擎指令
            // 2026-09-19 挖掘不停止：未绑定会话**不注入**——重启后绑定
            // 丢失期曾把个股（默认 root）指令注进 ETF 对话；绑定落盘后
            // 已知会话必有记录，无记录=真未知，宁可沉默也不串线
            const rootKey = rootOfSession(sessionId)
            if (rootKey === undefined) {
              ctx.logger.info('factor-mining drive: 会话无账本绑定（无落盘记录且未 '
                + 'factor_root_use）——不注入，避免错线')
              return
            }
            const svc = service.forRoot !== undefined
              ? (() => {
                  try {
                    return service.forRoot!(rootKey)
                  } catch {
                    return service
                  }
                })()
              : service
            const status = await svc.status({ lane: laneOfSession(sessionId) })
            loop = (status as unknown as Record<string, any>)?.loop
          } catch (e) {
            ctx.logger.warn(`factor-mining drive: status 查询失败（不注入）: ${renderThrown(e)}`)
            return
          }
          const loopState = loop?.state
          if (loopState !== 'running' && loopState !== 'must_rotate') {
            // 永续（2026-10-02 A.3.3）：引擎侧 loop.state 值域已钉死
            // {running, must_rotate}——本检查降级为纯保险丝（值域钉死后
            // 理论不可达；到达即引擎/注入器版本错配，静默并留日志）。
            ctx.logger.info(`factor-mining drive: 引擎判 ${loopState ?? 'unknown'}`
              + `（${loop?.stop_reason ?? '无 loop 指令'}）——超出值域（保险丝`
              + '静默，疑似版本错配）')
            return
          }
          const strategy: Strategy | undefined = loop.strategy
          if (strategy === undefined || typeof strategy.directive !== 'string') return
          const isSimple = SIMPLE_TYPES.has(strategy.type)
          // 2026-09-18 死锁兜底:query(里程碑审计)同 key 也允许限次重推——
          // 引擎侧 query key 曾只含 accepted 数,审计完成后 accepted 不变 →
          // key 冻结 → sameKey 去重永久拦截(实测 stock 账本 36 只恰为 3 的
          // 倍数,双线夜挖 stock 会话停摆)。引擎已正修(key 并入 agent_rounds),
          // 此处为旧引擎/AI 不写 trail 的残余场景兜底,与 simple 共用计数。
          const repeatable = isSimple || strategy.type === 'query'
          const sameKey = strategy.key !== undefined && strategy.key === state.lastKey
          // 同 key 去重只约束复杂类型（防无状态变化的无限循环）；可重推型
          // （continue/query）允许原地重推——用户 31 次 push 中的高频模式。
          // 0.1.22（2026-10-02 总任务书 A.3.4）：删除「连续 N 次可重推注入
          // 达上限→暂停等待真人」分支——现行活体违反永续（专咬无死线日间
          // 挖掘：第 6 注起注入器静默，会话停摆）。防复读护栏移交引擎侧
          // （primitives：同一欠账推 3 次无消账→升级告警用户+继续推不停推）。
          // consecutiveSimple 仅保留为日志/诊断计数。
          if (sameKey && !repeatable) return
          // Durability checkpoint before driving (goal-round-driver discipline).
          try {
            await scoped.sessions?.flush?.(agent.session)
          } catch { /* checkpoint failure is non-fatal for the injection */ }
          // 2026-09-19 用户决策：注入正文 = 指令本身——无 [引擎推进] 前缀、
          // 无依据尾注、source 标 user（不标注不是用户消息）。理由：模型
          // 已实测把带引擎标识的消息当可吸收的机器通知（等待环吸收
          // rotate 指令），用户口吻不打折。why/键等遥测保留在 loop 响应
          // 与宿主日志，不进注入正文。
          // 2026-10-07 压缩后恢复：宿主压缩器刚替换过早期历史时，指令正文
          // 前置恢复步骤（一次性；引擎账本全在磁盘上，恢复成本低）。
          const recover = state.needsContextRecovery === true
          state.needsContextRecovery = false
          const text = recover
            ? ('本会话的早期对话刚被压缩为摘要，工作细节可能丢失。'
              + '在执行下面的指令前，先恢复工作状态：调用 factor_trail_summary '
              + '查看本线的评估历史与在途作业，调用 factor_journal_read（level=1）'
              + '重新装载推理日志；引擎的全部账本都在磁盘上，试验与入册不受'
              + '压缩影响。恢复后立即继续挖掘内循环。\n\n' + strategy.directive)
            : strategy.directive
          // 2026-08-25 崩溃修复（review 修订）：不用 createUserMessage
          // （版本不匹配风险）也不 Object.freeze——DSH followup 内部可能
          // 需要往消息上附加字段（序列号/时间戳），freeze 在 ES module
          // 严格模式下会 throw TypeError。手动构造但保持可变。
          let message: any
          try {
            message = {
              id: (globalThis.crypto?.randomUUID?.()
                    ?? `drive-${Date.now()}-${Math.random().toString(36).slice(2)}`),
              role: 'user',
              content: [{ type: 'text', text }],
              source: { kind: 'user' },
            }
          } catch (e) {
            ctx.logger.warn(`factor-mining drive: 消息构造失败: ${renderThrown(e)}`)
            return
          }
          agent.followup(message)
          state.lastKey = strategy.key
          state.lastStrategyTs = Date.now()
          state.consecutiveSimple = repeatable ? state.consecutiveSimple + 1 : 0
          // 占用遥测（2026-10-07 观察期）：注入时刻的上下文占用估计
          const ctxEst = state.lastInputTokens !== undefined
            ? `${state.lastInputTokens + (state.lastCacheReadTokens ?? 0)}`
              + (state.lastCtxWindow !== undefined ? `/${state.lastCtxWindow}` : '')
            : 'n/a'
          fileLog(`inject ${idTail(sessionId)} type=${strategy.type} key=${strategy.key ?? 'no-key'}`
            + ` simple=${state.consecutiveSimple}/${maxSimple} ctx=${ctxEst}`
            + `${recover ? ' +ctx-recovery' : ''} msg=${message.id}`)
          ctx.logger.info(`factor-mining drive: 注入 ${strategy.type}`
            + ` (${strategy.key ?? 'no-key'}, 连续简单 ${state.consecutiveSimple}/${maxSimple})`
            + ` msg.id=${message.id}`)
        } catch (e) {
          // Never let a drive failure escape into the host (2026-08-24
          // boot-fatal postmortem): log and degrade.
          // 2026-08-25 review: 附完整 stack 以定位 "reading 'kind'" 类
          // 崩溃的确切位置（此前只有 message 无法区分是 drive 内还是
          // followup 内部异步抛出）
          const stack = e instanceof Error ? (e.stack ?? e.message) : String(e)
          ctx.logger.warn(`factor-mining drive: 注入流程异常（降级跳过）: ${stack}`)
        }
      }

      // Host session events; payload shape mirrors dsh-session's
      // SessionEventMap. Errors here must never propagate either.
      // 2026-10-16 矿工门：重启种子=落盘矿工名册（boot 已加载）——绑定表
      // 不再灌入扫描视野（此前 19 条历史绑定=16 旁观会话一轮全量复活）。
      // 未绑定会话照旧不注入（0.1.16 首调门）；绑定≠矿工，写入类工具
      // 调用才取得身份。
      driveTrace(`seed-loaded miners=${holders.size}`)
      scoped.on('session/event',
        (session: { id: unknown }, event: { type: string; data: any }) => {
          try {
            const data = event.data ?? {}
            // 活性时钟（楔死恢复）：任何类型的会话事件都证明宿主↔会话链路
            // 活着。只记已有 state 的会话（=在册矿工会话），不给全宿主
            // 会话白建 state 条目。
            const s = states.get(session.id)
            if (s !== undefined) s.lastEventTs = Date.now()
            if (event.type === 'tool/call'
                && typeof data.name === 'string' && data.name.startsWith('factor_')) {
              // 矿工门（2026-10-16）：在任者的任何 factor_* 调用续活；
              // 非在任者只有写类调用可取得/接管身份（观察类查询在任意
              // 对话里都合法，不得因此被驱动）。
              const root = rootOfSession(session.id)
              if (root !== undefined) {
                const heldRoot = holderRootOf(session.id)
                if (heldRoot === root) {
                  refreshClaimKeepalive(root)
                } else if (!READ_TOOLS.has(data.name)) {
                  claimRootFor(session.id, root, 'write')
                }
              }
              return
            }
            if (event.type === 'user/message' && data.source?.kind === 'user') {
              // Real human spoke: yield the window and reset the streak.
              noteUserPresence(session.id)
              cancelPending(session.id)
              const us = states.get(session.id)
              if (us !== undefined) us.consecutiveSimple = 0
              // D2（2026-10-02 总任务书）：一条真实用户消息=重新接管授权
              // ——收官/重启取消后的静默解除，恢复常规推进（否则每次收官
              // 都要手动 clear）。死线未到点则保持武装；已过期的死线一并
              // 解除（否则接管后下一轮扫描立刻再收官=死循环；要新死线
              // 重新 setDeadline）。
              const urec = recOf(session.id)
              if (urec?.finished) {
                urec.finished = false
                delete urec.finishedHow
                if (urec.deadline !== undefined
                    && Date.now() >= urec.deadline.epochMs) {
                  delete urec.deadline
                }
                saveDriveState()
                fileLog(`user-resume ${idTail(session.id)}（用户真实消息=重新接管，恢复常规推进）`)
              }
              return
            }
            if (event.type === 'turn/start') {
              cancelPending(session.id)
              return
            }
            // ---- 上下文占用遥测与压缩感知（2026-10-07，基座 compaction 已
            // 默认自动压缩；这三类事件都在 session/event 总线上）----
            if (event.type === 'request/context') {
              if (s !== undefined && typeof data.contextWindow === 'number') {
                s.lastCtxWindow = data.contextWindow
              }
              return
            }
            if (event.type === 'assistant/message') {
              if (s !== undefined && data.usage !== null && typeof data.usage === 'object') {
                if (typeof data.usage.inputTokens === 'number') {
                  s.lastInputTokens = data.usage.inputTokens
                }
                if (typeof data.usage.cacheReadTokens === 'number') {
                  s.lastCacheReadTokens = data.usage.cacheReadTokens
                }
              }
              return
            }
            if (event.type === 'compaction/start' || event.type === 'compaction/end') {
              if (isHolder(session.id)) {
                const st = stateFor(session.id)
                const ctxDesc = s !== undefined && s.lastCtxWindow !== undefined
                  && s.lastInputTokens !== undefined
                  ? ` ctx=${s.lastInputTokens + (s.lastCacheReadTokens ?? 0)}/${s.lastCtxWindow}`
                  : ''
                fileLog(`${event.type} ${idTail(session.id)}${ctxDesc}`)
                if (event.type === 'compaction/end') {
                  // 压缩后恢复注入：摘要不懂 journal/trail 纪律——下一次
                  // 注入前置恢复步骤。压缩多发生在空闲窗口，turn/end 早已
                  // 消费：无 pending 定时器时主动排一发（同 turn/end 路径
                  // 的 delayMs 节奏），防"压缩完没人推"。
                  st.needsContextRecovery = true
                  if (st.timer === undefined) {
                    const t = setTimeout(() => {
                      timers.delete(t)
                      void driveNow(session.id)
                    }, delayMs)
                    timers.add(t)
                    st.timer = t
                  }
                }
              }
              return
            }
            if (event.type !== 'turn/end') return
            cancelPending(session.id)
            // 2026-10-02：blocked 回合 = 宿主门禁拒绝（实测：归档会话的重
            // 物化链路里 resolveAgent 不查归档，注入送达后回合被 archived-
            // session-gate 拒）。用户归档=主动退役，让出矿工身份停止自动
            // 推进，防每个扫描周期白做一轮重物化+注入。
            if (data.reason?.kind === 'blocked' && (isHolder(session.id) || states.has(session.id))) {
              dropClaimFor(session.id, 'turn-blocked')
              states.delete(session.id)
              fileLog(`untracked-blocked ${idTail(session.id)}（宿主拒回合——归档/门禁，停止自动推进）`)
              return
            }
            if (data.reason?.kind !== 'completed') return
            // 矿工门：只有在任矿工的回合边界武装注入定时器。
            if (!isHolder(session.id)) return
            if (typeof data.turn !== 'number') return
            const sessionId = session.id
            const t = setTimeout(() => {
              timers.delete(t)
              void driveNow(sessionId)
            }, delayMs)
            timers.add(t)
            stateFor(sessionId).timer = t
          } catch (e) {
            ctx.logger.warn(`factor-mining drive: 事件处理异常: ${renderThrown(e)}`)
          }
        })

      // 方案 B (2026-09-17): stall-revival sweep, armed lazily — by an initial
      // wakeIntervalMs config OR by a runtime setDeadline (default 5 min).
      // Event-driven injection only fires on turn/end — a session that
      // stalls without emitting events would never be re-ignited. The sweep
      // periodically re-runs the guarded driveNow for tracked sessions;
      // pending turn/end timers are respected (skip while set).
      ensureSweep = (): void => {
        if (sweepDisposer !== undefined || wakeMs <= 0) return
        try {
          // 2026-10-02 修复：原用 ctx.interval（timer 服务）——但本插件 inject
          // 只声明 tools/factorMining，fiber 检查抛 "cannot get property
          // "timer" without inject"，扫描从未武装成功（catch 分支此前零
          // 可见日志=死 armor；事故复盘：51c 会话中止后无回合事件，唯一复
          // 活通道正是本扫描）。改用原生 setInterval + unref + 作用域清理，
          // 去掉 timer 服务依赖。
          const sweep = setInterval(() => {
            syncFromDisk()
            // 名册校验（矿工门，快照遍历——driveNow 异步让位会改表）：
            // 持有者已改绑别的账本=失效；超 CLAIM_TTL_MS 无工具调用=
            // 过期让位。两者都把 root 交回无主，下一位写入者可接管。
            for (const [root, claim] of [...holders]) {
              if (rootOfSession(claim.sessionId) !== root) {
                holders.delete(root)
                saveDriveState()
                fileLog(`miner-claim-invalid root=${root} ${idTail(claim.sessionId)}（已改绑）`)
                continue
              }
              if (Date.now() - claim.lastFactorCallAt > CLAIM_TTL_MS) {
                holders.delete(root)
                saveDriveState()
                fileLog(`miner-claim-expired root=${root} ${idTail(claim.sessionId)}（静默超保活）`)
                continue
              }
            }
            // 在任矿工巡检（原扫描语义不变，目标集从绑定表换成名册）
            for (const claim of [...holders.values()]) {
              const id = claim.sessionId
              const s = stateFor(id)
              // 0c（2026-09-29 送达通道加固；0.1.22 会话级化）：死线已过但
              // 收官注未落 → 30min 节流必告警，独立于 driveNow 的一切早退
              // 形态（agent 缺失/楔死等待收敛/inbox 受阻/无绑定静默）。
              // "死线必达"在插件面内的现实形态 = 必告警（送达保证需要宿主
              // 配合）；下一夜验收：收官注按时到，或 factor-drive.log 出现
              // 本告警——不允许第三种沉默。
              const drec = recOf(id)
              if (drec?.deadline !== undefined && !drec.finished
                  && Date.now() >= drec.deadline.epochMs) {
                const nowMs = Date.now()
                const lateMin = Math.round((nowMs - drec.deadline.epochMs) / 60_000)
                if (s.lastDeadlineWarnTs === undefined
                    || nowMs - s.lastDeadlineWarnTs >= 1_800_000) {
                  s.lastDeadlineWarnTs = nowMs
                  ctx.logger.warn(`factor-mining drive: 死线已过 ${lateMin}min，`
                    + `会话 ${idTail(id)} 未收到收官注`
                    + '（agent 缺失/楔死/inbox 受阻）')
                }
              }
              if (s.timer !== undefined) continue
              void driveNow(id)
            }
            // 非在任矿工的已过点死线：静默收官（矿工门——收官注只发给
            // 在任者；身份已被抢占/让位的会话无事可收官，注入它=注入
            // 旁观对话）。
            let unheldDirty = false
            for (const [id, rec] of driveRecords) {
              if (rec.deadline !== undefined && !rec.finished
                  && Date.now() >= rec.deadline.epochMs && !isHolder(id)) {
                rec.finished = true
                rec.finishedHow = 'unheld'
                unheldDirty = true
                fileLog(`deadline-unheld-finished ${idTail(id)} ${rec.deadline.iso}`)
              }
            }
            if (unheldDirty) saveDriveState()
          }, wakeMs)
          ;(sweep as unknown as { unref?: () => void }).unref?.()
          sweepDisposer = () => clearInterval(sweep)
          const armedN = [...driveRecords.values()]
            .filter((r) => r.deadline !== undefined && !r.finished).length
          ctx.logger.info(`factor-mining drive: 停摆复活扫描已武装 (每 ${wakeMs}ms`
            + `${armedN > 0 ? `，武装死线会话 ${armedN} 个` : ''})`)
          fileLog(`sweep-armed interval=${wakeMs}ms autoResume=${autoResume}`
            + ` armed=${armedN}`)
        } catch (e) {
          sweepDisposer = undefined
          fileLog(`sweep-arm-failed: ${renderThrown(e)}`)
          ctx.logger.warn(`factor-mining drive: 停摆复活扫描不可用（timer 服务缺失?）: ${renderThrown(e)}`)
        }
      }
      if (wakeMs > 0) ensureSweep()
    })

  const handle: DriveHandle = {
    setDeadline(iso: string, sessionId: unknown,
                wake?: number): { ok: boolean; error?: string } {
      const parsed = Date.parse(iso)
      if (Number.isNaN(parsed)) {
        return { ok: false, error: `无法解析时刻: ${iso}（需要 ISO 8601，如 2026-09-18T08:00）` }
      }
      if (typeof wake === 'number' && wake > 0) wakeMs = wake
      if (wakeMs <= 0) wakeMs = 300_000
      const dl = { iso, epochMs: parsed, label: iso,
                   setAtMs: Date.now(), setBy: 'model' }
      if (typeof sessionId === 'string' && sessionId.length > 0) {
        // D1 会话级：只武装调用方会话（"这个会话挖到八点"；双线不连坐）；
        // 新死线重置该会话收官态（fresh deadline re-arms）
        const rec = driveRecords.get(sessionId) ?? {}
        rec.deadline = dl
        rec.finished = false
        delete rec.finishedHow
        driveRecords.set(sessionId, rec)
        pruneDriveRecords()
        // 矿工门（2026-10-16）：死线=显式挖掘意图——调用方会话取得其
        // 账本的矿工身份（可抢占仍在任的旧会话：换会话挖就是换矿工）。
        const droot = rootOfSession(sessionId)
        if (droot !== undefined) claimRootFor(sessionId, droot, 'deadline')
        ctx.logger.info(`factor-mining drive: 会话 ${idTail(sessionId)} 死线已设置 `
          + `${iso}（跨重启持久；停摆复活扫描 ${wakeMs}ms）`)
      } else {
        // 无会话身份（回放/工具上下文）：等价宿主级——武装全部已追踪会话
        for (const rec of driveRecords.values()) {
          rec.deadline = dl
          rec.finished = false
          delete rec.finishedHow
        }
        ctx.logger.info(`factor-mining drive: 死线已设置 ${iso}`
          + `（无会话身份→武装全部已追踪会话；停摆复活扫描 ${wakeMs}ms）`)
      }
      saveDriveState()
      ensureSweep()
      return { ok: true }
    },
    clearDeadline(): void {
      // B.1：clear = 清死线 + 清全部 finished + 落盘（恢复常规推进）
      for (const rec of driveRecords.values()) {
        delete rec.deadline
        rec.finished = false
        delete rec.finishedHow
      }
      saveDriveState()
      ctx.logger.info('factor-mining drive: 死线已由会话清除——全部会话恢复常规自动推进')
    },
      status(): Record<string, unknown> {
        const now = Date.now()
        const wedged = [...states.entries()]
          .filter(([, s]) => s.lastEventTs !== undefined
            && wedgeMs > 0 && now - s.lastEventTs > wedgeMs)
          .map(([id]) => idTail(id))
        // 0d（2026-09-29 送达通道加固）：诊断面扩展——死线迟到/静默类
        // 悬案不再需要直读引擎 bk。undeliverable = agentMissing>0 的会话
        // （扫描唯一送达腿对它们失效）；per-session 活性/注入年龄让
        // "距上次注入多久/距上次事件多久"一眼可读。
        const sessions = [...states.entries()].map(([id, s]) => {
          const rec = recOf(id)
          return {
            session: idTail(id),
            lastEventAgeMs: s.lastEventTs !== undefined ? now - s.lastEventTs : null,
            lastStrategyKey: s.lastKey ?? null,
            lastStrategyAgeMs: s.lastStrategyTs !== undefined
              ? now - s.lastStrategyTs : null,
            agentMissing: s.agentMissing,
            deadline: rec?.deadline?.iso ?? null,
            finished: rec?.finished ?? false,
            finishedHow: rec?.finishedHow ?? null,
          }
        })
        const undeliverable = sessions
          .filter((x) => x.agentMissing > 0).map((x) => x.session)
        const deadlines = [...driveRecords.entries()]
          .filter(([, r]) => r.deadline !== undefined)
          .map(([id, r]) => ({
            session: idTail(id), iso: r.deadline!.iso,
            epochMs: r.deadline!.epochMs, setBy: r.deadline!.setBy,
            finished: !!r.finished, finishedHow: r.finishedHow ?? null,
          }))
        const armed = deadlines.filter((d) => !d.finished && d.epochMs > now)
        const wrapped = deadlines.filter((d) => d.finishedHow === 'wrap-up')
        const cancelled = deadlines.filter(
          (d) => d.finishedHow === 'restart-cancel' || d.finishedHow === 'unheld')
        const miners = [...holders.entries()].map(([root, claim]) => ({
          root,
          session: idTail(claim.sessionId),
          lastCallAgeMs: now - claim.lastFactorCallAt,
        }))
        return {
          deadlinePolicy: 'session-level-persisted',
          stateFile: DRIVE_STATE_FILE,
          deadlines,
          armedDeadlines: armed.length,
          wrapUpInjectedSessions: wrapped.length,
          restartCancelledSessions: cancelled.length,
          wakeIntervalMs: wakeMs > 0 ? wakeMs : null,
          wedgeAfterMs: wedgeMs > 0 ? wedgeMs : null,
          autoResume,
          resumeOkCount,
          wedgedSessions: wedged,
          undeliverableSessions: undeliverable,
          sessions,
          activeMiners: miners,
          trackedSessions: holders.size,
          note: ('死线=会话级且跨重启持久（~/.dsh/factor-drive-state.json）：'
            + '到点注入一次收官后该会话解除自动推进；重启时已过期的死线'
            + '自动取消并静默交还用户（不补收官）；收官/取消后用户真实'
            + '消息=重新接管授权，恢复常规推进；clear 清全部死线与收官态。'
            + '注入目标=矿工名册（每账本至多一个在任矿工，写类工具调用/'
            + '死线武装/用户在场接管取得，12h 无工具调用让位）；账本绑定'
            + '只管工具路由，不再使会话被注入'),
        }
      },
  }
  // 清理注册（2026-10-16 修复：原写在本函数 return handle 之后=永不执行
  // 的死代码，定时器/扫描随插件卸载泄漏）。必须在 return 之前注册。
  ctx.effect(() => () => {
    for (const t of timers) clearTimeout(t)
    timers.clear()
    states.clear()
    holders.clear()
    userPresence.clear()
    sweepDisposer?.()
  })
  return handle
}
