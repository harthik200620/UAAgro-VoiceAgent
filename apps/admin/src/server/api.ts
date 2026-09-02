import "server-only";

import type {
  CallDetail,
  CallRow,
  CampaignControl,
  CampaignDetail,
  CampaignImport,
  CampaignInput,
  CampaignSummary,
  CentreInput,
  CentrePatch,
  CentreRow,
  ConnectionInfo,
  ConnectionTest,
  FlowDetail,
  FlowPreview,
  FlowScript,
  FlowType,
  FlowVersion,
  KbDocumentRow,
  KnowledgeAnswer,
  LiveSnapshot,
  StockRow,
  StorageReport,
  TransferRules,
} from "@/lib/contract";
import type { Session } from "@/lib/rbac";

import { env } from "./env";

/**
 * The only route from the panel to the control plane (docs/ADMIN_API.md).
 *
 * Every function here runs in a Server Component, a Server Action or a Route
 * Handler, never in the browser. That is not a performance choice: the
 * browser holds no credential, so there is nothing it could ask the API for,
 * and the `server-only` import makes that structural -- a Client Component
 * that imports this file fails `next build`.
 *
 * One function per route, named for what the operator does with it. The
 * shapes live in `@/lib/contract`, which is where components read them.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** The contract's error code: `four_eyes`, `telephony_unconfigured`, ... */
    readonly code: string | null = null,
    /** What to do about it, in the API's words, when it says. */
    readonly remedy: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** What a Server Action hands back when a call fails: words, and a code the UI may act on. */
export type Failure = { ok: false; message: string; code: string | null };

export type ActionResult<T> = { ok: true; value: T } | Failure;

/**
 * An error as the operator should read it. The API's remedy wins over its
 * message when there is one -- "add a caller ID series first" is more use
 * than "campaign blocked" -- and anything else gets the caller's fallback.
 */
export function failure(error: unknown, fallback: string): Failure {
  if (error instanceof ApiError) {
    return { ok: false, message: error.remedy ?? error.message, code: error.code };
  }
  return { ok: false, message: fallback, code: null };
}

type FetchOptions = {
  session: Session;
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** JSON-encoded, unless it is a FormData, which goes multipart as it is. */
  body?: unknown;
  /** Set on mutations so a retried request cannot act twice. */
  idempotencyKey?: string;
};

export async function apiFetch<T>(path: string, options: FetchOptions): Promise<T> {
  const { session, method = "GET", body, idempotencyKey } = options;
  const multipart = body instanceof FormData;

  const headers: Record<string, string> = {
    accept: "application/json",
    // A bearer token, and nothing that asserts identity by itself. The role
    // and centre scope travel *inside* the signed token and the API binds its
    // RLS GUCs from the decoded claims, so the panel cannot widen its own
    // scope: it has no way to state one.
    authorization: `Bearer ${session.accessToken}`,
  };
  // A multipart body carries its boundary in the header the runtime writes;
  // naming the type here would strip it.
  if (body !== undefined && !multipart) headers["content-type"] = "application/json";
  if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;

  const response = await fetch(`${env().apiBaseUrl}${path}`, {
    method,
    headers,
    ...(body === undefined ? {} : { body: multipart ? body : JSON.stringify(body) }),
    // Nothing the panel shows is safe to serve stale: live calls, stock a
    // farmer is about to be told about, a campaign that may just have been
    // stopped. Every read goes to the control plane.
    cache: "no-store",
  });

  if (!response.ok) throw await readError(response);

  // A 204 (a delete) has no body to parse; the caller's T is void there.
  return (response.status === 204 ? undefined : await response.json()) as T;
}

/**
 * The typed error the API produces -- code, message, remedy -- rather than a
 * status number. A manager reading "422" learns nothing; one reading "these
 * farmers need recorded consent first" fixes it.
 */
async function readError(response: Response): Promise<ApiError> {
  let message = `Request failed with ${response.status}`;
  let code: string | null = null;
  let remedy: string | null = null;
  try {
    const payload = (await response.json()) as {
      error?: { code?: string; message?: string; remedy?: string };
      detail?: unknown;
    };
    if (payload.error?.message) message = payload.error.message;
    else if (typeof payload.detail === "string") message = payload.detail;
    code = payload.error?.code ?? null;
    remedy = payload.error?.remedy ?? null;
  } catch {
    // A non-JSON error body is not worth failing over; the status stands.
  }
  return new ApiError(response.status, message, code, remedy);
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const id = encodeURIComponent;

/* Live */

export const getLiveSnapshot = (session: Session) =>
  apiFetch<LiveSnapshot>("/admin/live/snapshot", { session });

/* Calls */

export type CallsQuery = {
  direction?: string;
  outcome?: string;
  centreId?: string;
  campaignId?: string;
  from?: string;
  to?: string;
  q?: string;
  limit: number;
  offset: number;
};

export const getCalls = (session: Session, filters: CallsQuery) =>
  apiFetch<{ rows: CallRow[]; total: number }>(
    `/admin/calls${query({
      direction: filters.direction,
      outcome: filters.outcome,
      centre_id: filters.centreId,
      campaign_id: filters.campaignId,
      from: filters.from,
      to: filters.to,
      q: filters.q,
      limit: filters.limit,
      offset: filters.offset,
    })}`,
    { session },
  );

export const getCall = (session: Session, callId: string) =>
  apiFetch<CallDetail>(`/admin/calls/${id(callId)}`, { session });

/* Outbound */

export const getCampaigns = (session: Session) =>
  apiFetch<CampaignSummary[]>("/admin/campaigns", { session });

export const getCampaign = (session: Session, campaignId: string) =>
  apiFetch<CampaignDetail>(`/admin/campaigns/${id(campaignId)}`, { session });

export const createCampaign = (session: Session, input: CampaignInput, key: string) =>
  apiFetch<CampaignImport>("/admin/campaigns", {
    session,
    method: "POST",
    body: input,
    idempotencyKey: key,
  });

export const updateCampaign = (
  session: Session,
  campaignId: string,
  patch: { maxConcurrent?: number; name?: string },
) =>
  apiFetch<CampaignSummary>(`/admin/campaigns/${id(campaignId)}`, {
    session,
    method: "PATCH",
    body: patch,
  });

const campaignControl =
  (control: CampaignControl) => (session: Session, campaignId: string, key: string) =>
    apiFetch<CampaignSummary>(`/admin/campaigns/${id(campaignId)}/${control}`, {
      session,
      method: "POST",
      idempotencyKey: key,
    });

/** 403 with code `four_eyes` when the caller created the campaign. */
export const approveCampaign = campaignControl("approve");
export const startCampaign = campaignControl("start");
export const pauseCampaign = campaignControl("pause");
export const resumeCampaign = campaignControl("resume");
export const stopCampaign = campaignControl("stop");

/* Flows */

export const getFlows = (session: Session, flowType?: FlowType) =>
  apiFetch<FlowVersion[]>(`/admin/flows${query({ flow_type: flowType })}`, { session });

export const getFlow = (session: Session, flowId: string) =>
  apiFetch<FlowDetail>(`/admin/flows/${id(flowId)}`, { session });

/** A new draft cloned from `flowId` with the edits applied; published versions are immutable. */
export const createFlowVersion = (
  session: Session,
  flowId: string,
  body: { name?: string; script: FlowScript; changelog?: string },
  key: string,
) =>
  apiFetch<FlowDetail>(`/admin/flows/${id(flowId)}/versions`, {
    session,
    method: "POST",
    body,
    idempotencyKey: key,
  });

/** Drafts only; the API answers 409 for a published version. */
export const updateFlow = (
  session: Session,
  flowId: string,
  patch: { name?: string; script?: FlowScript; changelog?: string },
) =>
  apiFetch<FlowDetail>(`/admin/flows/${id(flowId)}`, {
    session,
    method: "PATCH",
    body: patch,
  });

export const publishFlow = (session: Session, flowId: string, key: string) =>
  apiFetch<{ id: string; version: number; isPublished: boolean; previousVersion: number | null }>(
    `/admin/flows/${id(flowId)}/publish`,
    { session, method: "POST", idempotencyKey: key },
  );

/** How the voice will say it. No vendor call. */
export const previewFlow = (session: Session, flowId: string, text: string) =>
  apiFetch<FlowPreview>(`/admin/flows/${id(flowId)}/preview`, {
    session,
    method: "POST",
    body: { text },
  });

/** A real call with this draft; 503 with a remedy when telephony is not configured. */
export const testCallFlow = (session: Session, flowId: string, phone: string, key: string) =>
  apiFetch<{ callSid: string }>(`/admin/flows/${id(flowId)}/test-call`, {
    session,
    method: "POST",
    body: { phone },
    idempotencyKey: key,
  });

/* Inbound: knowledge base */

export const getKbDocuments = (session: Session) =>
  apiFetch<KbDocumentRow[]>("/admin/knowledge/documents", { session });

/** `form` carries `file` plus optional `title` and `language`. */
export const uploadKbDocument = (session: Session, form: FormData, key: string) =>
  apiFetch<KbDocumentRow>("/admin/knowledge/documents", {
    session,
    method: "POST",
    body: form,
    idempotencyKey: key,
  });

export const addKbUrl = (
  session: Session,
  body: { url: string; title?: string; maxPages?: number },
  key: string,
) =>
  apiFetch<KbDocumentRow>("/admin/knowledge/documents", {
    session,
    method: "POST",
    body,
    idempotencyKey: key,
  });

export const setKbPublished = (session: Session, documentId: string, isPublished: boolean) =>
  apiFetch<KbDocumentRow>(`/admin/knowledge/documents/${id(documentId)}`, {
    session,
    method: "PATCH",
    body: { isPublished },
  });

export const deleteKbDocument = (session: Session, documentId: string) =>
  apiFetch<void>(`/admin/knowledge/documents/${id(documentId)}`, {
    session,
    method: "DELETE",
  });

/** `answer: true` runs the same agent the phone uses -- a model call. */
export const askKnowledge = (session: Session, question: string, answer: boolean) =>
  apiFetch<KnowledgeAnswer>("/admin/knowledge/ask", {
    session,
    method: "POST",
    body: { question, answer },
  });

/* Inbound: centres, stock, hand-over */

export const getCentres = (session: Session) =>
  apiFetch<CentreRow[]>("/admin/centres", { session });

export const createCentre = (session: Session, input: CentreInput, key: string) =>
  apiFetch<CentreRow>("/admin/centres", {
    session,
    method: "POST",
    body: input,
    idempotencyKey: key,
  });

export const updateCentre = (session: Session, centreId: string, patch: CentrePatch) =>
  apiFetch<CentreRow>(`/admin/centres/${id(centreId)}`, {
    session,
    method: "PATCH",
    body: patch,
  });

export const getCentreStock = (session: Session, centreId: string) =>
  apiFetch<StockRow[]>(`/admin/centres/${id(centreId)}/stock`, { session });

/** Reaches the agent within five seconds. */
export const updateInventory = (
  session: Session,
  inventoryId: string,
  patch: { isAvailable?: boolean; price?: number; stockQty?: number },
) =>
  apiFetch<StockRow>(`/admin/inventory/${id(inventoryId)}`, {
    session,
    method: "PATCH",
    body: patch,
  });

export const getTransferRules = (session: Session) =>
  apiFetch<TransferRules>("/admin/transfer-rules", { session });

export const updateTransferRules = (session: Session, fallbackNumber: string) =>
  apiFetch<TransferRules>("/admin/transfer-rules", {
    session,
    method: "PATCH",
    body: { fallbackNumber },
  });

/* Data */

export const getStorage = (session: Session) =>
  apiFetch<StorageReport>("/admin/data/storage", { session });

/** Never carries the password. */
export const getConnection = (session: Session) =>
  apiFetch<ConnectionInfo>("/admin/data/connection", { session });

/** Tests only; the DSN is stored and logged by nobody. */
export const testConnection = (session: Session, dsn: string) =>
  apiFetch<ConnectionTest>("/admin/data/connection/test", {
    session,
    method: "POST",
    body: { dsn },
  });
