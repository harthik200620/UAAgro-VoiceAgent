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
