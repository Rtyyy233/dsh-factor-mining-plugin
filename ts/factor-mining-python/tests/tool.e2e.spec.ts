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

  it('factor_evaluate returns the full diagnosis via the tool pipeline', async () => {
    const { ctx, dir, dispose } = await setup()
    try {
      const source = "import pandas as pd\ndef factor(env):\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
      const causal = await runTool(ctx, 'factor_check_causality', { envId: 'primary', source })
      expect(causal.isError).toBe(false)

      const diag = await runTool(ctx, 'factor_evaluate', { envId: 'primary', source, stage: 'development' })
      expect(diag.isError).toBe(false)
      const value = diag.content[0]
      if (value === undefined) throw new Error('factor_evaluate returned no content')
      expect(value.type).toBe('text')
      const parsed = JSON.parse((value as { type: 'text'; text: string }).text) as { ic_ir_train?: number }
      expect(typeof parsed.ic_ir_train).toBe('number')
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })
})
