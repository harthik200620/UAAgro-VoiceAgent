"use server";

import { randomUUID } from "node:crypto";

import { revalidatePath } from "next/cache";

import { can } from "@/lib/rbac";
import { ApiError, setFarmerDnc } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * The manual DNC toggle (§15.1, §18).
 *
 * Both directions are audited server-side. Turning it *off* is a decision to
 * start calling somebody who was marked not to be called, and §18 puts the
 * burden of proof on the operator -- so "who un-marked this number, and when"
 * has to have an answer that is not somebody's memory.
 */
export async function toggleDnc(
  farmerId: string,
  isDnc: boolean,
): Promise<{ ok: true; isDnc: boolean } | { ok: false; message: string }> {
  const session = await currentSession();
  if (!can(session, "farmers.edit")) {
    return { ok: false, message: "You do not have permission to change this." };
  }

  try {
    const result = await setFarmerDnc(session!, farmerId, isDnc, randomUUID());
    revalidatePath("/[locale]/farmers", "page");
    return { ok: true, isDnc: result.isDnc };
  } catch (error) {
    if (error instanceof ApiError) {
      return { ok: false, message: error.remedy ?? error.message };
    }
    return { ok: false, message: "The change could not be saved." };
  }
}
