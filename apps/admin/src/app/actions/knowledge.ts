"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type { KbDocumentRow, KnowledgeAnswer, KnowledgeScope } from "@/lib/contract";
import {
  addKbUrl,
  askKnowledge,
  deleteKbDocument,
  failure,
  patchKbDocument,
  uploadKbDocument,
  type ActionResult,
} from "@/server/api";
import { guard } from "@/server/guard";

/**
 * The knowledge base: what the agent is allowed to answer from.
 *
 * A file goes up as multipart, a website as JSON; both come back as a row
 * that is still indexing, and the page polls until it is not. Switching a
 * document off is a PATCH, never a delete, so a wrong price list can be
 * silenced in a second and looked at later.
 */

const KNOWLEDGE_PAGE = "/[locale]/(panel)/inbound/knowledge";
const OUTBOUND_KNOWLEDGE_PAGE = "/[locale]/(panel)/outbound/knowledge";

export type UploadState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "queued"; title: string };

export async function addDocument(_previous: UploadState, formData: FormData): Promise<UploadState> {
  const t = await getTranslations("actions");
  const guarded = await guard("knowledge.upload");
  if (!guarded.ok) return { status: "error", message: guarded.message };
  const { session } = guarded;

  const file = formData.get("file");
  const url = String(formData.get("url") ?? "").trim();
  const title = String(formData.get("title") ?? "").trim();
  const language = String(formData.get("language") ?? "").trim();
  const scope = asScope(formData.get("scope"));

  try {
    let row: KbDocumentRow;
    if (file instanceof File && file.size > 0) {
      const body = new FormData();
      body.set("file", file, file.name);
      if (title) body.set("title", title);
      if (language) body.set("language", language);
      body.set("scope", scope);
      row = await uploadKbDocument(session, body, randomUUID());
    } else if (url) {
      row = await addKbUrl(session, { url, scope, ...(title ? { title } : {}) }, randomUUID());
    } else {
      return { status: "error", message: t("nothingToAdd") };
    }
    revalidatePath(KNOWLEDGE_PAGE, "page");
    revalidatePath(OUTBOUND_KNOWLEDGE_PAGE, "page");
    return { status: "queued", title: row.title };
  } catch (error) {
    return { status: "error", message: failure(error, t("uploadFailed")).message };
  }
}

export async function setDocumentPublished(
  documentId: string,
  isPublished: boolean,
): Promise<ActionResult<KbDocumentRow>> {
  return change(documentId, { isPublished });
}

/** Which calls may quote this document: the helpline, campaign calls, or both. */
export async function setDocumentScope(
  documentId: string,
  scope: KnowledgeScope,
): Promise<ActionResult<KbDocumentRow>> {
  return change(documentId, { scope });
}

async function change(
  documentId: string,
  patch: { isPublished?: boolean; scope?: KnowledgeScope },
): Promise<ActionResult<KbDocumentRow>> {
  const t = await getTranslations("actions");
  const guarded = await guard("knowledge.upload");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    const row = await patchKbDocument(session, documentId, patch);
    revalidatePath(KNOWLEDGE_PAGE, "page");
    revalidatePath(OUTBOUND_KNOWLEDGE_PAGE, "page");
    return { ok: true, value: row };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

/** A scope from a form field. Anything unexpected means "both", the safe default. */
function asScope(value: FormDataEntryValue | null): KnowledgeScope {
  return value === "inbound" || value === "outbound" ? value : "both";
}

export async function removeDocument(documentId: string): Promise<ActionResult<null>> {
  const t = await getTranslations("actions");
  const guarded = await guard("knowledge.upload");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  try {
    await deleteKbDocument(session, documentId);
    revalidatePath(KNOWLEDGE_PAGE, "page");
    revalidatePath(OUTBOUND_KNOWLEDGE_PAGE, "page");
    return { ok: true, value: null };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

/** `answer: true` runs the phone agent against the question -- a model call, only on the second button. */
export async function askQuestion(
  question: string,
  answer: boolean,
  direction: "inbound" | "outbound" = "inbound",
): Promise<ActionResult<KnowledgeAnswer>> {
  const t = await getTranslations("actions");
  const guarded = await guard("knowledge.ask");
  if (!guarded.ok) return guarded;
  const { session } = guarded;
  const trimmed = question.trim();
  if (!trimmed) return { ok: false, message: t("askSomething"), code: "validation_error" };
  try {
    return { ok: true, value: await askKnowledge(session, trimmed, answer, direction) };
  } catch (error) {
    return failure(error, t("askFailed"));
  }
}
