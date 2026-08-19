import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import LocalSubprocessRuntime from '@deepseek-ai/dsh-subprocess-local'
import { FactorMiningPythonService } from '@deepseek-ai/dsh-factor-mining-python'

const PYTHON = process.env.DSH_FACTOR_PYTHON ?? 'python'

function makePanel(path: string, rows = 600, symbols = 40): void {
  execFileSync(PYTHON, ['-c', `
import sys
import numpy as np, pandas as pd
path = ${JSON.stringify(path)}
rows = ${rows}; symbols = ${symbols}
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

async function setup() {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-mining-'))
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
  const svcFiber = await ctx.plugin(FactorMiningPythonService, {
    pythonExecutable: PYTHON,
    bridgeCwd: process.cwd(),
    stateRoot: join(dir, 'state'),
    dataConfigPath: configPath,
    requestTimeoutMs: 120_000,
  })
  const service = ctx.factorMining as FactorMiningPythonService
  return { ctx, service, dir, dispose: async () => {
    await svcFiber.dispose().catch(() => {})
    await subFiber.dispose().catch(() => {})
  } }
}

describe('FactorMiningPythonService', () => {
  it('starts the bridge and reports status', async () => {
    const { service, dir, dispose } = await setup()
    try {
      const status = await service.status()
      expect(status.ready).toBe(true)
      expect(status.dataConfigured).toBe(true)
      expect(Array.isArray(status.environments)).toBe(true)
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })

  it('loads synthetic data and evaluates a baseline factor', async () => {
    const { service, dir, dispose } = await setup()
    try {
      const loaded = await service.dataLoad({ envId: 'primary' })
      expect(loaded.T).toBe(600)
      expect(loaded.N).toBe(40)

      const source = "import pandas as pd\ndef factor(env):\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
      const causal = await service.checkCausality({ envId: 'primary', source })
      expect(causal.verdict).toBe('causal')

      const diag = await service.evaluate({ envId: 'primary', source, stage: 'development' })
      expect(typeof diag.ic_ir_train).toBe('number')

      const lib = await service.queryLibrary({ query: 'momentum' })
      expect(lib.configured).toBe(false)
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })
})
