"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type { FlowPreview, FlowScript } from "@/lib/contract";
import {
  createFlowVersion,
  failure,
  getFlow,
  previewFlow,
  publishFlow,
  testCallFlow,
  updateFlow,
  type ActionResult,
  type FlowEdit,
} from "@/server/api";
import { guard } from "@/server/guard";

/**
 * Scripts: what the agent says, versioned -- and for the helpline, the
 * persona it says it as.
 *
 * A published version is immutable, so every edit to one becomes a new draft
 * through `/versions`; a draft is patched in place. Publishing is the one
 * action with no queue between the click and a farmer hearing the result,
 * which is why the UI confirms first and why it is a separate capability.
 */

const FLOW_PAGES = ["/[locale]/(panel)/flows", "/[locale]/(panel)/flows/[id]"] as const;

/** The API refuses an empty prompt or one over this many characters; so does the panel, sooner. */
const PROMPT_MAX_CHARS = 12_000;

function revalidateFlows() {
  for (const page of FLOW_PAGES) revalidatePath(page, "page");
}

type Saved = { id: string; version: number };

/** The words, and the persona when it changed. `null` leaves the prompt as it is. */
function editOf(script: FlowScript, prompt: string | null): FlowEdit {
  return prompt === null ? { script } : { script, systemPrompt: prompt };
}

function promptProblem(prompt: string | null, wording: (key: "promptEmpty" | "promptTooLong") => string) {
  if (prompt === null) return null;
  if (prompt.trim() === "") return wording("promptEmpty");
  if (prompt.length > PROMPT_MAX_CHARS) return wording("promptTooLong");
  return null;
}

/** Edits become a new draft when the version is published, and a patch when it is not. */
export async function saveScript(
  flowId: string,
  script: FlowScript,
  isPublished: boolean,
  prompt: string | null,
): Promise<ActionResult<Saved>> {
  const t = await getTranslations("actions");
  const guarded = await guard("flows.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  const problem = promptProblem(prompt, t);
  if (problem) return { ok: false, message: problem, code: "validation_error" };
  try {
    const edit = editOf(script, prompt);
    const saved = isPublished
      ? await createFlowVersion(session, flowId, edit, randomUUID())
      : await updateFlow(session, flowId, edit);
    revalidateFlows();
    return { ok: true, value: { id: saved.id, version: saved.version } };
  } catch (error) {
    return failure(error, t("saveFailed"));
  }
}

/**
 * Publish, saving any unsaved edits first. Editing a live version and
 * publishing is therefore one click: draft vN+1, then publish it. The voice
 * worker reads the published row on every call, so this is live at once.
 */
export async function publishScript(
  flowId: string,
  script: FlowScript | null,
  isPublished: boolean,
  prompt: string | null,
): Promise<ActionResult<Saved>> {
  const t = await getTranslations("actions");
  const guarded = await guard("flows.publish");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  const problem = promptProblem(prompt, t);
  if (problem) return { ok: false, message: problem, code: "validation_error" };
  try {
    let target = flowId;
    if (script) {
      const edit = editOf(script, prompt);
      if (isPublished) {
        target = (await createFlowVersion(session, flowId, edit, randomUUID())).id;
      } else {
        await updateFlow(session, flowId, edit);
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
  const guarded = await guard("flows.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
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
  const guarded = await guard("flows.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
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
  const guarded = await guard("flows.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    return { ok: true, value: await previewFlow(session, flowId, text) };
  } catch (error) {
    return failure(error, t("previewFailed"));
  }
}

/** A real call to the operator's own phone. 503 arrives with the API's remedy, shown as it is. */
export async function testCall(flowId: string, phone: string): Promise<ActionResult<{ callSid: string }>> {
  const t = await getTranslations("actions");
  const guarded = await guard("flows.edit");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
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
