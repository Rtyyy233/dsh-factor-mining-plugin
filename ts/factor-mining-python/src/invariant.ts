/**
 * Package-owned invariant companion for `@deepseek-ai/dsh-factor-mining-python`.
 * @module @deepseek-ai/dsh-factor-mining-python/invariant
 */

/* jscpd:ignore-start */
import type { Context } from '@deepseek-ai/cordis'
import type { InvariantInstaller } from '@deepseek-ai/dsh-invariants'

const PACKAGE_NAME = '@deepseek-ai/dsh-factor-mining-python'

/** Cordis companion plugin name. */
export const name = 'factor-mining-python-invariant'
/** Service required before the companion can reserve package ownership. */
export const inject = ['invariants']

/**
 * No runtime invariant: this provider proxies one Python bridge process;
 * cross-process requests are validated by the bridge itself, and the client
 * holds no in-process event stream or mutable shared state to check.
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
