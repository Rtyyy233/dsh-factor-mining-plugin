/**
 * NDJSON JSON-RPC 2.0 client over the `ctx.subprocess` piped stdio seam.
 * @module dsh-factor-mining-python/client
 */

import { createInterface } from 'node:readline'
import type { SubprocessRuntime } from '@deepseek-ai/dsh-subprocess'
import type { Writable } from 'node:stream'
import type { JsonValue } from '@deepseek-ai/dsh-factor-mining'

/** Client-owned bridge lifecycle configuration. */
export interface JsonRpcClientConfig {
  /** Python executable, e.g. `python` or an absolute path. */
  pythonExecutable: string
  /** Python module entry, default `dsh_factor_mining.bridge`. */
  bridgeModule: string
  /** Working directory for the child; defaults to the process cwd. */
  bridgeCwd: string
  /** User state root forwarded to the bridge. */
  stateRoot: string
  /** Optional user data config path. */
  dataConfigPath?: string
  /** Ready-handshake timeout. */
  startupTimeoutMs: number
  /** Per-request timeout. */
  requestTimeoutMs: number
  /** Subprocess termination grace period. */
  graceMs: number
}

interface Pending {
  resolve(value: JsonValue): void
  reject(error: Error): void
  timer: ReturnType<typeof setTimeout>
}

interface WireMessage {
  jsonrpc: '2.0'
  id?: number | null
  method?: string
  params?: unknown
  result?: unknown
  error?: { code: number; message: string; data?: unknown }
}

/**
 * JSON-RPC error from the bridge. The structured `code` is preserved for
 * programmatic dispatch (-32602 bad params, -32002 user data error,
 * -32003 discipline rejection like FUTURE_LEAK or iron-rule duplicates);
 * the message text also embeds it so string-only consumers still see it.
 */
export class BridgeRpcError extends Error {
  constructor(readonly code: number, message: string) {
    super(message)
    this.name = 'BridgeRpcError'
  }
}

/**
 * A single persistent bridge subprocess.  Requests multiplex over stdin and
 * responses arrive as one JSON object per stdout line.  Disposal terminates
 * the process tree and rejects every pending call.
 */
export class JsonRpcClient {
  readonly #subprocess: SubprocessRuntime
  readonly #config: JsonRpcClientConfig
  #nextId = 1
  #pending = new Map<number, Pending>()
  #ready: Promise<void> | undefined
  #stderr = ''
  #lastError: Error | undefined

  constructor(subprocess: SubprocessRuntime, config: JsonRpcClientConfig) {
    this.#subprocess = subprocess
    this.#config = config
  }

  get stderr(): string {
    return this.#stderr
  }

  get lastError(): Error | undefined {
    return this.#lastError
  }

  #argv(): string[] {
    const argv = [this.#config.pythonExecutable, '-m', this.#config.bridgeModule, '--state-root', this.#config.stateRoot]
    if (this.#config.dataConfigPath !== undefined) {
      argv.push('--data-config', this.#config.dataConfigPath)
    }
    return argv
  }

  async ready(): Promise<void> {
    this.#ready ??= this.#start()
    return this.#ready
  }

  async request(method: string, params: Record<string, unknown> = {}): Promise<JsonValue> {
    await this.ready()
    const id = this.#nextId++
    const payload = JSON.stringify({ jsonrpc: '2.0', id, method, params })
    const promise = new Promise<JsonValue>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id)
        this.#notifyCancel(id)
        const progressNote = this.#lastProgress !== undefined
          ? ` Last progress: ${this.#lastProgress}.`
          : ''
        reject(new Error(
          `factor-mining-python: request ${method} timed out after ${this.#config.requestTimeoutMs}ms.${progressNote} `
          + 'The bridge is single-threaded: this computation likely still runs in it, and requests '
          + 'sent meanwhile queue behind it until it finishes (they may time out too). Wait, then retry with factor_status.'))
      }, this.#config.requestTimeoutMs)
      this.#pending.set(id, { resolve, reject, timer })
    })
    this.#write(payload)
    return promise
  }

  #notifyCancel(id: number): void {
    try {
      this.#write(JSON.stringify({ jsonrpc: '2.0', method: 'cancel', params: { id } }))
    } catch {
      // The process is already gone; the pending rejection carries the cause.
    }
  }

  #write(line: string): void {
    const stdin = this.#stdin
    if (stdin === undefined) throw new Error('factor-mining-python: bridge stdin is not available')
    stdin.write(line + '\n')
  }

  #stdin: Writable | undefined
  #handle: ReturnType<SubprocessRuntime['spawn']> | undefined
  #lastProgress: string | undefined

  async #start(): Promise<void> {
    const config = this.#config
    const handle = this.#subprocess.spawn({
      argv: this.#argv(),
      cwd: config.bridgeCwd,
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' },
      graceMs: config.graceMs,
    })
    this.#handle = handle
    const stdin = handle.stdin
    const stdout = handle.stdout
    const stderr = handle.stderr
    if (stdin === undefined || stdout === undefined || stderr === undefined) {
      handle.terminate()
      throw new Error('factor-mining-python: subprocess provider did not expose piped stdio')
    }
    this.#stdin = stdin
    stderr.setEncoding('utf8')
    stderr.on('data', (chunk: string) => {
      this.#stderr = (this.#stderr + chunk).slice(-64_000)
    })
    void handle.done.then(() => {
      const error = new Error(`factor-mining-python: bridge exited with stderr tail: ${this.#stderr.slice(-2000)}`)
      for (const pending of this.#pending.values()) {
        clearTimeout(pending.timer)
        pending.reject(error)
      }
      this.#pending.clear()
    }, (error: Error) => {
      this.#lastError = error
      for (const pending of this.#pending.values()) {
        clearTimeout(pending.timer)
        pending.reject(error)
      }
      this.#pending.clear()
    })

    const readyPromise = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        // Zombie guard: a handshake timeout must not leave the subprocess running.
        // Terminate it and clear the memoized #ready so a later ready() retries fresh.
        handle.terminate()
        this.#ready = undefined
        this.#handle = undefined
        this.#stdin = undefined
        reject(new Error(`factor-mining-python: bridge ready handshake timed out after ${config.startupTimeoutMs}ms`))
      }, config.startupTimeoutMs)
      const lines = createInterface({ input: stdout, crlfDelay: Infinity })
      lines.on('line', (line) => {
        let message: WireMessage
        try {
          message = JSON.parse(line) as WireMessage
        } catch {
          return
        }
        if (message.method === 'ready') {
          clearTimeout(timer)
          resolve()
          return
        }
        this.#settle(message)
      })
      lines.on('close', () => {
        clearTimeout(timer)
        reject(new Error('factor-mining-python: bridge stdout closed before ready'))
      })
    })
    await readyPromise
  }

  #settle(message: WireMessage): void {
    // Server-progress notifications (no id): recorded and surfaced in timeout errors,
    // so a hung long computation reports how far it got instead of silence.
    if (message.id === undefined || message.id === null) {
      if (message.method === 'progress') {
        const p = (message.params ?? {}) as { label?: string, done?: number, total?: number }
        this.#lastProgress = `${p.label ?? 'work'} ${p.done ?? '?'}/${p.total ?? '?'}`
      }
      return
    }
    const pending = this.#pending.get(message.id)
    if (pending === undefined) return
    this.#pending.delete(message.id)
    clearTimeout(pending.timer)
    if (message.error !== undefined) {
      pending.reject(new BridgeRpcError(
        message.error.code,
        `factor-mining-python: [code ${message.error.code}] ${message.error.message}`))
      return
    }
    pending.resolve(message.result as JsonValue)
  }

  get lastProgress(): string | undefined {
    return this.#lastProgress
  }

  async close(): Promise<void> {
    const handle = this.#handle
    this.#handle = undefined
    this.#ready = undefined
    if (handle === undefined) return
    for (const pending of this.#pending.values()) {
      clearTimeout(pending.timer)
      pending.reject(new Error('factor-mining-python: bridge disposed'))
    }
    this.#pending.clear()
    handle.terminate()
    await handle.done.catch(() => {})
  }
}
