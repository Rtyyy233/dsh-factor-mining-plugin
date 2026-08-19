/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-tool-factor-mining`.
 * @module @deepseek-ai/dsh-tool-factor-mining/invariant
 */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-tool-factor-mining'

/** Cordis companion plugin name. */
export const name = 'tool-factor-mining-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: these tools wrap the factor-mining seam without
 * owning event data; results are lossless JSON straight from the service.
 */
const install: InvariantInstaller = () => {}

/**
 * Register this package's invariant companion.
 * @param ctx - Cordis context carrying the invariant service.
 * @returns the installed registration's disposer after setup succeeds.
 */
export const apply = (ctx: Context): Promise<() => void> =>
  Promise.resolve(ctx.invariants.register(PACKAGE_NAME, install))
/* jscpd:ignore-end */
