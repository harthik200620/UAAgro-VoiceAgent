# Control-plane contract for the admin panel

The panel (`apps/admin`) and the API (`apps/api`) are built to this document.
Every field is camelCase JSON. Every route is under the API's origin, needs a
bearer access token (see `/auth/*`), and answers a domain error as
`{"error": {"code", "message", "remedy", "context"}}`.

Three properties hold everywhere and are not repeated per route:

- **No farmer phone number ever leaves the API.** Farmers are identified by
  `farmerName` plus `callerLast4` / `last4`. Staff numbers (a centre manager's
  transfer number) are returned in full because staff edit them.
- **Row-level security applies to every read.** A centre manager sees only
  their centres' calls, campaigns and stock; the API filters, the panel never
  has to.
- **Server-sent events carry deltas, never the whole state.** A consumer loads
  the snapshot with a normal GET, then applies events. Every stream sends
  `heartbeat` every 15 s and a `snapshot` event first.

Roles: `read_only < auditor < agronomist < centre_manager < ops_manager <
super_admin`. "Needs X" below means X or higher.

---

## Auth (unchanged)

| Route | Notes |
|---|---|
| `POST /auth/login` `{email, password}` | → `{status: "mfa_required" \| "mfa_enrolment_required", mfa_token, secret?, provisioning_uri?}` |
| `POST /auth/mfa/enrol` `{mfa_token, secret, code}` | → `{access_token, expires_in}` |
| `POST /auth/mfa/verify` `{mfa_token, code}` | → `{access_token, expires_in}` |
| `POST /auth/refresh` · `POST /auth/logout` · `GET /auth/me` | `/auth/me` → `{id, role, centre_ids, full_name}` (snake_case: shared with other clients) |

---

## Live

### `GET /admin/live/snapshot` — needs centre_manager

```ts
type LiveSnapshot = {
  capacity: number;                 // max concurrent calls this deployment allows
  calls: LiveCall[];                // in progress right now, oldest first
  today: {
    calls: number; inbound: number; outbound: number;
    firstReplyP50Ms: number | null; firstReplyP95Ms: number | null;
    handledByAgentPct: number | null;   // 0-100, calls that ended without a transfer
    transferred: number;
    offersAccepted: number; offersPitched: number;
  };
  recent: RecentCall[];             // finished today, newest first, max 25
};
type LiveCall = {
  id: string; callRef: string; startedAt: string;        // ISO 8601
  direction: "inbound" | "outbound";
  centreCode: string | null; centreName: string | null;
  language: string;                                      // "hi", "en", ... or ""
  farmerName: string | null; callerLast4: string | null;
  elapsedSeconds: number; turnCount: number;
  lastIntent: string | null;
  activity: "listening" | "thinking" | "speaking" | "transferring";
  lastReplyMs: number | null;
  campaignId: string | null;
};
type RecentCall = {
  id: string; startedAt: string; direction: "inbound" | "outbound";
  farmerName: string | null; callerLast4: string | null;
  centreCode: string | null; outcome: string | null;     // CallOutcome value
  durationSeconds: number | null; firstReplyMs: number | null;
  dtmf: string | null;                                   // "1", "2", "9" when pressed
};
```

### `GET /admin/live/events` — server-sent events, needs centre_manager

`event:` names and `data:` payloads (JSON):

| event | data |
|---|---|
| `snapshot` | `LiveSnapshot` |
| `call.started` | `LiveCall` |
| `call.identified` | `{callId, farmerName, callerLast4, centreCode, centreName, language, campaignId}` |
| `call.activity` | `{callId, activity}` |
| `call.turn` | `TurnEvent` |
| `call.dtmf` | `{callId, digit, at}` |
| `call.transfer` | `{callId, to: string, reason: string}` (`to` is a centre/line name, never a number) |
| `call.ended` | `{callId, status, outcome, durationSeconds, firstReplyMs}` |
| `heartbeat` | `{at}` |

```ts
type TurnEvent = {
  callId: string; turnIndex: number;
  role: "farmer" | "agent";
  text: string;
  at: number;                        // seconds from call start
  latency: null | {                  // agent turns only
    totalMs: number | null; fromCache: boolean;
    turnMs?: number; sttMs?: number; toolMs?: number;
    llmMs?: number; ttsMs?: number; networkMs?: number;
  };
  tools: { name: string; ms: number | null }[];
};
```

---

## Calls

### `GET /admin/calls` — needs centre_manager (unchanged shape, new filters)

Query: `direction`, `outcome`, `centre_id`, `campaign_id`, `from`, `to`
(ISO dates), `q` (transcript search), `limit` (≤200), `offset`.
→ `{rows: CallRow[], total}` where `CallRow` adds `farmerName`, `firstReplyMs`,
`dtmf`, `campaignId` to the existing row.

### `GET /admin/calls/{id}` — needs centre_manager

```ts
type CallDetail = {
  id: string; callRef: string; direction: "inbound" | "outbound";
  startedAt: string; endedAt: string | null; durationSeconds: number | null;
  status: string; outcome: string | null;
  farmerName: string | null; callerLast4: string | null;
  centreCode: string | null; centreName: string | null;
  language: string;
  campaignId: string | null; campaignName: string | null;
  flowName: string | null; flowVersion: number | null;
  summaryHi: string | null; summaryEn: string | null;
  recording: { available: boolean; durationSeconds: number | null; bytes: number | null; retainedUntil: string | null };
  firstReplyMs: number | null;
  turns: TurnEvent[];                     // ordered, farmer and agent
  events: { at: number; type: string; text: string }[];   // "dtmf", "transfer", "whatsapp_sent", "ended", ...
  followUps: string[];                    // human sentences: "WhatsApp offer sent", "Agronomist call-back created"
};
```

### `GET /admin/calls/{id}/recording` — needs centre_manager

Streams `audio/wav`. 404 when no recording. Never a redirect to storage.

---

## Outbound

```ts
type CampaignCounts = {
  total: number; done: number; inCall: number; noAnswer: number; waiting: number;
  pressed1: number; pressed2: number; talked: number; optedOut: number; removed: number;
};
type CampaignSummary = {
  id: string; name: string;
  status: "draft" | "pending_approval" | "approved" | "scheduled" | "running" | "paused" | "completed" | "cancelled";
  flowId: string | null; flowName: string | null; flowVersion: number | null;
  createdAt: string; createdByName: string | null; startedAt: string | null;
  maxConcurrent: number; windowStart: string; windowEnd: string;   // "10:00"
  counts: CampaignCounts;
  firstReplyP50Ms: number | null;
  canApprove: boolean;      // for the caller: four-eyes rule already applied
  blockedBy: string[];      // compliance checks that stop it running, empty when clear
};
type Contact = {
  id: string; farmerName: string | null; last4: string;
  status: "waiting" | "in_call" | "done" | "no_answer" | "removed";
  outcome: "pressed_1" | "pressed_2" | "talked" | "opted_out" | "no_answer" | "busy" | "failed" | "wrong_person" | null;
  dtmf: string | null; callId: string | null;
  attempts: number; lastAttemptAt: string | null;
  removedReason: string | null;      // ExclusionReason value when status is "removed"
};
```

| Route | Needs | Body → Result |
|---|---|---|
| `GET /admin/campaigns` | centre_manager | → `CampaignSummary[]` newest first |
| `POST /admin/campaigns` | ops_manager | `{name, flowId, numbers: string, maxConcurrent, consentAttested: true, consentNote?}` → `{campaign: CampaignSummary, imported, invalid: string[] (bad lines, never full numbers: first 3 + last 2 digits), removedBy: {check: string, count: number}[]}`. `numbers` is pasted text, one contact per line: `number` or `name, number`. `consentAttested` must be true (422 otherwise): the operator attests these farmers consented to promotional calls, and that attestation is audited. |
| `POST /admin/campaigns/contacts/extract` | ops_manager | `multipart/form-data` with `file` (`.xlsx`, `.csv`, `.tsv`, `.txt`) → `{lines: string[], found, skipped, sheets}`. Reads the contact list out of the file and hands back `name, number` lines for the operator to check; nothing is created. `.xls` is refused with instructions to save as `.xlsx`. |
| `GET /admin/campaigns/{id}` | centre_manager | → `CampaignSummary & {contacts: Contact[]}` |
| `PATCH /admin/campaigns/{id}` | ops_manager | `{maxConcurrent?, name?}` → `CampaignSummary` |
| `POST /admin/campaigns/{id}/approve` | ops_manager | → `CampaignSummary`. 403 with code `four_eyes` when the caller created it, unless the caller is super_admin. |
| `POST /admin/campaigns/{id}/start` | ops_manager | → `CampaignSummary` (enqueues the dialer; status → running) |
| `POST /admin/campaigns/{id}/pause` · `/resume` · `/stop` | ops_manager | → `CampaignSummary` |
| `GET /admin/campaigns/{id}/events` | centre_manager | SSE: `snapshot` (`CampaignSummary & {contacts}`), `contact.updated` (`Contact`), `campaign.updated` (`CampaignSummary`), `call.turn` (`TurnEvent`, calls of this campaign only), `heartbeat` |
| `GET /admin/campaigns/{id}/export.csv` | ops_manager | `text/csv`: name, last4, status, outcome, dtmf, attempts, callId |

---

## Flows

```ts
type FlowVersion = {
  id: string; name: string; flowType: "inbound" | "outbound"; version: number;
  isPublished: boolean; publishedAt: string | null; publishedByName: string | null;
  changelog: string | null; toolAllowlist: string[]; updatedAt: string;
  usedByCampaigns: number;
};
type OutboundScript = {
  opening: string;      // read-only: the legal disclosure + "क्या मैं {नाम} जी से बात कर रहा हूँ?"
  askTime: string;
  message: string;      // the offer / invitation / reminder; may use {नाम} and {केंद्र}
  thenAsk: string;
  onPress1: string;
  onPress2: "knowledge_base";   // fixed
  optOut: string;       // read-only
  closing: string;
};
type InboundScript = { greetingKnown: string; greetingUnknown: string; closing: string };
type FlowDetail = FlowVersion & {
  script: OutboundScript | InboundScript;
  systemPrompt: string;       // shown read-only, ops_manager+ only
};
```

| Route | Needs | Body → Result |
|---|---|---|
| `GET /admin/flows?flow_type=` | ops_manager | → `FlowVersion[]` |
| `GET /admin/flows/{id}` | ops_manager | → `FlowDetail` |
| `POST /admin/flows/{id}/versions` | ops_manager | `{name?, script, changelog?}` → `FlowDetail` — a new **draft** version cloned from `{id}` with the edits applied. Published versions are immutable; editing one always goes through here. |
| `PATCH /admin/flows/{id}` | ops_manager | `{name?, script?, changelog?}` → `FlowDetail` — draft only (409 on a published version) |
| `POST /admin/flows/{id}/publish` | ops_manager | → `{id, version, isPublished, previousVersion}` |
| `POST /admin/flows/{id}/preview` | ops_manager | `{text}` → `{spoken: string, words: number, seconds: number, substitutions: {from: string, to: string}[]}` — how the voice will say it (digits → Hindi words, dates read aloud). No vendor call. |
| `POST /admin/flows/{id}/audio` | ops_manager | `{text}` → `audio/wav` rendered by the configured Hindi voice (a vendor call; the panel calls it only on an explicit click) |
| `POST /admin/flows/{id}/test-call` | ops_manager | `{phone}` → `{callSid}` — places a real call with this draft (telephony must be configured; 503 with a remedy otherwise) |

---

## Inbound · knowledge base

```ts
type KbDocumentRow = {
  id: string; title: string; docType: string;        // "pdf" | "docx" | "csv" | "text" | "markdown" | "web"
  language: string; source: string | null;           // filename or URL
  version: number; chunks: number; embedded: number;
  pageCount: number | null;
  ingestStatus: "pending" | "indexing" | "indexed" | "failed";
  ingestError: string | null;
  isPublished: boolean; updatedAt: string;
};
```

| Route | Needs | Body → Result |
|---|---|---|
| `GET /admin/knowledge/documents` | agronomist | → `KbDocumentRow[]` |
| `POST /admin/knowledge/documents` | ops_manager | `multipart/form-data` with `file` (+ optional `title`, `language`, `scope`) **or** JSON `{url, title?, maxPages?, scope?}` → `KbDocumentRow` with `ingestStatus: "pending"`. `scope` is `"inbound" | "outbound" | "both"` and defaults to `both`. Extraction, chunking and embedding run in the background; poll the list. A document is published automatically when indexing succeeds. |
| `PATCH /admin/knowledge/documents/{id}` | ops_manager | `{isPublished?, scope?}` → `KbDocumentRow` ("switch off" = unpublish; `scope` moves it between the helpline, campaign calls and both). At least one field is required. |
| `DELETE /admin/knowledge/documents/{id}` | ops_manager | 204 |
| `POST /admin/knowledge/ask` | agronomist | `{question, answer?: boolean, direction?: "inbound" | "outbound"}` (default `inbound`; the direction decides which documents are reachable) → `{passages: {documentTitle, section: string | null, snippet, score}[], retrievalMs, degraded: boolean, answer: null \| {text, totalMs, note: string | null}}`. `answer: true` runs the same agent the phone uses (a model call). |

---

## Inbound · centres, managers, stock, hand-over

```ts
type StockRow = { inventoryId: string; productName: string; variantName: string; price: number | null; isAvailable: boolean; stockQty: number | null };
type CentreRow = {
  id: string; code: string; name: string; nameHi: string | null;
  district: string; block: string | null; state: string; pincode: string | null;
  latitude: number | null; longitude: number | null;
  managerName: string | null; managerNumber: string | null; phone: string | null;
  openTime: string; closeTime: string; workingDays: string[];    // "08:00", ["mon",...,"sat"]
  isActive: boolean; openNow: boolean;
  stock: StockRow[];                                              // the first 6 items; full list on /stock
};
type TransferRules = {
  reasons: { key: string; label: string; enabled: boolean }[];    // from configuration; read-only
  fallbackNumber: string | null;                                  // editable
  ringTimeoutSeconds: number; whisperSeconds: number;
};
```

| Route | Needs | Body → Result |
|---|---|---|
| `GET /admin/centres` | centre_manager | → `CentreRow[]` (RLS-scoped) |
| `POST /admin/centres` | ops_manager | `{name, nameHi?, district, block?, state?, pincode?, latitude?, longitude?, managerName?, managerNumber?, phone?, openTime, closeTime, workingDays?}` → `CentreRow` (code generated: `NKSK-<DIST>-<n>`) |
| `PATCH /admin/centres/{id}` | ops_manager | any subset of the POST body plus `isActive` → `CentreRow` |
| `GET /admin/centres/{id}/stock` | centre_manager | → `StockRow[]` |
| `PATCH /admin/inventory/{id}` | centre_manager | `{isAvailable?, price?, stockQty?}` → `StockRow` (reaches the agent within 5 s) |
| `GET /admin/transfer-rules` | ops_manager | → `TransferRules` |
| `PATCH /admin/transfer-rules` | ops_manager | `{fallbackNumber}` → `TransferRules` |

---

## Data

| Route | Needs | Result |
|---|---|---|
| `GET /admin/data/storage` | ops_manager | Measured at most once a minute and served from that measurement in between; `countedAt` says when. `{countedAt, tables: {label, table, rows, bytes, note, indexedBy, partitioned: boolean}[], recordings: {bucket, region, retentionDays}, backups: {schedule, lastAt: string \| null, lastBytes: number \| null}}` |
| `GET /admin/data/connection` | super_admin | `{host, port, database, user, tls: boolean, poolSize, poolBusy, latencyMs, serverVersion, rowLevelSecurity: true}` — never the password. Answers in a millisecond: the service probes moved to `/admin/data/health` so a store that is down cannot hold the page. |
| `GET /admin/data/health` | ops_manager | `{redis: {ok, latencyMs}, storage: {ok, latencyMs, endpoint}}` — both probed at once, each given two seconds before it is reported as not answering. Fetch it beside the page rather than before it. |
| `POST /admin/data/connection/test` | super_admin | `{dsn}` → `{ok, latencyMs, serverVersion, error: string \| null}`. Tests only; the running connection changes through the deployment's environment (`DATABASE_URL`) and a restart, and the response says so. The DSN is not stored or logged. |

---

## Errors the panel must handle by code

| code | meaning | panel behaviour |
|---|---|---|
| `four_eyes` | approver created the campaign | explain, keep the button disabled |
| `campaign_blocked` | compliance gate failed | show `blockedBy` with human labels |
| `consent_required` | `consentAttested` was not true | keep the checkbox required |
| `telephony_unconfigured` / `vendor_unconfigured` | a vendor key is missing | show the remedy text verbatim |
| `not_found`, `forbidden`, `validation_error` | as named | |

---

# Additions of 3 September 2026

The sections below extend the contract above. Where a shape is repeated
here it replaces the earlier one. Everything is camelCase JSON, needs a
bearer token, and answers errors as `{"error": {code, message, remedy, context}}`.

## Overview — the first page

### `GET /admin/overview?range=today|7d|30d` — needs centre_manager

Everything a manager needs to know at a glance, RLS-scoped, computed from
`calls`, `campaigns`, `tickets`, `inventory` and `kb_documents`. Served
from a 15-second cache; `generatedAt` says when.

```ts
type Overview = {
  range: "today" | "7d" | "30d"; from: string; to: string; generatedAt: string;
  live: { calls: number; capacity: number };
  calls: {
    total: number; inbound: number; outbound: number;
    answeredByAgent: number;      // ended without a transfer
    transferred: number; missed: number;  // missed = ended with an error or with no reply from the agent
    testCalls: number;            // placed from the browser page (provider "simulator"); counted separately, never in the others
  };
  outcomes: { key: string; label: string; count: number }[];   // CallOutcome values, largest first
  speed: {
    firstReplyP50Ms: number | null; firstReplyP95Ms: number | null;
    replyP50Ms: number | null;    // every agent reply in range
    withinBudgetPct: number | null;   // replies under 1,200 ms, 0-100
  };
  outbound: {
    campaignsRunning: number; contactsDialled: number; reached: number;
    pressed1: number; pressed2: number; optedOut: number;
  };
  attention: AttentionItem[];   // what needs a person, most urgent first, max 12
  recent: RecentCall[];         // newest first, max 10; RecentCall as in the live snapshot plus isTest: boolean and summaryHi: string | null
  byHour: { hour: string; inbound: number; outbound: number }[];   // "09:00" .. in Asia/Kolkata; 24 rows for today, one per day ("Mon 1") for 7d/30d
  topQuestions: { intent: string; label: string; count: number }[];   // from call_turns tool_calls / intents, max 8
};
type AttentionItem = {
  kind: "ticket" | "transfer_failed" | "unanswered" | "stock_out" | "knowledge_pending"
      | "knowledge_failed" | "campaign_blocked" | "telephony" | "llm" | "worker";
  severity: "high" | "medium" | "low";
  title: string;                // English; the panel translates by kind
  detail: string | null; href: string | null;   // panel route to act on it, e.g. "/calls/<id>", "/inbound/knowledge"
  at: string | null; count: number;
};
```

`attention` includes: open tickets (high when a safety emergency), transfers that
failed, calls the agent could not answer (two `unknown` intents in one call),
stock-outs per centre, knowledge documents pending for more than 2 minutes or
failed, campaigns blocked by the gate, telephony not configured
(`kind: "telephony"`), and the background worker not seen for 3 minutes
(`kind: "worker"`).

### `GET /admin/telephony` — needs centre_manager

```ts
type TelephonyStatus = {
  provider: "exotel" | "twilio" | "plivo" | "simulator";
  mode: "live" | "simulator";   // simulator: calls are answered from the browser page, nothing is dialled
  configured: boolean; inboundNumber: string | null;   // the DID, masked to the last 4 digits
  remedy: string | null;        // what to set when not configured
  browserCallUrl: string | null;  // the worker's /dev/call page, development only
};
```

## Calls — the list moves next to the detail

### `GET /admin/calls` — needs centre_manager

Same query parameters as before plus `is_test` (`true`/`false`; default both).
`CallRow` gains `isTest: boolean`, `summaryHi: string | null`, `intents: string[]`,
`transferred: boolean`, `recordingAvailable: boolean`. The legacy `/admin/*`
routes the panel never called are removed.

### `GET /admin/calls/{id}` — unchanged, plus

`CallDetail` gains `isTest: boolean`, `intents: string[]` and
`stats: {turns, farmerTurns, agentTurns, cachedReplies, toolCalls, llmModel: string | null}`.
`recording.available` is true once the post-call job has stored the audio; in
development recordings live on the local disk (`STORAGE_BACKEND=local`) and
stream through the same route.

### `POST /admin/calls/{id}/summarise` — needs centre_manager

Re-runs the post-call summary for one call (a model call). → `CallDetail`.

## Flows — the inbound prompt is editable

`FlowDetail.systemPrompt` is the inbound persona. `POST /admin/flows/{id}/versions`
and `PATCH /admin/flows/{id}` accept `systemPrompt?: string` for inbound flows
(ignored for outbound; 422 when empty or over 12,000 characters). Publishing
works as before; the voice worker reads the published row on every call, so a
publish is live on the next call. `FlowDetail` gains `promptWordCount: number`
and `defaults: {systemPrompt, greeting, closing}` (the seed wording, so an
operator can restore it).

## Knowledge — status, notes, re-index

```ts
type KnowledgeStatus = {
  worker: { alive: boolean; lastSeenAt: string | null };   // the background worker heartbeat (Redis key uaagro:worker:heartbeat, refreshed every 60 s)
  documents: { pending: number; indexing: number; indexed: number; failed: number };
  chunks: number; embedded: number;
  embeddingModel: string; retrievalMs: number | null;   // last measured search latency
};
```

| Route | Needs | Body → Result |
|---|---|---|
| `GET /admin/knowledge/status` | agronomist | → `KnowledgeStatus` |
| `POST /admin/knowledge/documents` | ops_manager | as before, **or** JSON `{text, title, scope?, language?}` — a typed note (`docType: "text"`). |
| `POST /admin/knowledge/documents/{id}/reindex` | ops_manager | → `KbDocumentRow` (`ingestStatus: "pending"`) |

`KbDocumentRow` gains `sizeBytes: number | null`, `indexedAt: string | null` and
`wordCount: number | null`. When the queue cannot be reached, the API indexes the
document itself in the background (`ingestError` reads `"indexed inline"` while
it runs) — a document is never left silently pending.

## Centres — the primary centre and the map

`CentreRow` gains `isPrimary: boolean` (the head office; the helpline answers
stock and price for this centre when the caller's own centre is unknown, and
says so), `addressSpoken: string | null`, `services: string[]` and
`stockOuts: number`. `POST`/`PATCH /admin/centres` accept `isPrimary`,
`addressSpoken` and `services`. Exactly one active centre is primary; setting it
on another clears the previous one. `GET /admin/centres/nearest?lat=&lng=` (needs
centre_manager) → `{centre: CentreRow, distanceKm}`.

## Outbound — call these numbers now

### `POST /admin/dial` — needs ops_manager

```ts
Body: { numbers: string; flowId?: string; name?: string; consentAttested: true; consentNote?: string }
→ { campaign: CampaignSummary; imported: number; invalid: string[]; removedBy: {check: string; count: number}[]; started: boolean; blockedBy: string[] }
```

Creates a campaign named `name` (default "Quick dial <date time>"), runs the
compliance gate, approves it and starts the dialer in one step. The four-eyes
rule is waived for quick dials when `OUTBOUND_QUICK_DIAL_SELF_APPROVE=true`
(default in development, false in production — then `started` is false and the
campaign waits for approval). When the gate blocks, `started` is false and
`blockedBy` says why.

`Contact` gains `answerUrl: string | null`: in simulator mode the worker's
browser page answers the call (`/dev/call?answer=<contactId>`); null otherwise.
`Contact.status` gains `"ringing"`.

## Data — the client's MySQL database as a source

The platform's own database stays PostgreSQL (row-level security, vector search
and partitioning depend on it). A **data source** is the client's MySQL
database, read directly: stores, products and stock are pulled into the
catalogue on demand or on a schedule, and the agent answers from the copy.

```ts
type TableMap = { table: string; columns: Record<string, string> };   // our field → their column
type SourceMapping = { stores: TableMap | null; products: TableMap | null; stock: TableMap | null };
type SyncRun = {
  id: string; startedAt: string; finishedAt: string | null;
  status: "running" | "ok" | "failed";
  stores: number; products: number; stock: number;   // rows written
  error: string | null;
};
type DataSource = {
  id: string; name: string; kind: "mysql";
  host: string; port: number; database: string; user: string; tls: boolean;
  hasPassword: boolean;                     // the password itself is never returned
  mapping: SourceMapping; schedule: "manual" | "hourly" | "daily";
  lastRun: SyncRun | null; createdAt: string; updatedAt: string;
};
```

Our fields per table (the mapping screen offers exactly these; `*` = required):

- stores: `code`*, `name`*, `name_hi`, `district`*, `block`, `address`, `pincode`,
  `latitude`, `longitude`, `phone`, `manager_name`, `manager_phone`, `open_time`, `close_time`
- products: `sku`*, `name`*, `name_hi`, `category`* (`seeds | fertilisers | crop_protection | cattle_feed | tools_equipment`, or any text the sync maps by keyword), `brand`, `pack_size`* (e.g. "50 kg", "1 L" — number and unit are split), `mrp`*, `description`
- stock: `store_code`*, `sku`*, `qty`*, `price`, `is_available`

| Route | Needs | Body → Result |
|---|---|---|
| `GET /admin/data/sources` | ops_manager | → `DataSource[]` |
| `POST /admin/data/sources` | super_admin | `{name, host, port?, database, user, password, tls?, mapping?, schedule?}` → `DataSource` (the password is encrypted at rest with the platform data key; never logged) |
| `PATCH /admin/data/sources/{id}` | super_admin | any subset; `password` replaces → `DataSource` |
| `DELETE /admin/data/sources/{id}` | super_admin | 204 |
| `POST /admin/data/sources/test` | super_admin | `{host, port?, database, user, password, tls?}` → `ConnectionReport` (nothing stored) |
| `POST /admin/data/sources/{id}/test` | super_admin | → `ConnectionReport` |
| `GET /admin/data/sources/{id}/tables/{table}/columns` | super_admin | → `{columns: {name, type}[], sample: Record<string, unknown>[]}` (first 5 rows, for the mapping screen) |
| `POST /admin/data/sources/{id}/sync` | ops_manager | → `SyncRun` (202; runs in the background; poll `/runs`) |
| `GET /admin/data/sources/{id}/runs` | ops_manager | → `SyncRun[]` newest first, max 20 |

```ts
type ConnectionReport = { ok: boolean; latencyMs: number | null; serverVersion: string | null; tables: {name: string; rows: number | null}[]; error: string | null };
```

Sync semantics: stores upsert by `code`, products by `sku` (one variant per pack
size), stock by (store, variant). Nothing is deleted by a sync; a product
missing from the source keeps its last known stock and is reported in the run.
Every run is audited.

## Security notes for the panel

- `GET /metrics` on both services needs the internal token (`x-internal-token`) outside development.
- `/docs` and `/openapi.json` exist only in development.
- Every response carries `Cache-Control: no-store`.
- Data-source writes, prompt publishes and quick dials are in the audit log.
