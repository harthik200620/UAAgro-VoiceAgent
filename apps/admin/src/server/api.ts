import "server-only";

import { env } from "./env";
import type { Session } from "@/lib/rbac";

/**
 * The only route from the panel to the control plane (§15, §17).
 *
 * Every fetch here runs in a Server Component or a Server Action, never in the
 * browser. That is not a performance choice: §1 N6 keeps prompts, flows,
 * catalogue logic and crop recommendations off the client entirely, and the way
 * to guarantee that is for the browser to have no credential with which to ask
 * for them.
 *
 * The `server-only` import makes it structural rather than aspirational -- a
 * Client Component that imports this file fails `next build`.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly remedy?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type FetchOptions = {
  session: Session;
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  /** Set on mutations so a retried request cannot act twice (§17). */
  idempotencyKey?: string;
  /** Seconds. Omit for mutations. */
  revalidate?: number;
};

export async function apiFetch<T>(
  path: string,
  options: FetchOptions,
): Promise<T> {
  const { session, method = "GET", body, idempotencyKey, revalidate } = options;
  const { apiBaseUrl } = env();

  const headers: Record<string, string> = {
    "content-type": "application/json",
    // A bearer token, and nothing that asserts identity by itself.
    //
    // The role and centre scope travel *inside* the signed token, and the API
    // binds its RLS GUCs from the decoded claims. That is the whole point: the
    // panel cannot widen its own scope, because it has no way to state one --
    // an earlier version of this file sent `x-user-role` and `x-centre-ids`
    // headers, which the API never read, and which would have been a
    // privilege-escalation surface if it had.
    authorization: `Bearer ${session.accessToken}`,
  };
  if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;

  const response = await fetch(`${apiBaseUrl}${path}`, {
    method,
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    // Mutations are never cached; reads are cached only when the caller says
    // how stale is acceptable. §15 requires catalogue changes to reach the
    // agent "within seconds" and shows the propagation delay, so a silent
    // default TTL here would make that number a lie.
    ...(method === "GET" && revalidate !== undefined
      ? { next: { revalidate } }
      : { cache: "no-store" as const }),
  });

  if (!response.ok) {
    // The body is read for the typed error the domain layer produces -- code,
    // message, remedy -- rather than being surfaced as a status number. A
    // centre manager reading "422" learns nothing; one reading "this
    // recommendation needs a pre-harvest interval" fixes it.
    let message = `Request failed with ${response.status}`;
    let remedy: string | undefined;
    try {
      const payload = (await response.json()) as {
        message?: string;
        remedy?: string;
      };
      if (payload.message) message = payload.message;
      if (payload.remedy) remedy = payload.remedy;
    } catch {
      // A non-JSON error body is not worth failing over; the status stands.
    }
    throw new ApiError(response.status, message, remedy);
  }

  return (await response.json()) as T;
}

/**
 * Read models the panel renders.
 *
 * Declared here rather than inferred from the API so a field the control plane
 * stops sending is a type error at build time, not an empty column at 6am in
 * sowing season.
 */

export type DashboardMetrics = {
  inboundCalls: number;
  outboundCalls: number;
  answerRate: number;
  avgDurationSeconds: number;
  resolutionRate: number;
  transferRate: number;
  containmentRate: number;
  liveConcurrency: number;
  concurrencyCapacity: number;
  spendTodayRupees: number;
  budgetRupees: number;
  latencyP50Ms: number;
  latencyP95Ms: number;
  unhandledIntents: number;
  failedCalls: number;
  topIntents: { intent: string; count: number }[];
  topProducts: { sku: string; nameHi: string; count: number }[];
};

export type AdvisoryRow = {
  id: string;
  cropHi: string;
  stage: string | null;
  problemHi: string | null;
  productHi: string;
  dose: string;
  timingHi: string | null;
  phiDays: number | null;
  precautionHi: string | null;
  isCropProtection: boolean;
  approvalState: "draft" | "pending_agronomist" | "approved" | "rejected";
  approvedByName: string | null;
  approvedAt: string | null;
};

export type GateCheck =
  | "consent"
  | "dnd"
  | "internal_dnc"
  | "caller_id_series"
  | "dlt_registration"
  | "calling_window"
  | "frequency_cap"
  | "duplicate_suppression";

export type CampaignGate = {
  total: number;
  eligible: number;
  removed: Record<GateCheck, number>;
  blockedBy: GateCheck[];
  estimatedCostRupees: number;
  estimatedMinutes: number;
};

export type CallRow = {
  id: string;
  callRef: string;
  startedAt: string;
  direction: "inbound" | "outbound";
  centreCode: string | null;
  centreId: string | null;
  language: string;
  qualityTier: "A" | "B" | "C";
  durationSeconds: number;
  outcome: string;
  intent: string | null;
  transferred: boolean;
  costRupees: number;
  /** Last four digits only. The panel never receives a full number (§17). */
  callerLast4: string | null;
};

export const getDashboard = (session: Session, range: string) =>
  apiFetch<DashboardMetrics>(`/admin/dashboard?range=${range}`, {
    session,
    // Ten seconds. The dashboard is glanced at, not watched -- Live Calls is
    // the screen that streams.
    revalidate: 10,
  });

export const getAdvisoryRows = (session: Session) =>
  apiFetch<{ rows: AdvisoryRow[]; unapproved: number }>("/admin/advisory", {
    session,
    revalidate: 0,
  });

export const getCampaignGate = (session: Session, campaignId: string) =>
  apiFetch<CampaignGate>(`/admin/campaigns/${campaignId}/gate`, {
    session,
    // Never cached. §13.1's gate is evaluated at dial time and a stale
    // eligible-count on an approval screen is a reviewer approving a number
    // that is no longer true.
    revalidate: 0,
  });

export const getCalls = (session: Session, query: string) =>
  apiFetch<{ rows: CallRow[]; total: number }>(`/admin/calls?${query}`, {
    session,
    revalidate: 5,
  });

export type FlowVersion = {
  id: string;
  name: string;
  flowType: "inbound" | "outbound";
  version: number;
  isPublished: boolean;
  publishedAt: string | null;
  publishedByName: string | null;
  changelog: string | null;
  toolAllowlist: string[];
  updatedAt: string;
};

/**
 * The prompt body.
 *
 * Deliberately a separate type from `FlowVersion`, and fetched separately.
 * §1 N6 keeps prompts server-side; listing every version should not put the
 * whole prompt library in one response, and a component that only needs the
 * list cannot accidentally receive one.
 */
export type FlowDetail = FlowVersion & {
  systemPrompt: string;
  greetingTemplate: string;
  closingTemplate: string;
  escalationRules: Record<string, unknown>;
  languageRoutes: Record<string, unknown>;
  llmSettings: Record<string, unknown>;
  ttsSettings: Record<string, unknown>;
  guardrails: Record<string, unknown>;
};

export type FarmerRow = {
  id: string;
  fullName: string;
  village: string | null;
  districtName: string | null;
  centreCode: string | null;
  preferredLanguage: string;
  crops: string[];
  landAreaBigha: number | null;
  /** Four digits. There is no field here for anything longer (§17). */
  phoneLast4: string;
  isDnc: boolean;
  hasPromotionalConsent: boolean;
  lastContactAt: string | null;
};

export type InventoryRow = {
  variantId: string;
  sku: string;
  productHi: string;
  packSize: string;
  centreId: string;
  centreCode: string;
  quantity: number;
  sellingPriceRupees: number;
  isAvailable: boolean;
  updatedAt: string;
};

export type KbDocumentRow = {
  id: string;
  title: string;
  sourceType: string;
  language: string;
  version: number;
  isPublished: boolean;
  chunkCount: number;
  embeddedCount: number;
  updatedAt: string;
};

export type Analytics = {
  costPerCallRupees: number;
  costPerResolvedRupees: number;
  costByComponent: Record<string, number>;
  byLanguage: {
    language: string;
    calls: number;
    avgConfidence: number | null;
    transferRate: number;
  }[];
  byCentre: {
    centreCode: string;
    calls: number;
    avgConfidence: number | null;
    resolutionRate: number;
  }[];
};

export type SpamRuleRow = {
  id: string;
  ruleType: string;
  pattern: string | null;
  isActive: boolean;
  action: string;
  hitCount: number;
  falsePositiveCount: number;
  falsePositiveRate: number;
};

export type UserRow = {
  id: string;
  email: string;
  fullName: string;
  role: string;
  centreIds: string[];
  isActive: boolean;
  mfaEnrolled: boolean;
  lastLoginAt: string | null;
};

export type AuditRow = {
  chainIndex: number;
  at: string;
  actorName: string | null;
  action: string;
  resourceType: string;
  resourceId: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
};

export const getFlows = (session: Session, flowType?: string) =>
  apiFetch<FlowVersion[]>(
    `/admin/flows${flowType ? `?flow_type=${flowType}` : ""}`,
    { session, revalidate: 0 },
  );

export const getFlowDetail = (session: Session, id: string) =>
  apiFetch<FlowDetail>(`/admin/flows/${id}`, { session, revalidate: 0 });

export const publishFlow = (session: Session, id: string, key: string) =>
  apiFetch<{ id: string; version: number; isPublished: boolean; previousVersion: number | null }>(
    `/admin/flows/${id}/publish`,
    { session, method: "POST", idempotencyKey: key },
  );

export const getFarmers = (session: Session, query: string) =>
  apiFetch<{ rows: FarmerRow[]; total: number }>(`/admin/farmers?${query}`, {
    session,
    revalidate: 0,
  });

export const setFarmerDnc = (
  session: Session,
  farmerId: string,
  isDnc: boolean,
  key: string,
) =>
  apiFetch<{ isDnc: boolean }>(`/admin/farmers/${farmerId}/dnc`, {
    session,
    method: "POST",
    body: { isDnc },
    idempotencyKey: key,
  });

export const getInventory = (session: Session, query: string) =>
  apiFetch<{ rows: InventoryRow[]; total: number; propagationSeconds: number }>(
    `/admin/inventory?${query}`,
    // Never cached. §15.1 shows the propagation delay to the agent; adding an
    // unrelated cache in front of the grid would make that number wrong.
    { session, revalidate: 0 },
  );

export const getKbDocuments = (session: Session) =>
  apiFetch<KbDocumentRow[]>("/admin/knowledge/documents", {
    session,
    revalidate: 0,
  });

export const getAnalytics = (session: Session, days: number) =>
  apiFetch<Analytics>(`/admin/analytics?days=${days}`, {
    session,
    revalidate: 60,
  });

export const getSpamRules = (session: Session) =>
  apiFetch<SpamRuleRow[]>("/admin/spam-rules", { session, revalidate: 30 });

export const getUsers = (session: Session) =>
  apiFetch<UserRow[]>("/admin/users", { session, revalidate: 0 });

export const getAudit = (session: Session, query: string) =>
  apiFetch<{
    rows: AuditRow[];
    total: number;
    chainIntact: boolean;
    brokenAt: number | null;
  }>(`/admin/audit?${query}`, { session, revalidate: 0 });

export type LiveCall = {
  id: string;
  callRef: string;
  startedAt: string;
  direction: string;
  centreCode: string | null;
  language: string;
  callerLast4: string | null;
  elapsedSeconds: number;
  turnCount: number;
  lastIntent: string | null;
};

export type OfferRow = {
  id: string;
  code: string;
  name: string;
  descriptionHi: string;
  discountType: string;
  discountValue: number;
  validFrom: string;
  validTo: string;
  isActive: boolean;
  whatsappTemplateName: string | null;
};

export const getLiveCalls = (session: Session) =>
  apiFetch<{ rows: LiveCall[]; concurrency: number; capacity: number }>(
    "/admin/live-calls",
    // Never cached: this is the one screen that is watched rather than
    // glanced at, and a cached "live" view is a contradiction.
    { session, revalidate: 0 },
  );

export const getOffers = (session: Session) =>
  apiFetch<OfferRow[]>("/admin/offers", { session, revalidate: 30 });
