import { mkdtempSync, rmSync, utimesSync, writeFileSync } from 'node:fs'
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

/**
 * Cold-start end-to-end: the REAL user path with no pre-seeded config.
 * The original tool.e2e.spec.ts pre-writes a config file and passes
 * dataConfigPath 鈥?that "warm path" never exercises first-use, which is how
 * four cold-start bugs shipped (2026-08-18 DSH field report).
 */
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

type ToolResult = { isError?: boolean; content: { type: string; text?: string }[] }

async function runTool(ctx: Context, name: string, args: Record<string, unknown>): Promise<ToolResult> {
  return await ctx.tools.execute({
    callId: CallId('tool-call-cold'),
    name,
    arguments: args,
    signal: testToolSignal,
  }) as ToolResult
}

function parseBody(result: ToolResult): Record<string, unknown> {
  expect(result.isError).toBe(false)
  const first = result.content[0]
  if (first === undefined || first.type !== 'text') throw new Error(`${JSON.stringify(result)}: no text content`)
  return JSON.parse((first as { type: 'text'; text: string }).text) as Record<string, unknown>
}

async function setup(stateRoot: string) {
  const ctx = new Context()
  const subFiber = await ctx.plugin(LocalSubprocessRuntime)
  const promptFiber = await ctx.plugin(SystemPrompt)
  const toolFiber = await ctx.plugin(ToolRuntime, { mode: 'native' })
  // NOTE: no dataConfigPath 鈥?the convention path stateRoot/data-config.json
  // must carry all state, exactly like a fresh DSH install.
  const svcFiber = await ctx.plugin(FactorMiningPythonService, {
    pythonExecutable: PYTHON,
    bridgeCwd: process.cwd(),
    stateRoot,
    requestTimeoutMs: 120_000,
  })
  const toolsFiber = await ctx.plugin(toolFactorMining as unknown as Parameters<Context['plugin']>[0], {})
  return {
    ctx,
    dispose: async () => {
      await toolsFiber.dispose().catch(() => {})
      await svcFiber.dispose().catch(() => {})
      await toolFiber.dispose().catch(() => {})
      await promptFiber.dispose().catch(() => {})
      await subFiber.dispose().catch(() => {})
    },
  }
}

function configObject(dataPath: string): Record<string, unknown> {
  return {
    version: 1,
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
  }
}

const FACTOR_SOURCE = "import pandas as pd\ndef factor(env):\n    c = pd.DataFrame(env.c)\n    return (c / c.shift(20) - 1.0).values\n"

describe('tool-factor-mining cold start (real user path, no pre-seeded config)', () => {
  it('status 鈫?probe 鈫?config_write(JSON-string config, no path) 鈫?load_env 鈫?evaluate', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-cold-'))
    const dataPath = join(dir, 'panel.parquet')
    makePanel(dataPath)
    const stateRoot = join(dir, 'state')
    const { ctx, dispose } = await setup(stateRoot)
    try {
      const st0 = parseBody(await runTool(ctx, 'factor_status', {}))
      expect(st0.dataConfigured).toBe(false)
      expect(st0.nextStep).toBe('factor_data_probe')
      expect(typeof st0.pythonExecutable).toBe('string')

      const probe = parseBody(await runTool(ctx, 'factor_data_probe', { path: dataPath }))
      expect(Array.isArray(probe.columns)).toBe(true)

      // LLM dual-shape regression: config arrives as a JSON STRING and path is
      // omitted 鈥?both shapes that crashed the field deployment.
      const write = parseBody(await runTool(ctx, 'factor_config_write', {
        config: JSON.stringify(configObject(dataPath)),
      }))
      expect(write.ok).toBe(true)
      expect(write.path).toBe(join(stateRoot, 'data-config.json'))

      const st1 = parseBody(await runTool(ctx, 'factor_status', {}))
      expect(st1.dataConfigured).toBe(true)
      expect(st1.nextStep).toBe('factor_load_env')

      const load = parseBody(await runTool(ctx, 'factor_load_env', { envId: 'primary' }))
      expect(load.ok).toBe(true)
      expect(load.T).toBe(600)

      const diag = parseBody(await runTool(ctx, 'factor_evaluate', { envId: 'primary', source: FACTOR_SOURCE, stage: 'development' }))
      expect(typeof diag.ic_ir_train).toBe('number')
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })

  it('mining loop: evaluate → record_trail → trail_summary → export_report → registry_submit(diagnosis) → state_reset', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-loop-'))
    const dataPath = join(dir, 'panel.parquet')
    makePanel(dataPath)
    const stateRoot = join(dir, 'state')
    const { ctx, dispose } = await setup(stateRoot)
    try {
      parseBody(await runTool(ctx, 'factor_config_write', {
        config: JSON.stringify(configObject(dataPath)),
      }))
      parseBody(await runTool(ctx, 'factor_load_env', { envId: 'primary' }))

      // P0-1 regression: the full diagnosis flows through the TOOL layer into the bridge.
      // Before batch 2a the tool interface had no `diagnosis` parameter at all, so every
      // registry_submit through DSH failed with "registry_submit needs diagnosis".
      const diag = parseBody(await runTool(ctx, 'factor_evaluate', {
        envId: 'primary', source: FACTOR_SOURCE, stage: 'development',
      }))
      expect(typeof diag.ic_ir_train).toBe('number')
      expect(diag.verdict).toBeDefined()

      const submit = parseBody(await runTool(ctx, 'factor_registry_submit', {
        envId: 'primary', source: FACTOR_SOURCE, name: 'mom20',
        diagnosis: diag,
      }))
      // receipt was carried inside the diagnosis -> verified true (not downgraded)
      expect(submit.receipt_verified).toBe(true)
      expect((submit.entry as { verified?: boolean }).verified).toBe(true)

      const trail = parseBody(await runTool(ctx, 'factor_record_trail', {
        entry: {
          round: 1, signal: 'mom20 20日动量',
          attribution: '随机游走面板上动量无截面排序力',
          next_hypothesis: '换波动率维度',
          new_information: '引入 amount/volume 维度',
        },
      }))
      expect(trail.ok !== false).toBe(true)

      const summary = parseBody(await runTool(ctx, 'factor_trail_summary', {}))
      expect((summary.evaluations as { total?: number }).total).toBeGreaterThanOrEqual(1)

      const report = parseBody(await runTool(ctx, 'factor_export_report', {
        envId: 'primary', source: FACTOR_SOURCE, name: 'mom20',
      }))
      expect(report.ok).toBe(true)

      // state_reset(mining) removes the engine trail too, with a backup
      const reset = parseBody(await runTool(ctx, 'factor_state_reset', { scope: 'mining' }))
      expect(reset.removed).toContain('trail_engine.json')
      expect(reset.backup).toBeTruthy()
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })

  it('state rebuilds from the convention file after bridge restart', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-restart-'))
    const dataPath = join(dir, 'panel.parquet')
    makePanel(dataPath)
    const stateRoot = join(dir, 'state')

    const first = await setup(stateRoot)
    try {
      const write = parseBody(await runTool(first.ctx, 'factor_config_write', {
        config: JSON.stringify(configObject(dataPath)),
      }))
      expect(write.ok).toBe(true)
    } finally {
      await first.dispose()
    }

    // "Restart": a fresh plugin stack over the same stateRoot, still no dataConfigPath.
    const second = await setup(stateRoot)
    try {
      const st = parseBody(await runTool(second.ctx, 'factor_status', {}))
      expect(st.dataConfigured).toBe(true)
      const load = parseBody(await runTool(second.ctx, 'factor_load_env', { envId: 'primary' }))
      expect(load.ok).toBe(true)
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await second.dispose()
    }
  })

  it('manual edit of the convention file is picked up lazily (no restart)', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-factor-lazy-'))
    const dataPath = join(dir, 'panel.parquet')
    makePanel(dataPath)
    const stateRoot = join(dir, 'state')
    const { ctx, dispose } = await setup(stateRoot)
    try {
      parseBody(await runTool(ctx, 'factor_config_write', { config: configObject(dataPath) }))

      // Hand-edit the convention file: add a second environment.
      const cfgPath = join(stateRoot, 'data-config.json')
      const cfg = JSON.parse(await import('node:fs').then(fs => fs.readFileSync(cfgPath, 'utf-8'))) as Record<string, unknown>
      const envs = cfg.environments as Record<string, unknown>
      envs.alt = JSON.parse(JSON.stringify(envs.primary))
      writeFileSync(cfgPath, JSON.stringify(cfg), 'utf-8')
      utimesSync(cfgPath, new Date(0), new Date(0))

      const st = parseBody(await runTool(ctx, 'factor_status', {}))
      const ids = (st.environments as { id: string }[]).map(e => e.id).sort()
      expect(ids).toEqual(['alt', 'primary'])
    } finally {
      rmSync(dir, { recursive: true, force: true })
      await dispose()
    }
  })
})
