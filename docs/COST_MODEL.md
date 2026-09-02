# Cost model

**Vendor rates verified 31 August 2026.** §24 requires this check in Phase 1 and
a daily job thereafter; the live rate card lives in the `vendor_rates` table and
is seeded from `config/defaults.yaml`.

Every rate below carries its source and the date it was checked. A rate with no
`verified_at` in the database is one the daily job has not yet confirmed, and the
compliance dashboard shows that gap rather than implying freshness.

---

## 1. Verification results, 31 August 2026

### Speech — confirmed, no change from the specification

| Item | Specification said | Verified | Status |
|---|---|---|---|
| Sarvam STT | ₹30/hour (₹0.50/min) | ₹30/hour, billed per second | ✅ unchanged |
| Sarvam TTS `bulbul:v3` | ₹30 / 10,000 chars | ₹30 / 10,000 chars | ✅ unchanged |
| Deepgram Flux multilingual | $0.0078/min PAYG, $0.0068 Growth | $0.0078 / $0.0068 | ✅ unchanged |

Newly observed and **not** in the specification:

- **Sarvam STT with diarization is ₹45/hour.** Not needed here — a helpline call
  has one caller — but it is what a future "who said what" feature would cost.
- **Sarvam also sells STT+Translate at ₹30/hour** and translation at ₹20/10K
  chars. Relevant later if the admin panel ever shows English transcripts of
  Hindi calls without paying the LLM to translate them.
- **Deepgram Flux *English* is on a promotional rate** — $0.0065/min against a
  regular $0.0077. The multilingual tier we use is not discounted, so this does
  not affect the Hindi path, but an English-only deployment would be cheaper
  than the model below suggests until the promotion ends.
- **Deepgram Nova-3 multilingual is $0.0058/min promotional** ($0.0092 regular),
  cheaper than Flux multilingual. It does **not** carry Flux's semantic
  end-of-turn detection, which is the entire reason §5.1 selects Flux. Trading
  it away to save ₹0.18/min would cost the eager-EOT overlap that §7 depends on.

### WhatsApp — one material change inside the build horizon

| Category | Rate (INR, ex-GST) | Status |
|---|---|---|
| Marketing | ₹0.8631 | ✅ matches spec |
| Utility | ₹0.1150 | ✅ matches spec |
| Authentication | ₹0.1150 | — |
| Service (free-form reply in the 24-hour window) | ₹0 **until 30 Sep 2026** | ⚠️ see below |

> **⚠️ The free service window ends on 1 October 2026 — 31 days from this
> verification.**
>
> Multiple independent industry sources report that from 1 October 2026:
> - free-form **service replies** inside the 24-hour customer service window
>   become chargeable (free since November 2024);
> - **utility templates** sent inside that window become chargeable (free from
>   1 July 2025 to 30 September 2026);
> - service messages will price at the same rate as utility/authentication for
>   the country, with **no volume discount**;
> - Meta was expected to publish final country rates by **1 September 2026**.
>
> **Confidence: strong but not first-party.** Several independent sources agree
> on the dates and mechanics. Meta's own pricing page, fetched on 31 August
> 2026, described service and in-window utility messages as free and did not
> mention the change — that page may simply lag. **Action for the operator:
> confirm with your BSP before go-live, and re-check Meta's rate card after
> 1 September 2026 when final rates are published.**
>
> **Why this matters to the build:** §14 recommends routing an inbound WhatsApp
> reply into a `tickets` row and having a human answer inside the free window
> rather than sending a fresh template. That is sound advice today and becomes a
> *paid* path in a month. The design does not change — a human reply is still
> right — but the cost model must stop treating it as free. `vendor_rates`
> already carries `meta / wa_service` as a row, seeded at ₹0, so this is a rate
> update rather than a code change.

Add 18% GST to all Indian WhatsApp rates, plus any BSP markup (commonly 10–30%).

### Telephony

Per-minute Indian telephony rates are commercially negotiated and not reliably
published. The seeded figures (₹0.60/min DID inbound, ₹1.80 toll-free inbound,
₹0.70/min outbound) are the specification's own estimates and are marked
**unverified** in `vendor_rates`. Replace them with UA Agro's actual Exotel
contract rates before the cost dashboard means anything.

---

## 1b. The engines changed, 2 September 2026

The helpline moved to **Soniox** for recognition and **Raya Bakbak** for
synthesis, and the LLM now has a direct path to Anthropic alongside the LiteLLM
gateway. What that does to §8:

| Item | Was | Now | Effect |
|---|---|---|---|
| STT | Sarvam ₹0.50/min, or Deepgram $0.0078/min | Soniox **$0.12/hr = $0.002/min** | ₹0.176/min against ₹0.50 — roughly a **third** |
| TTS | Sarvam `bulbul:v3` ₹30 / 10,000 chars | Bakbak — **no published rate** | **unknown** |
| LLM | LiteLLM → Claude Haiku | same model, one fewer hop | no rate change |

**Soniox is checked in.** $0.12/hour for realtime STT, verified 2 September 2026
on the vendor's pricing page, seeded as $0.002/min in `cost.seed_rates`. On the
worked example below that is ₹0.53 for three minutes against Sarvam's ₹1.50.

**Bakbak is not, and is seeded at zero deliberately.** Raya publishes a bundled
per-minute price for its agent platform, which is a different product; there is
no per-character or per-minute rate for Bakbak TTS on its own. Seeding a guess
would be worse than seeding nothing: §8's per-call total would look complete and
be wrong, and `MAX_COST_PER_CALL_INR` would silently stop guarding the largest
line item on the bill. The row exists at 0.0 with `verified_at` unset so the
daily job reports it as unconfirmed and the number stays visibly missing.

**This is the one thing to get from Raya alongside the API key.** TTS is the
largest controllable line item in this model — larger than STT and far larger
than the LLM — so until that rate is known, the per-call cost printed anywhere
in this system is a lower bound, not a total.

---

## 2. Worked example — 3-minute Hindi inbound call

Reproduced by `test_cost_breakdown_reproduces_the_specification_worked_example`,
so a regression here fails the build rather than surfacing on an invoice.

| Component | Basis | Cost |
|---|---|---|
| Telephony (DID inbound) | 3 min × ₹0.60 | ₹1.80 |
| STT (Sarvam) | 3 min × ₹0.50 | ₹1.50 |
| TTS (`bulbul:v3`) | ~950 chars × ₹0.003 | ₹2.85 |
| LLM (small fast model, cached) | ~18k in / 0.8k out | ₹0.20 |
| Compute + storage | amortised | ₹0.30 |
| **Total** | | **₹6.65 → ₹2.22/min** |

Swapping Sarvam STT for Deepgram Flux multilingual adds **₹0.19/min**
($0.0078 × 88 = ₹0.686 against ₹0.50). Asserted in
`test_deepgram_premium_over_sarvam_matches_the_specification`.

The example above is still written against Sarvam and `bulbul:v3`, because it
reproduces the specification's own worked example and a test asserts on it.
On the engines actually configured today the STT line falls to **₹0.53**
(3 min × $0.002 × 88) and the TTS line is **unknown** — see §1b. It is left as
written rather than half-updated: a table with one real number and one guess
reads as a total, and this one would not be.

**TTS is the largest controllable line item** — larger than STT and far larger
than the LLM. This inverts most intuitions and dictates the optimisations:

1. **Cache aggressively.** Greeting, holds, closings, transfer notice,
   disclosure and the top ~40 sentences are synthesised once. Removes 15–25% of
   TTS characters on a typical call.
2. **Be brief.** Every unnecessary word is billed twice — once as TTS characters
   and once as call duration. The 35-word answer cap in `config/defaults.yaml`
   is a cost control as much as a UX one.
3. **Answer cache** (§9 Tier 3) skips the LLM entirely and, on a cached
   utterance, the TTS too.
4. **Outbound uses a local 140-series DID, never toll-free.**
5. **Reject spam in 6 seconds.** ₹0.06 against ₹6.65 for a full call.

---

## 3. Guards

Both enforced in code, both configurable:

- `MAX_COST_PER_CALL_INR` (default ₹25) — a call crossing this is closed with an
  apology and a ticket rather than allowed to run away.
- `DAILY_SPEND_CAP_INR` (default ₹5,000) — pauses outbound campaigns and alerts.

`CostBreakdown` never raises. An unknown vendor rate contributes ₹0 and is
recorded in `unpriced`, because a metering bug must not be able to drop a call.

---

## 4. Latency measurements outstanding

This applies to Soniox too, and more urgently: the Hindi path now depends on it
for both recognition and the end-of-turn decision, so its round-trip time from
Mumbai is inside §7's turn budget twice over. `scripts/check_vendors.py` prints
the socket-accept time, which is a floor on it and not a substitute for the
§19.2 measurement.

§7.1 requires measuring **real Deepgram round-trip time from Mumbai** before
committing the Hindi path to Flux. Deepgram is not India-hosted; Sarvam is. If
the measured RTT pushes the Hindi path past the §7 budget, the decision is to
move Hindi to Sarvam STT and keep Flux for English.

**Not yet measured** — it needs a live `DEEPGRAM_API_KEY` and a host in
`ap-south-1`. This is a Phase 2 gate item and is recorded here so the decision
is made on a number rather than an assumption.

---

## Sources

- [Soniox pricing](https://soniox.com/pricing) — checked 2 Sep 2026
- [Bakbak / Raya API docs](https://docs.litwizlabs.com) — checked 2 Sep 2026; no TTS rate published
- [Sarvam AI pricing](https://docs.sarvam.ai/api-reference-docs/pricing) — checked 31 Aug 2026
- [Deepgram pricing](https://deepgram.com/pricing) — checked 31 Aug 2026
- [WhatsApp Business Platform pricing (Meta)](https://developers.facebook.com/docs/whatsapp/pricing) — checked 31 Aug 2026
- [WhatsApp Business API pricing in India 2026 (MyOperator)](https://myoperator.com/blog/whatsapp-business-api-pricing-india-2026) — checked 31 Aug 2026
- [WhatsApp service message pricing changes, October 2026 (SendPulse)](https://sendpulse.com/blog/whatsapp-service-message-pricing) — checked 31 Aug 2026
- [WhatsApp service messages and the 24-hour window (YCloud)](https://www.ycloud.com/blog/whatsapp-service-messages-24-hour-window-pricing) — checked 31 Aug 2026

---

## 5. Measured latency — tools and retrieval (Phase 3, 31 August 2026)

Measured on the embedded Postgres 16.2 used by the test suite, on a Windows
development laptop. Not `ap-south-1` on RDS, so treat these as an upper bound on
query cost and a lower bound on network cost — the real deployment adds a network
hop and removes a laptop.

| Tool | p50 | p95 | §6.3 budget |
|---|---:|---:|---|
| `lookup_farmer` | 6.0 ms | 7.4 ms | 150 ms |
| `search_products` | 2.2 ms | 3.3 ms | 150 ms |
| `check_availability` | 3.7 ms | 4.6 ms | 150 ms |
| `get_product_details` | 1.8 ms | 2.5 ms | 150 ms |
| `find_nearest_centre` | 2.5 ms | 3.8 ms | 150 ms |
| `get_order_status` | 3.6 ms | 5.5 ms | 150 ms |
| `recommend_for_crop` | 3.5 ms | 4.3 ms | 150 ms |

Two things these numbers hide, both worth stating.

**Cold start is 40× the warm p95.** The first `lookup_farmer` of a worker's life
measured ~300 ms and the next four decayed to ~165 ms before settling at 6 ms.
That is connection establishment and query-plan compilation, and it lands on the
first caller after every deploy. §7.5's warm-up is what absorbs it; the numbers
above are taken after warming, and a benchmark that did not warm would have
blamed the queries for it.

**`lookup_farmer` was over budget until it stopped being four round trips.**
Orders, tickets, call count and consent are four independent lookups on the same
farmer. asyncpg serialises queries on a connection, so four awaits are four
sequential network hops. Issued as one statement with four scalar subqueries,
they cost one.

### Embedding cost

`multilingual-e5-base` through ONNX Runtime on CPU. Measured, and the two
directions differ by a factor of forty:

| Operation | Tokens | Single-threaded |
|---|---:|---:|
| One query (`query:` prefix) | ~15 | **52 ms p50, 58 ms p95** |
| One passage (`passage:` prefix) | ~500 | **~2,000 ms** |
| The 18-chunk seed corpus | ~7,000 | **35 s** |

Cost is roughly linear in tokens, so the gap is chunk length rather than
anything pathological about batching. Two consequences:

**The query embedding is on the turn path.** At ~55 ms it is a real charge
against §7's 1,500 ms tool-call ceiling — comparable to the entire TTS
time-to-first-byte allowance. This is why `search_knowledge` tries the Tier-3
answer cache before retrieval, and part of why §9's cross-encoder reranker is
not enabled by default: it would add twenty more inferences on top.

**Ingestion must not use the worker's thread setting.** A live worker pins ONNX
to one intra-op thread so the encoder cannot make the 20 ms media loop jitter.
That setting turns a thousand-chunk corpus into a half-hour run, so `uaagro-kb`
and the test fixture raise it to `cpu_count() - 1`. The two paths want opposite
things from the same model, so the thread count is a constructor argument rather
than a constant.

There is no per-token vendor charge: the model runs in-process. The cost is CPU
on the worker and 1.1 GB of memory-mapped weights per host — which is also an
argument for keeping the reranker out until an eval says it earns its place.
