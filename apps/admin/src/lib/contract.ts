/**
 * Read models of the control-plane contract (docs/ADMIN_API.md), including
 * the additions of 3 September 2026.
 *
 * Types only, and in `lib` rather than `server` on purpose: Client Components
 * render these shapes as plain props, and the boundary test refuses any import
 * from `@/server/*` in a client file. Field names are the contract's,
 * verbatim, so a field the API stops sending is a type error at build time
 * rather than an empty column at 6am in sowing season.
 */

export type Direction = "inbound" | "outbound";

export type Activity = "listening" | "thinking" | "speaking" | "transferring";

export type TurnLatency = {
  totalMs: number | null;
  fromCache: boolean;
  turnMs?: number;
  sttMs?: number;
  toolMs?: number;
  llmMs?: number;
  ttsMs?: number;
  networkMs?: number;
};

export type TurnEvent = {
  callId: string;
  turnIndex: number;
  role: "farmer" | "agent";
  text: string;
  /** Seconds from the start of the call. */
  at: number;
  /** Agent turns only. */
  latency: TurnLatency | null;
  tools: { name: string; ms: number | null }[];
};

export type LiveCall = {
  id: string;
  callRef: string;
  startedAt: string;
  direction: Direction;
  centreCode: string | null;
  centreName: string | null;
  language: string;
  farmerName: string | null;
  callerLast4: string | null;
  elapsedSeconds: number;
  turnCount: number;
  lastIntent: string | null;
  activity: Activity;
  lastReplyMs: number | null;
  campaignId: string | null;
};

export type RecentCall = {
  id: string;
  startedAt: string;
  direction: Direction;
  farmerName: string | null;
  callerLast4: string | null;
  centreCode: string | null;
  outcome: string | null;
  durationSeconds: number | null;
  firstReplyMs: number | null;
  dtmf: string | null;
};

export type LiveToday = {
  calls: number;
  inbound: number;
  outbound: number;
  firstReplyP50Ms: number | null;
  firstReplyP95Ms: number | null;
  /** 0-100: calls that ended without a transfer. */
  handledByAgentPct: number | null;
  transferred: number;
  offersAccepted: number;
  offersPitched: number;
};

export type LiveSnapshot = {
  capacity: number;
  /** In progress right now, oldest first. */
  calls: LiveCall[];
  today: LiveToday;
  /** Finished today, newest first, at most 25. */
  recent: RecentCall[];
};

/* Overview */

export type OverviewRange = "today" | "7d" | "30d";

export type AttentionKind =
  | "ticket"
  | "transfer_failed"
  | "unanswered"
  | "stock_out"
  | "knowledge_pending"
  | "knowledge_failed"
  | "campaign_blocked"
  | "telephony"
  | "llm"
  | "worker";

export type AttentionSeverity = "high" | "medium" | "low";

export type AttentionItem = {
  kind: AttentionKind;
  severity: AttentionSeverity;
  /** English; the panel translates by `kind`. */
  title: string;
  detail: string | null;
  /** A panel route to act on it, e.g. "/calls/<id>". */
  href: string | null;
  at: string | null;
  count: number;
};

/** A finished call on the Overview: the live snapshot's row plus the summary and the test flag. */
export type OverviewRecentCall = RecentCall & { isTest: boolean; summaryHi: string | null };

export type ByHourRow = { hour: string; inbound: number; outbound: number };

export type TopQuestion = { intent: string; label: string; count: number };

export type Overview = {
  range: OverviewRange;
  from: string;
  to: string;
  generatedAt: string;
  live: { calls: number; capacity: number };
  calls: {
    total: number;
    inbound: number;
    outbound: number;
    /** Ended without a transfer. */
    answeredByAgent: number;
    transferred: number;
    /** Ended with an error or with no reply from the agent. */
    missed: number;
    /** Placed from the browser page; counted separately, never in the others. */
    testCalls: number;
  };
  /** CallOutcome values, largest first. */
  outcomes: { key: string; label: string; count: number }[];
  speed: {
    firstReplyP50Ms: number | null;
    firstReplyP95Ms: number | null;
    /** Every agent reply in range. */
    replyP50Ms: number | null;
    /** Replies under 1,200 ms, 0-100. */
    withinBudgetPct: number | null;
  };
  outbound: {
    campaignsRunning: number;
    contactsDialled: number;
    reached: number;
    pressed1: number;
    pressed2: number;
    optedOut: number;
  };
  /** What needs a person, most urgent first, at most 12. */
  attention: AttentionItem[];
  /** Newest first, at most 10. */
  recent: OverviewRecentCall[];
  /** 24 rows for today, one per day for 7d/30d; hours in Asia/Kolkata. */
  byHour: ByHourRow[];
  /** At most 8. */
  topQuestions: TopQuestion[];
};

export type TelephonyStatus = {
  provider: "exotel" | "twilio" | "plivo" | "simulator";
  /** Simulator: calls are answered from the browser page, nothing is dialled. */
  mode: "live" | "simulator";
  configured: boolean;
  /** The DID, masked to the last four digits. */
  inboundNumber: string | null;
  /** What to set when not configured. */
  remedy: string | null;
  /** The demo backend's browser call page, development only. */
  browserCallUrl: string | null;
};

/* Calls */

export type CallRow = {
  id: string;
  callRef: string;
  startedAt: string;
  direction: Direction;
  centreCode: string | null;
  centreId: string | null;
  language: string;
  qualityTier: "A" | "B" | "C";
  durationSeconds: number;
  outcome: string;
  intent: string | null;
  transferred: boolean;
  costRupees: number;
  callerLast4: string | null;
  farmerName: string | null;
  firstReplyMs: number | null;
  dtmf: string | null;
  campaignId: string | null;
  isTest: boolean;
  summaryHi: string | null;
  intents: string[];
  recordingAvailable: boolean;
};

export type CallEvent = { at: number; type: string; text: string };

export type Recording = {
  available: boolean;
  durationSeconds: number | null;
  bytes: number | null;
  retainedUntil: string | null;
};

export type CallStats = {
  turns: number;
  farmerTurns: number;
  agentTurns: number;
  cachedReplies: number;
  toolCalls: number;
  llmModel: string | null;
};

export type CallDetail = {
  id: string;
  callRef: string;
  direction: Direction;
  startedAt: string;
  endedAt: string | null;
  durationSeconds: number | null;
  status: string;
  outcome: string | null;
  farmerName: string | null;
  callerLast4: string | null;
  centreCode: string | null;
  centreName: string | null;
  language: string;
  campaignId: string | null;
  campaignName: string | null;
  flowName: string | null;
  flowVersion: number | null;
  summaryHi: string | null;
  summaryEn: string | null;
  recording: Recording;
  firstReplyMs: number | null;
  turns: TurnEvent[];
  events: CallEvent[];
  followUps: string[];
  isTest: boolean;
  intents: string[];
  stats: CallStats;
};

/* Outbound */

export type CampaignStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "scheduled"
  | "running"
  | "paused"
  | "completed"
  | "cancelled";

export type CampaignCounts = {
  total: number;
  done: number;
  inCall: number;
  noAnswer: number;
  waiting: number;
  pressed1: number;
  pressed2: number;
  talked: number;
  optedOut: number;
  removed: number;
};

export type CampaignSummary = {
  id: string;
  name: string;
  status: CampaignStatus;
  flowId: string | null;
  flowName: string | null;
  flowVersion: number | null;
  createdAt: string;
  createdByName: string | null;
  startedAt: string | null;
  maxConcurrent: number;
  /** "10:00" */
  windowStart: string;
  windowEnd: string;
  counts: CampaignCounts;
  firstReplyP50Ms: number | null;
  /** For the caller, with the four-eyes rule already applied. */
  canApprove: boolean;
  /** Compliance checks that stop it running; empty when clear. */
  blockedBy: string[];
};

export type ContactStatus = "waiting" | "ringing" | "in_call" | "done" | "no_answer" | "removed";

export type ContactOutcome =
  | "pressed_1"
  | "pressed_2"
  | "talked"
  | "opted_out"
  | "no_answer"
  | "busy"
  | "failed"
  | "wrong_person";

export type Contact = {
  id: string;
  farmerName: string | null;
  last4: string;
  status: ContactStatus;
  outcome: ContactOutcome | null;
  dtmf: string | null;
  callId: string | null;
  attempts: number;
  lastAttemptAt: string | null;
  removedReason: string | null;
  /** In simulator mode, the worker's browser page that answers this call; null otherwise. */
  answerUrl: string | null;
};

export type CampaignDetail = CampaignSummary & { contacts: Contact[] };

/** The five POST routes that drive a campaign, named as the API names them. */
export type CampaignControl = "approve" | "start" | "pause" | "resume" | "stop";

/** What a contact file offered up, before a campaign exists. */
export type ExtractedContacts = {
  /** `name, number` or a bare number, in file order. */
  lines: string[];
  found: number;
  /** Rows with no number in them: headers, blanks, totals. */
  skipped: number;
  sheets: number;
};

export type CampaignImport = {
  campaign: CampaignSummary;
  imported: number;
  /** Bad lines, masked: never a full number. */
  invalid: string[];
  removedBy: { check: string; count: number }[];
};

export type CampaignInput = {
  name: string;
  flowId: string;
  /** Pasted text, one contact per line: `number` or `name, number`. */
  numbers: string;
  maxConcurrent: number;
  consentAttested: true;
  consentNote?: string;
};

/** "Call now": a campaign created, gated, approved and started in one step. */
export type DialInput = {
  numbers: string;
  flowId?: string;
  name?: string;
  consentAttested: true;
  consentNote?: string;
};

export type DialResult = CampaignImport & {
  /** False when the gate blocked it or it waits for a second approver. */
  started: boolean;
  blockedBy: string[];
};

/* Flows */

export type FlowType = "inbound" | "outbound";

export type FlowVersion = {
  id: string;
  name: string;
  flowType: FlowType;
  version: number;
  isPublished: boolean;
  publishedAt: string | null;
  publishedByName: string | null;
  changelog: string | null;
  toolAllowlist: string[];
  updatedAt: string;
  usedByCampaigns: number;
};

export type OutboundScript = {
  /** Read-only: the legal disclosure and the name check. */
  opening: string;
  askTime: string;
  /** The offer, invitation or reminder; may use {नाम} and {केंद्र}. */
  message: string;
  thenAsk: string;
  onPress1: string;
  onPress2: "knowledge_base";
  /** Read-only. */
  optOut: string;
  closing: string;
};

export type InboundScript = {
  greetingKnown: string;
  greetingUnknown: string;
  closing: string;
};

export type FlowScript = OutboundScript | InboundScript;

export type FlowDetail = FlowVersion & {
  script: FlowScript;
  /** The inbound persona. Editable for inbound flows; read-only elsewhere. */
  systemPrompt: string;
  promptWordCount: number;
  /** The seed wording, so an operator can restore it. */
  defaults: { systemPrompt: string; greeting: string; closing: string };
};

export type FlowPreview = {
  spoken: string;
  words: number;
  seconds: number;
  substitutions: { from: string; to: string }[];
};

/* Knowledge */

export type IngestStatus = "pending" | "indexing" | "indexed" | "failed";

/** "both" is the default: crop and product material belongs on either call. */
export type KnowledgeScope = "inbound" | "outbound" | "both";

export type KbDocumentRow = {
  id: string;
  title: string;
  /** "pdf" | "docx" | "csv" | "text" | "markdown" | "web" */
  docType: string;
  language: string;
  /** Filename or URL. */
  source: string | null;
  version: number;
  chunks: number;
  embedded: number;
  pageCount: number | null;
  ingestStatus: IngestStatus;
  ingestError: string | null;
  isPublished: boolean;
  /** Which calls may quote it. Retrieval enforces this, not just the panel. */
  scope: KnowledgeScope;
  updatedAt: string;
  sizeBytes: number | null;
  indexedAt: string | null;
  wordCount: number | null;
};

export type KnowledgeStatus = {
  /** The background worker's heartbeat, refreshed every 60 s. */
  worker: { alive: boolean; lastSeenAt: string | null };
  documents: { pending: number; indexing: number; indexed: number; failed: number };
  chunks: number;
  embedded: number;
  embeddingModel: string;
  /** The last measured search latency. */
  retrievalMs: number | null;
};

export type KnowledgeAnswer = {
  passages: { documentTitle: string; section: string | null; snippet: string; score: number }[];
  retrievalMs: number;
  degraded: boolean;
  answer: null | { text: string; totalMs: number; note: string | null };
};

/* Centres */

export type StockRow = {
  inventoryId: string;
  productName: string;
  variantName: string;
  price: number | null;
  isAvailable: boolean;
  stockQty: number | null;
};

export type CentreRow = {
  id: string;
  code: string;
  name: string;
  nameHi: string | null;
  district: string;
  block: string | null;
  state: string;
  pincode: string | null;
  latitude: number | null;
  longitude: number | null;
  managerName: string | null;
  /** Staff numbers come in full because staff edit them. */
  managerNumber: string | null;
  phone: string | null;
  /** "08:00" */
  openTime: string;
  closeTime: string;
  /** ["mon", ..., "sat"] */
  workingDays: string[];
  isActive: boolean;
  openNow: boolean;
  /** The first six items; the full list comes from /stock. */
  stock: StockRow[];
  /** The head office: the helpline answers stock and price for it when the caller's centre is unknown. */
  isPrimary: boolean;
  addressSpoken: string | null;
  services: string[];
  stockOuts: number;
};

export type CentreInput = {
  name: string;
  nameHi?: string;
  district: string;
  block?: string;
  state?: string;
  pincode?: string;
  latitude?: number;
  longitude?: number;
  managerName?: string;
  managerNumber?: string;
  phone?: string;
  openTime: string;
  closeTime: string;
  workingDays?: string[];
  isPrimary?: boolean;
  addressSpoken?: string;
  services?: string[];
};

export type CentrePatch = Partial<CentreInput> & { isActive?: boolean };

export type TransferRules = {
  /** From configuration; read-only. */
  reasons: { key: string; label: string; enabled: boolean }[];
  fallbackNumber: string | null;
  ringTimeoutSeconds: number;
  whisperSeconds: number;
};

/* Data */

export type StorageReport = {
  countedAt: string;
  tables: {
    label: string;
    table: string;
    rows: number;
    bytes: number;
    note: string;
    indexedBy: string;
    partitioned: boolean;
  }[];
  recordings: { bucket: string; region: string; retentionDays: number };
  backups: { schedule: string; lastAt: string | null; lastBytes: number | null };
};

export type ConnectionInfo = {
  host: string;
  port: number;
  database: string;
  user: string;
  tls: boolean;
  poolSize: number;
  poolBusy: number;
  latencyMs: number;
  serverVersion: string;
  rowLevelSecurity: true;
};

/**
 * Redis and the recordings store, probed on their own.
 *
 * Separate from the connection because a service that is down answers by
 * timing out: the Data page renders without waiting for this and fills the
 * row in when it arrives.
 */
export type ServiceHealthReport = {
  redis: { ok: boolean; latencyMs: number | null };
  storage: { ok: boolean; latencyMs: number | null; endpoint: string | null };
};

export type ConnectionTest = {
  ok: boolean;
  latencyMs: number | null;
  serverVersion: string | null;
  error: string | null;
};

/* Data sources: the client's MySQL database */

export type SourceTable = "stores" | "products" | "stock";

/** Their table, and our field → their column. */
export type TableMap = { table: string; columns: Record<string, string> };

export type SourceMapping = Record<SourceTable, TableMap | null>;

export type SyncRun = {
  id: string;
  startedAt: string;
  finishedAt: string | null;
  status: "running" | "ok" | "failed";
  /** Rows written. */
  stores: number;
  products: number;
  stock: number;
  error: string | null;
};

export type SourceSchedule = "manual" | "hourly" | "daily";

export type DataSource = {
  id: string;
  name: string;
  kind: "mysql";
  host: string;
  port: number;
  database: string;
  user: string;
  tls: boolean;
  /** The password itself is never returned. */
  hasPassword: boolean;
  mapping: SourceMapping;
  schedule: SourceSchedule;
  lastRun: SyncRun | null;
  createdAt: string;
  updatedAt: string;
};

export type DataSourceInput = {
  name: string;
  host: string;
  port?: number;
  database: string;
  user: string;
  password: string;
  tls?: boolean;
  mapping?: SourceMapping;
  schedule?: SourceSchedule;
};

/** Any subset; `password` replaces the stored one. */
export type DataSourcePatch = Partial<DataSourceInput>;

export type SourceConnectionInput = {
  host: string;
  port?: number;
  database: string;
  user: string;
  password: string;
  tls?: boolean;
};

export type ConnectionReport = {
  ok: boolean;
  latencyMs: number | null;
  serverVersion: string | null;
  tables: { name: string; rows: number | null }[];
  error: string | null;
};

/** One of their tables: its columns, and the first five rows for the mapping screen. */
export type TableColumns = {
  columns: { name: string; type: string }[];
  sample: Record<string, unknown>[];
};
