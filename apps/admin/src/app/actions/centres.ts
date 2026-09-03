"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type { CentreInput, CentrePatch, CentreRow, StockRow, TransferRules } from "@/lib/contract";
import {
  createCentre,
  failure,
  getCentreStock,
  updateCentre,
  updateInventory,
  updateTransferRules,
  type ActionResult,
} from "@/server/api";
import { guard } from "@/server/guard";

/**
 * Centres, their managers and their stock -- what the agent tells a farmer
 * about where to go and who to speak to. A stock toggle reaches the agent
 * within five seconds, so a centre manager can switch urea off the moment
 * the last bag leaves.
 */

const CENTRES_PAGE = "/[locale]/(panel)/centres";

export type AddCentreState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "added"; code: string };

/** "seeds, fertiliser, soil testing" -> ["seeds", "fertiliser", "soil testing"]. */
function servicesFrom(text: string): string[] {
  return text
    .split(/[,;\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export async function addCentre(_previous: AddCentreState, formData: FormData): Promise<AddCentreState> {
  const t = await getTranslations("actions");
  const guarded = await guard("centres.edit");
  if (!guarded.ok) return { status: "error", message: guarded.message };
  const { session } = guarded;

  const text = (key: string) => String(formData.get(key) ?? "").trim();
  const input: CentreInput = {
    name: text("name"),
    district: text("district"),
    openTime: text("openTime"),
    closeTime: text("closeTime"),
  };
  if (!input.name || !input.district || !input.openTime || !input.closeTime) {
    return { status: "error", message: t("centreIncomplete") };
  }
  for (const key of [
    "nameHi",
    "block",
    "state",
    "pincode",
    "managerName",
    "managerNumber",
    "phone",
    "addressSpoken",
  ] as const) {
    const value = text(key);
    if (value) input[key] = value;
  }
  const services = servicesFrom(text("services"));
  if (services.length > 0) input.services = services;
  if (formData.get("isPrimary") === "on") input.isPrimary = true;

  // "27.87, 81.50" -- typed, or picked on the map and copied into the field.
  const location = text("location");
  if (location) {
    const [latitude, longitude] = location.split(/[,\s]+/).map(Number);
    if (latitude === undefined || longitude === undefined || !Number.isFinite(latitude) || !Number.isFinite(longitude)) {
      return { status: "error", message: t("badLocation") };
    }
    input.latitude = latitude;
    input.longitude = longitude;
  }

  try {
    const centre = await createCentre(session, input, randomUUID());
    revalidatePath(CENTRES_PAGE, "page");
    return { status: "added", code: centre.code };
  } catch (error) {
    return { status: "error", message: failure(error, t("changeFailed")).message };
  }
}

/** Any subset of a centre, `isPrimary: true` included: the API clears the previous primary itself. */
export async function saveCentre(centreId: string, patch: CentrePatch): Promise<ActionResult<CentreRow>> {
  const t = await getTranslations("actions");
  const guarded = await guard("centres.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    const centre = await updateCentre(session, centreId, patch);
    revalidatePath(CENTRES_PAGE, "page");
    return { ok: true, value: centre };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

export async function loadStock(centreId: string): Promise<ActionResult<StockRow[]>> {
  const t = await getTranslations("actions");
  const guarded = await guard("centres.view");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    return { ok: true, value: await getCentreStock(session, centreId) };
  } catch (error) {
    return failure(error, t("loadFailed"));
  }
}

export async function setStockAvailable(
  inventoryId: string,
  isAvailable: boolean,
): Promise<ActionResult<StockRow>> {
  const t = await getTranslations("actions");
  const guarded = await guard("inventory.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    const row = await updateInventory(session, inventoryId, { isAvailable });
    revalidatePath(CENTRES_PAGE, "page");
    return { ok: true, value: row };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

export type FallbackState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "saved"; rules: TransferRules };

export async function saveFallbackNumber(
  _previous: FallbackState,
  formData: FormData,
): Promise<FallbackState> {
  const t = await getTranslations("actions");
  const guarded = await guard("centres.edit");
  if (!guarded.ok) return { status: "error", message: guarded.message };
  const { session } = guarded;

  const fallbackNumber = String(formData.get("fallbackNumber") ?? "").trim();
  if (!fallbackNumber) return { status: "error", message: t("numberRequired") };
  try {
    const rules = await updateTransferRules(session, fallbackNumber);
    revalidatePath(CENTRES_PAGE, "page");
    return { status: "saved", rules };
  } catch (error) {
    return { status: "error", message: failure(error, t("changeFailed")).message };
  }
}
