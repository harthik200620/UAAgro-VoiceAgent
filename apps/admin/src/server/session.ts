import "server-only";

import { cookies } from "next/headers";
import { cache } from "react";
import type { Role, Session } from "@/lib/rbac";
import { ROLES } from "@/lib/rbac";

/**
 * The signed-in user (§17).
 *
 * The cookie holds the API access token and is `httpOnly`, so the browser can
 * send it back but no script can read it. The role is *not* in the cookie in
 * any form the panel trusts: it is resolved from `/auth/me` on every render,
 * because a role a client could edit is a role a client would edit.
 *
 * The token is a signed JWT carrying the role and the centre scope as claims,
 * and the control plane binds its RLS GUCs from those claims rather than from
 * anything the panel asserts. So even a tampered cookie cannot widen scope --
 * it fails signature validation and the session is simply absent.
 *
 * Returns `null` rather than throwing when there is no session. The layout
 * renders a signed-out shell in that case; throwing here would turn "not
 * logged in yet" into a 500 on the login page itself.
 *
 * Wrapped in React's `cache`, so one page render asks the control plane who
 * this is exactly once. Without it every layout, page and Server Action that
 * needs the role opens its own request -- three round trips before the page
 * has fetched anything it will actually show, on every navigation.
 */

export const SESSION_COOKIE = "uaagro_session";

export const currentSession = cache(async (): Promise<Session | null> => {
  const store = await cookies();
  const token = store.get(SESSION_COOKIE)?.value;
  if (!token) return null;

  try {
    const { env } = await import("./env");
    const response = await fetch(`${env().apiBaseUrl}/auth/me`, {
      // Bearer, not a cookie: the control plane authenticates from this header
      // and reads no cookies at all.
      headers: { authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) return null;

    // The API's own shape, snake_case, mapped here rather than renamed there.
    // `/auth/me` is the control plane's contract with every client, and bending
    // it to one consumer's casing would make the next consumer the odd one out.
    const payload = (await response.json()) as {
      id?: string;
      role?: string;
      centre_ids?: string[];
      full_name?: string;
    };
    if (!payload.id || !isRole(payload.role)) return null;

    return {
      userId: payload.id,
      role: payload.role,
      centreIds: payload.centre_ids ?? [],
      name: payload.full_name ?? "",
      accessToken: token,
    };
  } catch {
    // A control-plane outage signs everyone out rather than failing open with
    // a fabricated session. Losing the panel during an outage is bad; granting
    // an unauthenticated user a role during one is worse.
    return null;
  }
});

/**
 * Store the access token after a successful sign-in.
 *
 * `httpOnly` so no script can read it, `sameSite: lax` so it survives a
 * top-level navigation but is not sent on cross-site requests, and `secure`
 * everywhere except local development -- where there is no TLS and setting it
 * would mean the cookie is silently never stored.
 */
export async function setSessionCookie(token: string, maxAgeSeconds: number): Promise<void> {
  const store = await cookies();
  store.set(SESSION_COOKIE, token, {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: maxAgeSeconds,
  });
}

export async function clearSessionCookie(): Promise<void> {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
}

function isRole(value: unknown): value is Role {
  return typeof value === "string" && (ROLES as readonly string[]).includes(value);
}
