/**
 * Bridge-death self-healing regression (2026-08-31 kill incident).
 * The mining agent force-killed a busy bridge; every subsequent request
 * then hung until its full request timeout because the client kept writing
 * into the dead pipe and never respawned. This locks the self-healing:
 * after a kill, the next request transparently respawns a fresh bridge
 * instead of hanging until the timeout.
 */
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFileSync } from 'node:child_process'
import { describe, expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import LocalSubprocessRuntime from '@deepseek-ai/dsh-subprocess-local'
import { FactorMiningPythonService } from '@deepseek-ai/dsh-factor-mining-python'

const PYTHON = process.env.DSH_FACTOR_PYTHON ?? 'python'

function findBridgePid(stateRoot: string): number {
  const esc = stateRoot.replaceAll('\\', '\\\\').replaceAll("'", "''")
  const out = execFileSync('powershell', ['-NoProfile', '-Command',
    `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'dsh_factor_mining.bridge' -and $_.CommandLine -match '${esc}' } | Select-Object -ExpandProperty ProcessId`],
    { encoding: 'utf8' })
  const pid = Number(out.trim().split('\n')[0])
  if (!Number.isFinite(pid) || pid <= 0) throw new Error(`bridge pid not found for ${stateRoot}`)
  return pid
}

describe('bridge death self-healing', () => {
  it('a killed bridge is respawned by the next request, not hung until timeout', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'fm-respawn-'))
    const ctx = new Context()
    const subFiber = await ctx.plugin(LocalSubprocessRuntime)
    const svcFiber = await ctx.plugin(FactorMiningPythonService, {
      pythonExecutable: PYTHON,
      bridgeCwd: process.cwd(),
      stateRoot: join(dir, 'state'),
      startupTimeoutMs: 60_000,
      requestTimeoutMs: 90_000,
    })
    try {
      const service = ctx.factorMining
      const before = await service.status() as Record<string, unknown>
      expect(before).toBeTruthy()

      // Kill the bridge child exactly the way the incident did.
      const killed = findBridgePid(join(dir, 'state'))
      execFileSync('taskkill', ['/F', '/PID', String(killed)], { stdio: 'ignore' })

      // Before the fix any request after the kill hung for the full
      // requestTimeout (90s) — writing into a dead pipe with no respawn.
      // Now: a request racing the exit-event delivery is REJECTED FAST by
      // the exit handler (correct: it was pending on a dying bridge), and
      // the next request transparently spawns a fresh bridge.
      const t0 = Date.now()
      let after: Record<string, unknown> | undefined
      try {
        after = await service.status() as Record<string, unknown>
      } catch (e) {
        // Legal path: this call raced the exit-event delivery and belongs
        // to the dead bridge — it must fail FAST, not hang for 90s.
        expect(Date.now() - t0).toBeLessThan(10_000)
        expect(String(e)).toMatch(/bridge exited|bridge disposed/i)
      }
      expect(Date.now() - t0).toBeLessThan(60_000)
      // The follow-up call must succeed via a transparent respawn.
      const after2 = await service.status() as Record<string, unknown>
      expect(after2).toBeTruthy()
      expect(after2).toBeTruthy()
      // A genuinely fresh bridge process, not the killed one.
      expect(findBridgePid(join(dir, 'state'))).not.toBe(killed)
    } finally {
      await svcFiber.dispose().catch(() => {})
      await subFiber.dispose().catch(() => {})
      rmSync(dir, { recursive: true, force: true })
    }
  }, 180_000)
})
