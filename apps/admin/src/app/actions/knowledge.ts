"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type { KbDocumentRow, KnowledgeAnswer } from "@/lib/contract";
import { can } from "@/lib/rbac";
import {
  addKbUrl,
  askKnowledge,
  deleteKbDocument,
  failure,
  setKbPublished,
  uploadKbDocument,
  type ActionResult,
} from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * The knowledge base: what the agent is allowed to answer from.
 *
 * A file goes up as multipart, a website as JSON; both come back as a row
 * that is still indexing, and the page polls until it is not. Switching a
 * document off is a PATCH, never a delete, so a wrong price list can be
 * silenced in a second and looked at later.
 */

const KNOWLEDGE_PAGE = "/[locale]/(panel)/inbound/knowledge";

export type UploadState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "queued"; title: string };

export async function addDocument(_previous: UploadState, formData: FormData): Promise<UploadState> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "knowledge.upload")) {
    return { status: "error", message: t("forbidden") };
  }

  const file = formData.get("file");
  const url = String(formData.get("url") ?? "").trim();
  const title = String(formData.get("title") ?? "").trim();
  const language = String(formData.get("language") ?? "").trim();

  try {
    let row: KbDocumentRow;
    if (file instanceof File && file.size > 0) {
      const body = new FormData();
      body.set("file", file, file.name);
      if (title) body.set("title", title);
      if (language) body.set("language", language);
      row = await uploadKbDocument(session, body, randomUUID());
    } else if (url) {
      row = await addKbUrl(session, { url, ...(title ? { title } : {}) }, randomUUID());
    } else {
      return { status: "error", message: t("nothingToAdd") };
    }
    revalidatePath(KNOWLEDGE_PAGE, "page");
    return { status: "queued", title: row.title };
  } catch (error) {
    return { status: "error", message: failure(error, t("uploadFailed")).message };
  }
}

export async function setDocumentPublished(
  documentId: string,
  isPublished: boolean,
): Promise<ActionResult<KbDocumentRow>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "knowledge.upload")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    const row = await setKbPublished(session, documentId, isPublished);
    revalidatePath(KNOWLEDGE_PAGE, "page");
    return { ok: true, value: row };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

export async function removeDocument(documentId: string): Promise<ActionResult<null>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "knowledge.upload")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  try {
    await deleteKbDocument(session, documentId);
    revalidatePath(KNOWLEDGE_PAGE, "page");
    return { ok: true, value: null };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

/** `answer: true` runs the phone agent against the question -- a model call, only on the second button. */
export async function askQuestion(
  question: string,
  answer: boolean,
): Promise<ActionResult<KnowledgeAnswer>> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "knowledge.ask")) {
    return { ok: false, message: t("forbidden"), code: "forbidden" };
  }
  const trimmed = question.trim();
  if (!trimmed) return { ok: false, message: t("askSomething"), code: "validation_error" };
  try {
    return { ok: true, value: await askKnowledge(session, trimmed, answer) };
  } catch (error) {
    return failure(error, t("askFailed"));
  }
}
