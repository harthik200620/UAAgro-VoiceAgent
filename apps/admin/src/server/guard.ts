import "server-only";

import { getTranslations } from "next-intl/server";

import { can, type Capability, type Session } from "@/lib/rbac";
import { resolveSession } from "./session";

/**
 * The check every Server Action starts with, in one place.
 *
 * There are three ways an action can be refused before it does anything, and
 * they are three different sentences:
 *
 * - the operator is signed out, and should sign in again;
 * - the control plane did not answer, and nobody's account is at fault;
 * - the operator is signed in and genuinely lacks the permission.
 *
 * They were one sentence — *"Your account cannot do this"* — which is right
 * only for the third. During a restart of the API the panel told an operator
 * their account was not allowed to place a test call, which sent them looking
 * at roles for a problem that was a process being down.
 *
 * Returns the session, or the failure to hand straight back to the caller.
 */
export type Guarded = { ok: true; session: Session } | { ok: false; message: string; code: string };

export async function guard(capability: Capability): Promise<Guarded> {
  const [t, state] = await Promise.all([getTranslations("actions"), resolveSession()]);

  if (state.kind === "unreachable") {
    return { ok: false, message: t("controlPlaneDown"), code: "upstream_unreachable" };
  }
  if (state.kind === "signed-out") {
    return { ok: false, message: t("signedOut"), code: "unauthenticated" };
  }
  if (!can(state.session, capability)) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  return { ok: true, session: state.session };
}
