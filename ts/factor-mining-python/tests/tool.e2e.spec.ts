import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { describe, expect, it, vi } from 'vitest'
vi.setConfig({ testTimeout: 60_000 })
import { Context } from '@deepseek-ai/cordis'
import LocalSubprocessRuntime from '@deepseek-ai/dsh-subprocess-local'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import { CallId } from '@deepseek-ai/dsh-llm'
import { FactorMiningPythonService } from '@deepseek-ai/dsh-factor-mining-python'
import * as toolFactorMining from '@deepseek-ai/dsh-tool-factor-mining'

const PYTHON = process.env.DSH_FACTOR_PYTHON ?? 'python'
const testToolSignal = new AbortController().signal

function makePanel(path: string, rows = 600, symbols = 40): void {
  execFileSync(PYTHON, ['-c', `
import numpy as np, pandas as pd
path, rows, symbols = ${JSON.stringify(path)}, ${rows}, ${symbols}
rng = np.random.default_rng(4)
dates = pd.bdate_range('2019-01-02', periods=rows)
out=[]
for i in range(symbols):
    c = 10 + np.cumsum(rng.normal(0.001, 0.01, rows))
    for t in range(rows):
        p = max(float(c[t]), 0.5)
        out.append({'eob': dates[t], 'symbol': f'S{i:02d}', 'open': p*1.001, 'high': p*1.01, 'low': p*0.99, 'close': p, 'volume': float(rng.integers(100,10000)), 'amount': float(rng.integers(1000,100000))})
pd.DataFrame(out).to_parquet(path)
`])
}

/** Dispatch one registered tool through the registry pipeline, as the agent loop would. */
async function runTool(ctx: Context, name: string, args: Record<string, unknown>) {
  return ctx.tools.execute({
    callId: CallId('tool-call-1'),
    name,
    arguments: args,
    signal: testToolSignal,
  })
}

async function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-mining-tool-'))
  const dataPath = join(dir, 'panel.parquet')
  makePanel(dataPath)
  const configPath = join(dir, 'factor-mining.config.json')
  writeFileSync(configPath, JSON.stringify({
    version: 1,
    stateRoot: join(dir, 'state'),
    environments: {
      primary: {
        label: 'synthetic',
        kind: 'panel',
        source: { type: 'parquet', path: dataPath, options: {} },
        layout: 'long',
        mapping: { symbol: 'symbol', date: 'eob', open: 'open', high: 'high', low: 'low', close: 'close', volume: 'volume', amount: 'amount' },
        constraints: { minSymbols: 10, minDates: 100, requireFiniteOhlcv: true, allowZeroVolume: true },
      },
    },
  }))
  const ctx = new Context()
  const subFiber = await ctx.plugin(LocalSubprocessRuntime)
  const promptFiber = await ctx.plugin(SystemPrompt)
  const toolFiber = await ctx.plugin(ToolRuntime, { mode: 'native' })
  const svcFiber = await ctx.plugin(FactorMiningPythonService, {
    pythonExecutable: PYTHON,
    bridgeCwd: process.cwd(),
    stateRoot: join(dir, 'state'),
    dataConfigPath: configPath,
    requestTimeoutMs: 120_000,
  })
  const toolsFiber = await ctx.plugin(toolFactorMining as unknown as Parameters<Context['plugin']>[0], {})
  return {
    ctx,
    dir,
    dispose: async () => {
      await toolsFiber.dispose().catch(() => {})
      await svcFiber.dispose().catch(() => {})
      await toolFiber.dispose().catch(() => {})
      await promptFiber.dispose().catch(() => {})
      await subFiber.dispose().catch(() => {})
    },
  }
}

describe('tool-factor-mining end-to-end (tools.execute 鈫?service 鈫?Python bridge)', () => {
  it('factor_status reports the mounted bridge through the tool pipeline', async () => {
    const { ctx, dir, dispose } = await setup()
    try {
      const result = await runTool(ctx, 'factor_status', {})
      expect(result.isError).toBe(false)
      const text = result.content[0]
      if (text === undefined) throw new Error('factor_status returned no content')
      expect(text.type).toBe('text')
      const parsed = JSON.parse((text as { type: 'text'; text: string }).text) as { ready?: boolean }
      expect(parsed.ready).toBe(true)
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })

  it('factor_evaluate async contract: job_id poll through the tool pipeline (2026-08-31)', async () => {
    const { ctx, dir, dispose } = await setup()
    try {
      const source = "import pandas as pd\ndef factor(env):\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
      const causal = await runTool(ctx, 'factor_check_causality', { envId: 'primary', source })
      expect(causal.isError).toBe(false)

      const submit = await runTool(ctx, 'factor_evaluate', { envId: 'primary', source, stage: 'development' })
      expect(submit.isError).toBe(false)
      const submitVal = JSON.parse((submit.content[0] as { type: 'text'; text: string }).text) as {
        job_id?: string, status?: string, ic_ir_train?: number,
      }
      // ASYNC BY DEFAULT: fresh source -> {job_id, status}; poll until the
      // diagnosis. (A cache HIT returns the diagnosis directly — legal too.)
      let parsed = submitVal
      if (submitVal.job_id !== undefined) {
        expect(typeof submitVal.job_id).toBe('string')
        const deadline = Date.now() + 120_000
        for (;;) {
          const poll = await runTool(ctx, 'factor_evaluate', { envId: 'primary', job_id: submitVal.job_id })
          expect(poll.isError).toBe(false)
          parsed = JSON.parse((poll.content[0] as { type: 'text'; text: string }).text) as typeof submitVal
          if (parsed.job_id === undefined && parsed.status === undefined) break
          if (Date.now() > deadline) throw new Error('job did not finish in 120s')
          await new Promise(r => setTimeout(r, 500))
        }
      }
      expect(typeof parsed.ic_ir_train).toBe('number')
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  }, 180_000)

  // R24 (2026-08-31 isolated review): job_id-only polls of composite/batch
  // must survive the TOOL layer — composite's unconditional
  // jsonRecord(args.ingredients) threw on undefined and batch's
  // required:sources rejected the poll at the schema gate. Both tools'
  // own descriptions teach exactly this poll shape.
  const pollTerminal = async (ctx: any,
                              tool: string, submit: Record<string, unknown>,
                              terminal: (v: Record<string, unknown>) => boolean,
                              outKeys: string[]) => {
    let v = JSON.parse(((await runTool(ctx, tool, submit)).content[0] as { type: 'text'; text: string }).text) as Record<string, unknown>
    const deadline = Date.now() + 180_000
    while (!terminal(v)) {
      if (v.job_id === undefined) throw new Error(`${tool}: expected job_id, got ${JSON.stringify(v).slice(0, 200)}`)
      const poll = await runTool(ctx, tool, { envId: 'primary', job_id: v.job_id })
      if (poll.isError) throw new Error(`${tool} poll errored: ${JSON.stringify(poll.content).slice(0, 300)}`)
      v = JSON.parse((poll.content[0] as { type: 'text'; text: string }).text) as Record<string, unknown>
      if (Date.now() > deadline) throw new Error(`${tool} job did not finish in 180s`)
      await new Promise(r => setTimeout(r, 500))
    }
    for (const k of outKeys) if (v[k] === undefined) throw new Error(`${tool}: terminal missing ${k}`)
    return v
  }

  it('factor_evaluate_composite job_id-only poll survives the tool layer', async () => {
    const { ctx, dir, dispose } = await setup()
    try {
      const source = "def factor(env):\n    return env.c * env.v\n"
      const ing = { base: "def factor(env):\n    return env.c\n" }
      const submit = { envId: 'primary', source, ingredients: ing, async: true }
      const v = await pollTerminal(ctx, 'factor_evaluate_composite', submit,
        (x) => x.job_id === undefined && x.status === undefined,
        ['composite'])
      expect(v.composite).toBeDefined()
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  }, 180_000)

  it('factor_evaluate_batch job_id-only poll survives the tool layer (sources optional)', async () => {
    const { ctx, dir, dispose } = await setup()
    try {
      const sources = { f1: "def factor(env):\n    return env.c / (env.v + 1.0)\n" }
      const submit = { envId: 'primary', sources, async: true }
      const v = await pollTerminal(ctx, 'factor_evaluate_batch', submit,
        (x) => x.job_id === undefined && x.status === undefined,
        ['factors'])
      const factors = v.factors as Record<string, unknown>
      const f1 = factors.f1 as Record<string, unknown>
      expect(f1._meta).toBeDefined()
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  }, 180_000)
})
