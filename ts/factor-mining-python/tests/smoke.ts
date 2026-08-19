import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { Context } from '@deepseek-ai/cordis'
import LocalSubprocessRuntime from '@deepseek-ai/dsh-subprocess-local'
import { FactorMiningPythonService } from '@deepseek-ai/dsh-factor-mining-python'

const PYTHON = process.env.DSH_FACTOR_PYTHON ?? 'python'
const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-mining-smoke-'))
const dataPath = join(dir, 'panel.parquet')
execFileSync(PYTHON, ['-c', `
import numpy as np, pandas as pd
rows, symbols, path = 600, 40, ${JSON.stringify(dataPath)}
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
const configPath = join(dir, 'factor-mining.config.json')
writeFileSync(configPath, JSON.stringify({
  version: 1,
  stateRoot: join(dir, 'state'),
  environments: {
    primary: {
      label: 'synthetic', kind: 'panel',
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
  stateRoot: join(dir, 'state'),
  dataConfigPath: configPath,
  requestTimeoutMs: 120_000,
})
try {
  const status = await ctx.factorMining.status()
  if (status.ready !== true) throw new Error('status not ready')
  const loaded = await ctx.factorMining.dataLoad({ envId: 'primary' })
  if (loaded.T !== 600 || loaded.N !== 40) throw new Error(`unexpected shape ${JSON.stringify(loaded)}`)
  const source = "import pandas as pd\ndef factor(env):\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"
  const causal = await ctx.factorMining.checkCausality({ envId: 'primary', source })
  if (causal.verdict !== 'causal') throw new Error(`causality failed: ${JSON.stringify(causal)}`)
  const diag = await ctx.factorMining.evaluate({ envId: 'primary', source, stage: 'development' })
  if (typeof diag.ic_ir_train !== 'number') throw new Error('missing ic_ir_train')
  console.log('TS_PROVIDER_SMOKE_PASS', JSON.stringify({ T: loaded.T, N: loaded.N, ic_ir_train: diag.ic_ir_train }))
} finally {
  await svcFiber.dispose().catch(() => {})
  await subFiber.dispose().catch(() => {})
  rmSync(dir, { recursive: true, force: true })
}
