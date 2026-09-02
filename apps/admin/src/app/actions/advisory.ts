"use server";

import { revalidatePath } from "next/cache";

import { can } from "@/lib/rbac";
import { ApiError, apiFetch } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * Approve one crop recommendation (§9's safety control).
 *
 * Three checks stand between a click and a servable dose, and this is only the
 * first: the capability here, a permission dependency on the API, and a CHECK
 * constraint in Postgres that refuses `approved` without a named approver --
 * and refuses a crop-protection row without its pre-harvest interval and
 * precaution. Any one of them failing still leaves two.
 *
 * Returns a result rather than throwing. A thrown Server Action surfaces as a
 * generic error boundary, and the message that matters here -- *why* the row
 * cannot be approved -- is exactly what that would discard.
 */
export async function approveRecommendation(
  recommendationId: string,
): Promise<{ ok: true } | { ok: false; message: string }> {
  const session = await currentSession();
  if (!can(session, "advisory.approve")) {
    return { ok: false, message: "You cannot approve advisory content." };
  }

  try {
    await apiFetch(`/admin/advisory/${recommendationId}/approve`, {
      session: session!,
      method: "POST",
      // The approver is taken from the session on the server, never from the
      // request body: §9 records *who* signed off, and a client-supplied
      // approver id would make that record worthless.
      idempotencyKey: `approve-${recommendationId}-${session!.userId}`,
    });
  } catch (error) {
    if (error instanceof ApiError) {
      return { ok: false, message: error.remedy ?? error.message };
    }
    return { ok: false, message: "Something went wrong. Please try again." };
  }

  revalidatePath("/[locale]/advisory", "page");
  return { ok: true };
}
