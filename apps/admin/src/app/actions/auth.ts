"use server";

import { redirect } from "next/navigation";

import { env } from "@/server/env";
import { clearSessionCookie, setSessionCookie } from "@/server/session";

/**
 * Sign-in against the control plane (§17).
 *
 * The credentials cross the wire once, from this Server Action to the API, and
 * are never held anywhere. The browser posts a form to the panel's own origin;
 * the panel talks to the API server-side. That is why the panel needs no API
 * credential of its own and why no token reaches client JavaScript.
 *
 * §17 makes TOTP mandatory, so this is a two-step flow and the first step
 * never returns a session:
 *
 *   password ─→ enrolment required (first sign-in: a secret to scan)
 *            └→ challenge (subsequent: a code from the authenticator)
 *
 * Both branches end at `/auth/mfa/*`, which is the only place a token is
 * issued.
 */

export type LoginState =
  | { step: "credentials"; error?: string }
  | { step: "enrol"; mfaToken: string; secret: string; provisioningUri: string; error?: string }
  | { step: "verify"; mfaToken: string; error?: string };

type ApiError = { message?: string; remedy?: string };

async function post(path: string, body: unknown): Promise<Response> {
  return fetch(`${env().apiBaseUrl}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
}

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = (await response.json()) as ApiError;
    return payload.remedy ?? payload.message ?? fallback;
  } catch {
    return fallback;
  }
}

async function signIn(formData: FormData): Promise<LoginState> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password) {
    return { step: "credentials", error: "Enter your email and password." };
  }

  let response: Response;
  try {
    response = await post("/auth/login", { email, password });
  } catch {
    // Named rather than generic: an operator whose control plane is down
    // should not spend ten minutes doubting their own password.
    return { step: "credentials", error: "The control plane is not reachable." };
  }

  if (!response.ok) {
    // Deliberately does not distinguish "no such account" from "wrong
    // password". §17: an unauthenticated caller must not be able to enumerate
    // who has an account here.
    return {
      step: "credentials",
      error: await readError(response, "Those credentials were not accepted."),
    };
  }

  const payload = (await response.json()) as {
    status?: string;
    mfa_token?: string;
    secret?: string;
    provisioning_uri?: string;
  };

  if (payload.status === "mfa_enrolment_required" && payload.mfa_token) {
    return {
      step: "enrol",
      mfaToken: payload.mfa_token,
      secret: payload.secret ?? "",
      provisioningUri: payload.provisioning_uri ?? "",
    };
  }
  if (payload.mfa_token) {
    return { step: "verify", mfaToken: payload.mfa_token };
  }
  return { step: "credentials", error: "The control plane returned an unexpected response." };
}

async function submitMfa(
  previous: Extract<LoginState, { step: "enrol" | "verify" }>,
  formData: FormData,
): Promise<LoginState> {
  const code = String(formData.get("code") ?? "").trim();
  if (!/^\d{6}$/.test(code)) {
    return { ...previous, error: "Enter the six-digit code from your authenticator." };
  }

  const enrolling = previous.step === "enrol";
  const response = await post(
    enrolling ? "/auth/mfa/enrol" : "/auth/mfa/verify",
    enrolling
      ? { mfa_token: previous.mfaToken, secret: previous.secret, code }
      : { mfa_token: previous.mfaToken, code },
  );

  if (!response.ok) {
    return { ...previous, error: await readError(response, "That code was not accepted.") };
  }

  const token = (await response.json()) as { access_token?: string; expires_in?: number };
  if (!token.access_token) {
    return { ...previous, error: "The control plane issued no token." };
  }

  await setSessionCookie(token.access_token, token.expires_in ?? 900);
  // `typedRoutes` knows only the `/[locale]/...` shapes, so there is no
  // locale-less root to redirect to; `en` is the only configured locale.
  redirect("/en");
}

/**
 * One action for both steps, dispatching on where the flow currently is.
 *
 * A single `useActionState` rather than one per step: two hooks means the
 * second is initialised once, at first render, and still holds
 * `{step: "credentials"}` by the time the MFA form submits -- so the code goes
 * to a branch that immediately returns without doing anything. The state
 * machine has one state, so it gets one hook.
 */
export async function authenticate(
  previous: LoginState,
  formData: FormData,
): Promise<LoginState> {
  if (previous.step === "credentials") {
    return signIn(formData);
  }
  return submitMfa(previous, formData);
}

export async function signOut(): Promise<void> {
  await clearSessionCookie();
  redirect("/en/login");
}
