"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type { FlowPreview, FlowScript } from "@/lib/contract";
import { can } from "@/lib/rbac";
import {
  createFlowVersion,
  failure,
  getFlow,
  previewFlow,
  publishFlow,
  testCallFlow,
  updateFlow,
  type ActionResult,
} from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * Scripts: what the agent says, versioned.
 *
 * A published version is immutable, so every edit to one becomes a new draft
 * through `/versions`; a draft is patched in place. Publishing is the one
 * action with no queue between the click and a farmer hearing the result,
 * which is why the UI confirms first and why it is a separate capability.
 */

const FLOW_PAGES = [
  "/[locale]/(panel)/flows",
  "/[locale]/(panel)/flows/[id]",
  "/[locale]/(panel)/inbound/greeting",
] as const;

function revalidateFlows() {
  for (const page of FLOW_PAGES) revalidatePath(page, "page");
}

type Saved = { id: string; version: number };

/** Edits become a new draft when the version is published, and a patch when it is not. */
export async function saveScript(
  flowId: string,
  script: FlowScript,
  isPublished: boolean,
): Promise<ActionResult<Saved>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    const saved = isPublished
      ? await createFlowVersion(session, flowId, { script }, randomUUID())
      : await updateFlow(session, flowId, { script });
    revalidateFlows();
    return { ok: true, value: { id: saved.id, version: saved.version } };
  } catch (error) {
    return failure(error, t("saveFailed"));
  }
}

/**
 * Publish, saving any unsaved edits first. Editing a live version and
 * publishing is therefore one click: draft vN+1, then publish it.
 */
export async function publishScript(
  flowId: string,
  script: FlowScript | null,
  isPublished: boolean,
): Promise<ActionResult<Saved>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "flows.publish")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    let target = flowId;
    if (script) {
      if (isPublished) {
        target = (await createFlowVersion(session, flowId, { script }, randomUUID())).id;
      } else {
        await updateFlow(session, flowId, { script });
      }
    }
    const published = await publishFlow(session, target, randomUUID());
    revalidateFlows();
    return { ok: true, value: { id: published.id, version: published.version } };
  } catch (error) {
    return failure(error, t("publishFailed"));
  }
}

/** A new script: a draft cloned from an existing one under a new name. The API has no "create from nothing". */
export async function createScriptFrom(
  sourceId: string,
  name: string,
): Promise<ActionResult<{ id: string }>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  const trimmed = name.trim();
  if (!trimmed) return { ok: false, message: t("nameRequired"), code: "validation_error" };
  try {
    const source = await getFlow(session, sourceId);
    const draft = await createFlowVersion(
      session,
      sourceId,
      { name: trimmed, script: source.script },
      randomUUID(),
    );
    revalidateFlows();
    return { ok: true, value: { id: draft.id } };
  } catch (error) {
    return failure(error, t("saveFailed"));
  }
}

/** Restore = a new draft with an old version's words. Nothing is overwritten. */
export async function restoreVersion(versionId: string): Promise<ActionResult<{ id: string }>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    const source = await getFlow(session, versionId);
    const draft = await createFlowVersion(
      session,
      versionId,
      { script: source.script, changelog: t("restoredFrom", { version: source.version }) },
      randomUUID(),
    );
    revalidateFlows();
    return { ok: true, value: { id: draft.id } };
  } catch (error) {
    return failure(error, t("saveFailed"));
  }
}

export async function previewScript(flowId: string, text: string): Promise<ActionResult<FlowPreview>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    return { ok: true, value: await previewFlow(session, flowId, text) };
  } catch (error) {
    return failure(error, t("previewFailed"));
  }
}

/** A real call to the operator's own phone. 503 arrives with the API's remedy, shown as it is. */
export async function testCall(flowId: string, phone: string): Promise<ActionResult<{ callSid: string }>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  const digits = phone.replace(/\D/g, "");
  if (digits.length < 10 || digits.length > 13) {
    return { ok: false, message: t("badPhone"), code: "validation_error" };
  }
  try {
    return { ok: true, value: await testCallFlow(session, flowId, phone.trim(), randomUUID()) };
  } catch (error) {
    return failure(error, t("testCallFailed"));
  }
}
