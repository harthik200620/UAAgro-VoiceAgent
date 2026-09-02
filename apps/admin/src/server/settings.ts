import "server-only";

export { apiFetch } from "./api";
export type { MaskedSecret } from "./env";

import type { MaskedSecret } from "./env";

/**
 * What the Settings screen receives (§15.1).
 *
 * A list of presence flags and hints. There is deliberately no field on this
 * type that could hold a key value -- so "never returned by any API" is
 * enforced by the shape rather than by remembering not to render it.
 */
export type MaskedSecretList = { secrets: MaskedSecret[] };
