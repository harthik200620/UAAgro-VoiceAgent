"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type { CallDetail } from "@/lib/contract";
import { failure, summariseCall, type ActionResult } from "@/server/api";
import { guard } from "@/server/guard";

/** What "Summarise again" hands back: the words that changed, nothing the page already has. */
export type Summary = Pick<CallDetail, "summaryHi" | "summaryEn" | "followUps">;

/**
 * Re-run the post-call summary for one call. A model call on the API side,
 * so it happens on a click and never on load; the fresh key means a retried
 * click cannot run it twice.
 */
export async function summariseAgain(callId: string): Promise<ActionResult<Summary>> {
  const t = await getTranslations("actions");
  const guarded = await guard("calls.view");
  if (!guarded.ok) return guarded;
  try {
    const call = await summariseCall(guarded.session, callId, randomUUID());
    revalidatePath("/[locale]/(panel)/calls/[id]", "page");
    return {
      ok: true,
      value: { summaryHi: call.summaryHi, summaryEn: call.summaryEn, followUps: call.followUps },
    };
  } catch (error) {
    return failure(error, t("summariseFailed"));
  }
}
