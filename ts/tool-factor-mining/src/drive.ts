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
 * Guardrails (user decisions 2026-08-24):
 * - simple pushes (`continue`) max N consecutive injections (default 5);
 *   complex pushes are uncapped — the engine's mechanical stops bound them.
 * - a real user message cancels any pending injection and resets the
 *   simple-push counter; a busy agent (new turn already running or inbox
 *   non-empty) is never injected into.
 * - same strategy key is never injected twice in a row (engine keys embed
 *   the monotonically growing trial count; `query` keys on accepted count).
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
import { laneOfSession, rootOfSession } from './lane.ts'

/** Injector configuration; every field has a deployment-visible default. */
export interface DriveConfig {
  /** Quiet window between turn end and injection (default 60s). */
  delayMs?: number
  /** Max consecutive `continue` injections before yielding to the human. */
  maxConsecutiveSimple?: number
  /** Mining deadline (ISO 8601, e.g. "2026-09-18T08:00"). While armed the
   *  deadline REPLACES the consecutive-simple cap as the stop condition; on
   *  crossing, one wrap-up directive is injected and the drive disarms for
   *  that session (a real human message still works — only auto-injection
   *  stops). 方案 B (2026-09-17): continuous mining until a wall-clock. */
  deadline?: string
  /** Fallback sweep interval (ms). Periodically re-runs the injection check
   *  for tracked sessions so a stalled one (no turn/end events reaching us)
   *  still gets re-ignited. 0 disables the sweep (default). */
  wakeIntervalMs?: number
}

/** Runtime control surface for the deadline (2026-09-17 方案 B 追加):
 * the model sets/inspects the one-shot mining deadline from inside a session
 * via the factor_drive_deadline tool — the user never edits config files.
 * A deadline is ephemeral: it fires once (wrap-up injection + disarm) and
 * does not survive a host restart. */
export interface DriveHandle {
  /** Arm/replace the deadline (ISO 8601). Resets per-session wrap-up state
   *  (a fresh deadline re-arms sessions already wrapped up). Optionally set
   *  the fallback sweep interval; defaults to 5 min when unset. */
  setDeadline(iso: string, wakeMs?: number): { ok: boolean; error?: string }
  /** Disarm the deadline and re-arm normal injection (early call-off). */
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

/** Simple (momentum-preserving) push types subject to the consecutive cap. */
const SIMPLE_TYPES = new Set(['continue'])

interface DriveState {
  timer: ReturnType<typeof setTimeout> | undefined
  lastKey: string | undefined
  consecutiveSimple: number
  /** Deadline wrap-up already injected — stay disarmed for this session. */
  finished: boolean
}

function renderThrown(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

export function applyDrive(
  ctx: Context,
  service: FactorMiningService,
  config: DriveConfig,
): DriveHandle {
  const delayMs = config.delayMs ?? 60_000
  const maxSimple = config.maxConsecutiveSimple ?? 5
  // 方案 B (2026-09-17): optional wall-clock deadline. Mutable at runtime via
  // the DriveHandle (factor_drive_deadline tool) — the static config value
  // is only the initial arm; malformed initial values log once and disarm.
  let deadlineMs: number | undefined = (() => {
    if (config.deadline === undefined) return undefined
    const parsed = Date.parse(config.deadline)
    if (Number.isNaN(parsed)) {
      ctx.logger.warn(`factor-mining drive: driveDeadline 无法解析 (${config.deadline})——死线不生效`)
      return undefined
    }
    ctx.logger.info(`factor-mining drive: 死线已武装 ${config.deadline}`
      + '（连续简单上限让位，到点注入收官后解除自动推进）')
    return parsed
  })()
  let deadlineLabel: string | undefined = config.deadline
  let wakeMs = config.wakeIntervalMs ?? 0
  /** Sessions that called a factor_* tool in this host run (mining sessions). */
  const factorSessions = new Set<unknown>()
  const states = new Map<unknown, DriveState>()
  const timers = new Set<ReturnType<typeof setTimeout>>()
  let sweepDisposer: (() => void) | undefined
  // Assigned once the inject scope arms; a no-op until then (drive inert).
  let ensureSweep: () => void = () => {}

  // Bounded bookkeeping (2026-08-25 review): both containers otherwise grow
  // unbounded over a long host run (one entry per session ever seen). Insertion
  // order is preserved, so evicting the oldest entry is O(1).
  const MAX_TRACKED = 500
  const trimSessions = (): void => {
    if (factorSessions.size <= MAX_TRACKED) return
    const oldest = factorSessions.values().next().value
    if (oldest !== undefined) factorSessions.delete(oldest)
    if (states.size > MAX_TRACKED) {
      const oldestState = states.keys().next().value
      if (oldestState !== undefined && oldestState !== oldest) {
        cancelPending(oldestState)
        states.delete(oldestState)
      }
    }
  }

  const stateFor = (id: unknown): DriveState => {
    let s = states.get(id)
    if (s === undefined) {
      s = { timer: undefined, lastKey: undefined, consecutiveSimple: 0, finished: false }
      states.set(id, s)
    }
    return s
  }

  const cancelPending = (id: unknown): void => {
    const s = states.get(id)
    if (s?.timer !== undefined) {
      clearTimeout(s.timer)
      timers.delete(s.timer)
      s.timer = undefined
    }
  }

  // Scoped dynamic inject: the scope runs once the host provides the agent
  // and session services, and unwinds with this plugin. Without them the
  // drive simply never arms — tools keep working (graceful degradation).
  ;(ctx as unknown as Record<string, any>).inject(
    ['agents', 'sessions'],
    (scoped: Record<string, any>) => {
      // Review 诊断：确认 scoped 上下文可用 + session/event 能否到达
      ctx.logger.info('factor-mining drive: inject scope armed'
        + ` (has agents: ${typeof scoped.agents === 'object'},`
        + ` has sessions: ${typeof scoped.sessions === 'object'})`)
      async function driveNow(sessionId: unknown): Promise<void> {
        const state = states.get(sessionId)
        if (state === undefined) return
        state.timer = undefined
        try {
          const agent = scoped.agents?.get?.(sessionId)
          if (agent === undefined) return
          if (agent.status !== 'idle') return
          const inbox = agent.inbox
          if (inbox !== undefined
              && ((Array.isArray(inbox.nextTurn) && inbox.nextTurn.length > 0)
              || (Array.isArray(inbox.nextStep) && inbox.nextStep.length > 0))) {
            return
          }
          // 方案 B (2026-09-17): deadline wrap-up. Checked after the idle /
          // inbox guards (a busy agent must not be interrupted) but before
          // the engine status query — wrap-up does not depend on the loop
          // state. One-shot per session, then permanently disarmed.
          if (deadlineMs !== undefined) {
            if (state.finished) return
            if (Date.now() >= deadlineMs) {
              try {
                await scoped.sessions?.flush?.(agent.session)
              } catch { /* checkpoint failure is non-fatal for the injection */ }
              const text = '[引擎推进][deadline] 挖掘死线已到（'
                + `${deadlineLabel}）。请立即收官，不要再发起新试验：`
                + '汇总本会话的试验数/入册/near-miss 与账本状态，'
                + '写一份收官战报后结束。'
              let message: any
              try {
                message = {
                  id: (globalThis.crypto?.randomUUID?.()
                        ?? `drive-${Date.now()}-${Math.random().toString(36).slice(2)}`),
                  role: 'user',
                  content: [{ type: 'text', text }],
                  source: { kind: 'plugin', plugin: 'dsh-tool-factor-mining/drive' },
                }
              } catch (e) {
                ctx.logger.warn(`factor-mining drive: 收官消息构造失败: ${renderThrown(e)}`)
                return
              }
              agent.followup(message)
              state.finished = true
              state.lastKey = 'deadline'
              ctx.logger.info(`factor-mining drive: 死线收官已注入 msg.id=${message.id}`
                + '——本会话自动推进解除，交还用户')
              return
            }
          }
          let loop: any
          try {
            // 2026-08-31 方案 A：status 按本会话的 lane 查——注入器推的
            // 必须是该会话自己的 pending/族链/预算，不是别的并行线的
            // 2026-09-17 双线：会话绑定过账本(factor_root_use)则查该
            // 账本的 loop——stock 会话与 etf 会话各推各的引擎指令
            const rootKey = rootOfSession(sessionId)
            const svc = rootKey !== undefined && service.forRoot !== undefined
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
            // 2026-08-24 可观测性：静默必须有原因日志——用户测试时注入器
            // 正确静默（机械停点已触发）但完全不可见，排查只能靠读 state
            // 2026-08-25 arc 化（v7）：direction_budget 停点（must_rotate）
            // 照常注入——rotate 指令驱动换向，断链后预算自动重置回
            // running；静默终态仅 convergence/finalize/fail_streak
            // （IC 全局收敛=换方向救不了，交还用户做数据集轮换）
            ctx.logger.info(`factor-mining drive: 引擎判 ${loopState ?? 'unknown'}`
              + `（${loop?.stop_reason ?? '无 loop 指令'}）——注入器静默，`
              + '该停点换方向无法解除，交还用户')
            return
          }
          const strategy: Strategy | undefined = loop.strategy
          if (strategy === undefined || typeof strategy.directive !== 'string') return
          const isSimple = SIMPLE_TYPES.has(strategy.type)
          const sameKey = strategy.key !== undefined && strategy.key === state.lastKey
          // 同 key 去重只约束复杂类型（防无状态变化的无限循环）；简单类型
          // （continue）允许原地重推——用户 31 次 push 中的高频模式——
          // 但受连续上限约束（用户决策 2：简单 push ≤5 连续）。
          if (sameKey && !isSimple) return
          if (isSimple && state.consecutiveSimple >= maxSimple) {
            // 方案 B (2026-09-17): an armed deadline REPLACES the simple cap
            // as the stop condition — without one the original fuse holds.
            if (deadlineMs === undefined) {
              ctx.logger.info(`factor-mining drive: 连续简单注入达上限 ${maxSimple}，`
                + '暂停自动推进，等待真人指示')
              return
            }
          }
          // Durability checkpoint before driving (goal-round-driver discipline).
          try {
            await scoped.sessions?.flush?.(agent.session)
          } catch { /* checkpoint failure is non-fatal for the injection */ }
          const text = `[引擎推进][${strategy.type}] ${strategy.directive}\n`
            + `（依据：${strategy.why}。本消息由因子挖掘引擎自动注入；`
            + '停点仅由引擎机械判据触发，请继续内循环，不要停下来等待指示。）'
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
              source: { kind: 'plugin', plugin: 'dsh-tool-factor-mining/drive' },
            }
          } catch (e) {
            ctx.logger.warn(`factor-mining drive: 消息构造失败: ${renderThrown(e)}`)
            return
          }
          agent.followup(message)
          state.lastKey = strategy.key
          state.consecutiveSimple = isSimple ? state.consecutiveSimple + 1 : 0
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
      scoped.on('session/event',
        (session: { id: unknown }, event: { type: string; data: any }) => {
          try {
            const data = event.data ?? {}
            if (event.type === 'tool/call'
              && typeof data.name === 'string' && data.name.startsWith('factor_')) {
              factorSessions.add(session.id)
              trimSessions()
              return
            }
            if (event.type === 'user/message' && data.source?.kind === 'user') {
              // Real human spoke: yield the window and reset the streak.
              cancelPending(session.id)
              stateFor(session.id).consecutiveSimple = 0
              return
            }
            if (event.type === 'turn/start') {
              cancelPending(session.id)
              return
            }
            if (event.type !== 'turn/end') return
            cancelPending(session.id)
            if (data.reason?.kind !== 'completed') return
            if (!factorSessions.has(session.id)) return
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

      // 方案 B (2026-09-17): fallback sweep, armed lazily — by an initial
      // wakeIntervalMs config OR by a runtime setDeadline (default 5 min).
      // Event-driven injection only fires on turn/end — a session that
      // stalls without emitting events would never be re-ignited. The sweep
      // periodically re-runs the guarded driveNow for tracked sessions;
      // pending turn/end timers are respected (skip while set).
      ensureSweep = (): void => {
        if (sweepDisposer !== undefined || wakeMs <= 0) return
        try {
          const sweep = (ctx as unknown as Record<string, any>).interval(() => {
            for (const id of factorSessions) {
              const s = states.get(id)
              if (s === undefined || s.timer !== undefined) continue
              void driveNow(id)
            }
          }, wakeMs)
          sweepDisposer = typeof sweep?.dispose === 'function' ? () => sweep.dispose() : () => {}
          ctx.logger.info(`factor-mining drive: 兜底唤醒已武装 (每 ${wakeMs}ms`
            + `${deadlineMs !== undefined ? `，死线 ${deadlineLabel}` : ''})`)
        } catch (e) {
          sweepDisposer = undefined
          ctx.logger.warn(`factor-mining drive: 兜底唤醒不可用（timer 服务缺失?）: ${renderThrown(e)}`)
        }
      }
      if (wakeMs > 0) ensureSweep()
    })

  const handle: DriveHandle = {
    setDeadline(iso: string, wake?: number): { ok: boolean; error?: string } {
      const parsed = Date.parse(iso)
      if (Number.isNaN(parsed)) {
        return { ok: false, error: `无法解析时刻: ${iso}（需要 ISO 8601，如 2026-09-18T08:00）` }
      }
      deadlineMs = parsed
      deadlineLabel = iso
      if (typeof wake === 'number' && wake > 0) wakeMs = wake
      if (wakeMs <= 0) wakeMs = 300_000
      // Fresh deadline re-arms every session (including wrapped-up ones).
      for (const s of states.values()) s.finished = false
      ensureSweep()
      ctx.logger.info(`factor-mining drive: 死线已由会话设置 ${iso}`
        + `（兜底唤醒 ${wakeMs}ms；到点注入收官后解除自动推进）`)
      return { ok: true }
    },
    clearDeadline(): void {
      deadlineMs = undefined
      deadlineLabel = undefined
      for (const s of states.values()) s.finished = false
      ctx.logger.info('factor-mining drive: 死线已由会话清除——恢复常规自动推进（连续简单上限重新生效）')
    },
    status(): Record<string, unknown> {
      const wrapped = [...states.values()].filter((s) => s.finished).length
      return {
        deadline: deadlineLabel,
        deadlineEpochMs: deadlineMs,
        wrapUpInjectedSessions: wrapped,
        wakeIntervalMs: wakeMs > 0 ? wakeMs : null,
        trackedSessions: factorSessions.size,
        note: deadlineMs === undefined
          ? '未设死线：常规自动推进（连续简单注入上限生效）'
          : `死线 ${deadlineLabel}：连续简单上限让位，到点注入一次收官后自动解除；一次性，重启即失`,
      }
    },
  }
  return handle

  ctx.effect(() => () => {
    for (const t of timers) clearTimeout(t)
    timers.clear()
    states.clear()
    sweepDisposer?.()
  })
}
