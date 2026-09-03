"use server";

import { randomUUID } from "node:crypto";

import { getLocale, getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import type {
  CampaignControl,
  CampaignImport,
  CampaignSummary,
  DialResult,
  ExtractedContacts,
} from "@/lib/contract";
import { can } from "@/lib/rbac";
import {
  approveCampaign,
  createCampaign,
  dialNumbers,
  failure,
  pauseCampaign,
  readContactFile,
  resumeCampaign,
  startCampaign,
  stopCampaign,
  updateCampaign,
  type ActionResult,
  type Failure,
} from "@/server/api";
import { guard } from "@/server/guard";

/**
 * Outbound campaigns: creating one from a pasted list, calling a list right
 * now, and driving a campaign that exists.
 *
 * Every capability is re-checked here rather than trusted from the UI: a
 * Server Action is a public endpoint, and hiding a button stops nobody who
 * can POST. Every mutation carries a fresh idempotency key so a retried
 * request cannot start a campaign twice.
 */

const OUTBOUND_PAGE = "/[locale]/(panel)/outbound";
const CAMPAIGN_PAGE = "/[locale]/(panel)/outbound/[id]";

export type CreateCampaignState =
  | { status: "idle" }
  | { status: "error"; message: string; code: string | null }
  | { status: "created"; result: CampaignImport };

export async function createCampaignAction(
  _previous: CreateCampaignState,
  formData: FormData,
): Promise<CreateCampaignState> {
  const t = await getTranslations("actions");
  const guarded = await guard("campaigns.create");
  if (!guarded.ok) return { status: "error", message: guarded.message, code: guarded.code };
  const { session } = guarded;

  const name = String(formData.get("name") ?? "").trim();
  const flowId = String(formData.get("flowId") ?? "");
  const numbers = String(formData.get("numbers") ?? "");
  const maxConcurrent = Number(formData.get("maxConcurrent") ?? 10);
  const consentNote = String(formData.get("consentNote") ?? "").trim();

  if (!name || !flowId || numbers.trim() === "") {
    return { status: "error", message: t("campaignIncomplete"), code: "validation_error" };
  }
  // The attestation is the operator's, not the form's: the box is required
  // in the browser and checked again here, and the API refuses without it.
  if (formData.get("consentAttested") !== "on") {
    return { status: "error", message: t("consentRequired"), code: "consent_required" };
  }

  try {
    const result = await createCampaign(
      session,
      {
        name,
        flowId,
        numbers,
        maxConcurrent: Number.isFinite(maxConcurrent) && maxConcurrent > 0 ? maxConcurrent : 10,
        consentAttested: true,
        ...(consentNote ? { consentNote } : {}),
      },
      randomUUID(),
    );
    revalidatePath(OUTBOUND_PAGE, "page");
    return { status: "created", result };
  } catch (error) {
    const reason = failure(error, t("campaignFailed"));
    return { status: "error", message: reason.message, code: reason.code };
  }
}

export type DialState =
  | { status: "idle" }
  | { status: "error"; message: string; code: string | null }
  /** The campaign exists but did not start: the gate or the four-eyes rule stopped it. */
  | { status: "waiting"; result: DialResult };

/**
 * "Call now": the pasted numbers become a campaign that is gated, approved
 * and started in one step. When it started, the operator lands on its board
 * and watches the cards turn; when it did not, the reasons are shown where
 * the numbers were typed.
 */
export async function dialNowAction(_previous: DialState, formData: FormData): Promise<DialState> {
  const t = await getTranslations("actions");
  const guarded = await guard("campaigns.dial");
  if (!guarded.ok) return { status: "error", message: guarded.message, code: guarded.code };
  const { session } = guarded;

  const numbers = String(formData.get("numbers") ?? "");
  const flowId = String(formData.get("flowId") ?? "").trim();
  const name = String(formData.get("name") ?? "").trim();
  const consentNote = String(formData.get("consentNote") ?? "").trim();

  if (numbers.trim() === "") {
    return { status: "error", message: t("numbersRequired"), code: "validation_error" };
  }
  if (formData.get("consentAttested") !== "on") {
    return { status: "error", message: t("consentRequired"), code: "consent_required" };
  }

  let result: DialResult;
  try {
    result = await dialNumbers(
      session,
      {
        numbers,
        consentAttested: true,
        ...(flowId ? { flowId } : {}),
        ...(name ? { name } : {}),
        ...(consentNote ? { consentNote } : {}),
      },
      randomUUID(),
    );
  } catch (error) {
    const reason = failure(error, t("dialFailed"));
    return { status: "error", message: reason.message, code: reason.code };
  }

  revalidatePath(OUTBOUND_PAGE, "page");
  if (!result.started) return { status: "waiting", result };
  redirect(`/${await getLocale()}/outbound/${encodeURIComponent(result.campaign.id)}`);
}

/** Approve, then start. Returns only on failure: success navigates to the campaign. */
export async function approveAndStart(campaignId: string): Promise<Failure> {
  const t = await getTranslations("actions");
  // Approving and starting in one gesture, so both permissions are required.
  const guarded = await guard("campaigns.approve");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  if (!can(session, "campaigns.control")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    await approveCampaign(session, campaignId, randomUUID());
    await startCampaign(session, campaignId, randomUUID());
  } catch (error) {
    return failure(error, t("campaignFailed"));
  }
  revalidatePath(CAMPAIGN_PAGE, "page");
  redirect(`/${await getLocale()}/outbound/${encodeURIComponent(campaignId)}`);
}

const CONTROLS = {
  approve: approveCampaign,
  start: startCampaign,
  pause: pauseCampaign,
  resume: resumeCampaign,
  stop: stopCampaign,
} as const;

export async function controlCampaign(
  campaignId: string,
  control: CampaignControl,
): Promise<ActionResult<CampaignSummary>> {
  const t = await getTranslations("actions");
  const needed = control === "approve" ? "campaigns.approve" : "campaigns.control";
  const guarded = await guard(needed);
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    const campaign = await CONTROLS[control](session, campaignId, randomUUID());
    revalidatePath(CAMPAIGN_PAGE, "page");
    return { ok: true, value: campaign };
  } catch (error) {
    return failure(error, t("campaignFailed"));
  }
}

export async function setCampaignConcurrency(
  campaignId: string,
  maxConcurrent: number,
): Promise<ActionResult<CampaignSummary>> {
  const t = await getTranslations("actions");
  const guarded = await guard("campaigns.control");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    const campaign = await updateCampaign(session, campaignId, { maxConcurrent });
    revalidatePath(CAMPAIGN_PAGE, "page");
    return { ok: true, value: campaign };
  } catch (error) {
    return failure(error, t("campaignFailed"));
  }
}

/**
 * Read a contact list out of a spreadsheet or CSV.
 *
 * The file goes to the control plane, which parses it in Python -- the same
 * language, and nearly the same judgement about "which cell is the phone
 * number", as the parser that validates the pasted list. The lines come back
 * for the operator to check; nothing is created here.
 */
export async function readContacts(formData: FormData): Promise<ActionResult<ExtractedContacts>> {
  const t = await getTranslations("actions");
  const guarded = await guard("campaigns.create");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  const file = formData.get("file");
  if (!(file instanceof File) || file.size === 0) {
    return { ok: false, message: t("nothingToAdd"), code: "validation_error" };
  }
  const body = new FormData();
  body.set("file", file, file.name);
  try {
    return { ok: true, value: await readContactFile(session, body) };
  } catch (error) {
    return failure(error, t("uploadFailed"));
  }
}
