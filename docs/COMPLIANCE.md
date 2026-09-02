# Compliance

**Regulatory position verified 31 August 2026** (§24). This area moves; re-verify
before go-live and on a quarterly cadence thereafter, and update the date above.

Everything here is implemented as a **platform control**, not documentation. §18
is explicit that an operator must not be able to override the calling window or
the DND scrub, so those live in the compliance gate and the dialer rather than in
a policy PDF.

---

## 1. TRAI / TCCCPR — commercial calling

### Designated number series — verified, with one correction

| Series | Purpose | Available to UA Agro? |
|---|---|---|
| **140xx** | Promotional calls, **entities of any sector** | ✅ Yes — this is the CLI for every promotional campaign |
| **1600xx** | Service and transactional calls by entities regulated by **RBI, SEBI, IRDAI or PFRDA**, and government-to-citizen communication | ❌ **No** |

> **Correction to the specification.** §2 and §13.1 imply UA Agro would use "the
> appropriate designated series" for transactional and service campaigns. The
> 1600 series is restricted to entities regulated by RBI, SEBI, IRDAI or PFRDA,
> plus government departments. **An agri-input retailer does not qualify.**
>
> Practical consequence: `OUTBOUND_CLI_TRANSACTIONAL` cannot be a 1600-series
> number. Genuine transactional calls (an order-ready notification to a farmer
> who bought something) go out on a normal number; anything promotional must use
> 140. The compliance gate enforces the 140 requirement for promotional
> campaigns and does **not** assert a 1600 prefix for transactional ones, which
> is the correct behaviour given the above.

Also confirmed:

- Entities wanting a 140-series number must **register with their telecom service
  provider** under the TCCCPR framework.
- Subscribers can allow or block 140-series calls per sector via the **DND / NCPR
  registry**. A prior customer relationship does not exempt a DND-registered
  number.
- Calls from 140 and 1600 **cannot be blocked or labelled as spam by
  call-filtering apps** — but 140 *can* be DND-registered by the subscriber, which
  is the control that actually applies to us.
- TRAI issued a clarification on the 1600/140 framework on **10 July 2026**,
  after media confusion. The position above reflects that clarification.

### Enforced in the platform

| Rule | Where | Overridable by an operator? |
|---|---|---|
| 140-series CLI for promotional campaigns | Compliance gate, before approval | **No** |
| DND / NCPR scrub before every campaign | Compliance gate | **No** |
| Internal DNC — permanent, written synchronously during the call | `dnd_status.internal_dnc` | **No** |
| 09:00–21:00 window, recipient local time, checked **at dial time** | Dialer | **No** |
| Consent present and unexpired | Compliance gate | **No** |
| Max attempts per contact, minimum gap between attempts | Dialer | Configurable within caps |
| Four-eyes approval — creator cannot approve their own campaign | Database CHECK constraint + service layer | **No** |
| DLT entity ID and registered template present | Compliance gate | **No** |

The four-eyes rule and the "approved campaign must carry a CLI" rule are
**database constraints**, not just service-layer checks:
`ck_campaigns_approver_differs_from_creator`,
`ck_campaigns_running_campaign_requires_approval`,
`ck_campaigns_approved_campaign_requires_cli`. A service bug cannot route around
them.

### Consent is not permanent

`consent_records.expires_at` is **mandatory** for promotional consent types —
enforced by `ck_consent_records_promotional_consent_expires`. The compliance gate
excludes expired consent automatically. Default validity is 90 days
(`compliance.consent_validity_days`); confirm the current TCCCPR figure with
UA Agro's telecom counsel before go-live, since this is the parameter most likely
to have moved.

---

## 2. DPDP Act 2023

| Requirement | Implementation |
|---|---|
| Recording disclosure at call start, in the caller's language | Cached disclosure phrase, spoken on every call |
| Purpose-limited consent with expiry | `consent_records`, typed by purpose |
| Data-subject access / correction / erasure | Endpoints, not a policy page. Erasure cascades to recordings, transcripts, embeddings and next-cycle backups, and writes a tombstone |
| Retention | Recordings 180 days, transcripts 730 days, both configurable; automated purge with a dry-run mode |
| Breach notification | Runbook procedure |
| Data-processing register | §5 below |

Phone numbers are never stored in plaintext: HMAC-SHA256 under a server-side
pepper for the lookup index, AES-256-GCM under a KMS-wrapped data key for the
retrievable value, and `phone_last4` for display so list views never decrypt.
Decryption is a privileged, audited operation.

---

## 3. Agrochemical advisory — the Insecticides Act framework

The control that protects UA Agro if a recommendation is ever disputed is that
**every dose the agent speaks traces to an agronomist-signed row**.

Implemented as three independent barriers, because one would be a single point of
failure on the highest-severity path in the system:

1. **A partial index.** `ix_crop_reco_servable` covers only rows where
   `approval_state = 'approved' AND deleted_at IS NULL`. An unapproved row is not
   in the index the agent's query reaches for.
2. **A database CHECK.** `ck_crop_recommendations_approved_requires_approver`
   makes `approved` without a named approver and timestamp impossible to store.
   A second CHECK,
   `ck_crop_recommendations_crop_protection_needs_phi_and_precaution`, refuses to
   approve a crop-protection recommendation that lacks its pre-harvest interval
   and at least one precaution.
3. **A test.** Unapproved rows are proven unservable rather than assumed to be.

`products.cib_registration_no` is required for every insecticide, fungicide,
herbicide and PGR, enforced by
`ck_products_agrochemical_needs_cib_registration` — an unregistered agrochemical
cannot exist in the catalogue, so it cannot be recommended.

Seeded advisory content is **all `draft`**. Nothing in the seed is servable, by
design (KB §11).

### The barriers now extend past the database (Phase 3)

Three more, at the layers a caller actually reaches:

4. **The tool refuses.** `recommend_for_crop` raises `UnapprovedAdvisoryError`
   when no approved row exists. It does not widen the search, fall back to a
   similar crop, or defer to the model — those are the three ways this control
   would be lost while still looking like it worked. `calculate_dose` refuses a
   recommendation id it cannot load as approved, so a fabricated id yields no
   dose.
5. **A dose cannot come out of a document.** `search_knowledge` flags any
   retrieved chunk carrying a quantity and tells the agent to call
   `recommend_for_crop` instead. §9's Tier 2 is prose; a document is not an
   approval, and the chunker will not split a dosage row away from its
   pre-harvest interval and precaution.
6. **An approved answer carries its precaution.** The tool returns `phi_days`
   and `precaution_hi` alongside the dose and sets `must_speak_precaution`, so
   the safety text is in the payload rather than left to the model to remember.

A symptom is also not a diagnosis. `crop_problems.requires_clarification` makes
`recommend_for_crop` return a question instead of a recommendation for ambiguous
reports — yellowing leaves are nitrogen, water or disease (KB §6), and
prescribing for a guess is what the approval workflow exists to prevent.

### Knowledge-base content is not servable on upload

`uaagro-kb ingest` never publishes; `uaagro-kb publish` requires a named
approver, and retrieval filters on `is_published`. Editing a published document
**clears its approval** — the reviewer signed for the text that was there, not
for what replaced it. §9 requires the admin panel to show an unapproved-content
banner until the queue is empty; `uaagro-kb status` reports the same count for
anyone working without the panel.

---

## 4. Compliance dashboard

Ships in Phase 8. Surfaces: consent coverage, DND scrub freshness, calls outside
the calling window (must read zero), complaint counters per campaign, retention
job status, unapproved advisory row count, and the last verification date for
each rule on this page.

---

## 5. Data-processing register

Every third party that receives farmer audio or text. Zero-retention / no-training
must be requested in writing from each and the setting recorded in
`docs/SECURITY.md` with the date.

| Processor | Receives | Purpose | Retention setting | Confirmed |
|---|---|---|---|---|
| Exotel | Call audio, caller MSISDN | Telephony transport | — | ☐ not yet requested |
| Deepgram | Call audio (hi/en path) | Speech recognition | — | ☐ not yet requested |
| Sarvam AI | Call audio (Indic paths), response text | Recognition and synthesis | — | ☐ not yet requested |
| Anthropic | Turn text, tool results | Response generation | — | ☐ not yet requested |
| Meta / BSP | Farmer MSISDN, template parameters | WhatsApp delivery | — | ☐ not yet requested |
| AWS (ap-south-1) | Recordings, transcripts, all data at rest | Hosting | Under our control | — |

**Every row is outstanding.** These are written requests UA Agro must make and
retain; they are not something the build can satisfy on its own. Data residency
matters here: Sarvam is India-hosted, Deepgram is not, and the DPDP position on
cross-border transfer should be reviewed with counsel before the Hindi path is
committed to a non-Indian processor.

---

## 6. Open items before go-live

1. **Confirm the current TCCCPR consent-validity window** and update
   `compliance.consent_validity_days`.
2. **Register the 140-series CLI** with the telecom service provider and record
   the DLT entity ID and template IDs.
3. **Obtain zero-retention confirmations** from all five external processors.
4. **Confirm cross-border transfer position** for Deepgram under DPDP.
5. **Populate agronomist-approved crop recommendations** for the four
   highest-volume crops. Until then, restrict the agent to availability, price,
   composition, centre information and service booking, and route every dosage
   question to a human (KB §11).
6. **Confirm the WhatsApp service-window pricing change** taking effect
   1 October 2026 — see `COST_MODEL.md`. Compliance-neutral, but it changes the
   economics of the recommended human-reply path.

---

## Sources

- [TRAI clarifies the 1600 and 140 series framework (SCC Online, 18 Jul 2026)](https://www.scconline.com/blog/post/2026/07/18/trai-clarifies-1600-and-140-series-number-framework/) — checked 31 Aug 2026
- [TRAI Press Release No. 91/2026](https://www.trai.gov.in/sites/default/files/2026-07/PR_No91of2026.pdf) — checked 31 Aug 2026
- [TRAI clarification on designated promotional and transactional number series (MediaNama)](https://www.medianama.com/2026/07/223-trai-releases-clarification-designated-promotional-transactional-number-series/) — checked 31 Aug 2026
