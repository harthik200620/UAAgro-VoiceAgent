import "server-only";

import { ApiError } from "./api";

/**
 * A page's read of the control plane as something it can render either way.
 *
 * A section whose data cannot be fetched should say so in its own card and
 * let the rest of the page stand: the knowledge status strip is not worth
 * losing the document list over, and a route the control plane does not
 * serve yet is a sentence, not a crash. What went wrong is folded into four
 * reasons, because that is how many different sentences there are.
 */

export type LoadFailure =
  /** The control plane answered 404 or 501: the route is not there yet. */
  | "missing"
  /** No answer at all. */
  | "unreachable"
  /** 403: the session lacks the role the route needs. */
  | "forbidden"
  /** Anything else, with the API's own words when it had any. */
  | "failed";

export type Loaded<T> =
  | { ok: true; value: T }
  | { ok: false; reason: LoadFailure; message: string | null };

export async function load<T>(request: Promise<T>): Promise<Loaded<T>> {
  try {
    return { ok: true, value: await request };
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 404 || error.status === 501) {
        return { ok: false, reason: "missing", message: null };
      }
      if (error.status === 403) return { ok: false, reason: "forbidden", message: null };
      return { ok: false, reason: "failed", message: error.remedy ?? error.message };
    }
    return { ok: false, reason: "unreachable", message: null };
  }
}

/** The value, or `null` when it could not be had and the page does fine without it. */
export async function optional<T>(request: Promise<T>): Promise<T | null> {
  const loaded = await load(request);
  return loaded.ok ? loaded.value : null;
}
