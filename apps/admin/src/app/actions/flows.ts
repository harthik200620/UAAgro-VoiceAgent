"use server";

import { randomUUID } from "node:crypto";

import { revalidatePath } from "next/cache";

import { can } from "@/lib/rbac";
import { ApiError, publishFlow } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * Publishing an agent configuration (§15.1).
 *
 * This is the action that lets the system answer a phone. `build_call_pipeline`
 * refuses to assemble an agent without a published config -- an agent speaking
 * words nobody approved is worse than one that hands the caller to a person --
 * so a freshly seeded install takes no calls until somebody does this.
 *
 * The capability is re-checked here rather than trusted from the UI. A Server
 * Action is a public endpoint; hiding the button stops nobody who can POST.
 */
export async function publish(
  configId: string,
): Promise<{ ok: true } | { ok: false; message: string }> {
  const session = await currentSession();
  if (!can(session, "flows.publish")) {
    return { ok: false, message: "You do not have permission to publish." };
  }

  try {
    // A fresh key per attempt. Publishing is idempotent server-side --
    // publishing the live version again is a no-op rather than an error -- so
    // this guards against a retried request, not against a second click.
    await publishFlow(session!, configId, randomUUID());
  } catch (error) {
    if (error instanceof ApiError) {
      return { ok: false, message: error.remedy ?? error.message };
    }
    return { ok: false, message: "Publishing failed." };
  }

  revalidatePath("/[locale]/flows", "page");
  return { ok: true };
}
