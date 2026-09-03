# Architecture

Written as the system is built (§22), not after. Sections marked **Phase N**
describe what exists at that phase; nothing here describes code that has not been
written.

---

## 1. Shape

```
PSTN ──▶ Exotel Voicebot applet ──ws──▶ voice-worker ──▶ Postgres
                                            │                ▲
                                            └── Redis        │
                                                             │
                    admin (Next.js) ──▶ api (FastAPI) ───────┘
```

Three properties drive every other decision:

**The voice worker is stateless per call.** Durable state is in Postgres,
ephemeral live state in Redis keyed by `call_id`. Killing a worker drops only the
calls on it, and those calls' records are already written — the `calls` row is
created at INIT and updated incrementally, so a crash leaves a queryable
interrupted call rather than nothing (§1 N8).

**The control plane is never in the audio path.** The API and the voice worker
reach Postgres through separate engines. The worker's carries a hard
`statement_timeout` of 120 ms, so a query that would exceed the latency budget is
killed by Postgres rather than allowed to stall a live call (§4.1, §7.6).

**Everything vendor-facing is behind an adapter.** Swapping Exotel for Plivo, or
Sarvam for another TTS, is a config change plus one adapter file.

---

## 2. Repository

```
apps/
  voice-worker/   media path: telephony transport, speech pipeline, tools
  api/            control plane: auth, RBAC, admin surface
  worker/         background jobs (Phase 6+)
  admin/          Next.js control panel (Phase 7)
packages/
  domain/         shared vocabulary: enums, errors, units, money, config
  db/             models, migrations, seeds, crypto, audit
  evals/          voice evaluation harness (Phase 3+)
infra/docker/     local stack
config/           non-secret defaults, including the language routing table
```

A `uv` workspace. `packages/domain` imports nothing from the rest of the system
and contains no I/O — it is the vocabulary everything else speaks. `packages/db`
depends only on `domain`. Both apps depend on both packages and not on each
other; when the API briefly imported the logger from the voice worker, that was a
mistake and the logger moved to `domain`.

---

## 3. Decisions worth the reader's time

### Enums are text with CHECK constraints, not native Postgres enums

Adding a value to a native enum takes an `ALTER TYPE`. At 80 centres with a 5–10×
seasonal peak (§3), a lock during Rabi sowing is not acceptable. A CHECK
constraint is replaced without a table rewrite.

### The call tables are partitioned monthly, and carry no foreign key to `calls`

`calls`, `call_turns`, `call_events` and `dtmf_events` are range-partitioned on
`started_at`. Two consequences the schema lives with:

- Postgres requires the partition key in every unique constraint, so the natural
  key is `(call_ref, started_at)` rather than `call_ref` alone. Lookup by
  provider SID during a live call goes through Redis, which already holds
  ephemeral per-call state.
- A foreign key to a partitioned table would have to reference `(id, started_at)`
  and would be checked on every insert — from the audio path, on the largest
  tables in the system. Referential integrity is maintained by the writer, which
  creates the parent row first.

An insert with no matching partition **fails**, and that failure would land in the
audio path. The initial migration creates a window of ±3 months and
`make db-partitions` rolls it forward. A default partition is deliberately not
used: it silently absorbs out-of-range rows and then blocks creating the
partition that should have held them.

### RLS is `FORCE`d, and the application owns nothing

Two things decide whether centre scoping actually holds:

1. A table's owner **bypasses RLS** unless the table is `FORCE`d.
2. The application must not connect as the owner.

Both are done. `uaagro_app` is granted DML and owns nothing; migrations run as a
separate owner role. Policies read transaction-local GUCs (`app.user_id`,
`app.role`, `app.centre_ids`) bound with `set_config(..., true)` so they cannot
leak into the next request that borrows the same pooled connection.

`call_turns` carries a denormalised `centre_id` purely so its policy is a column
comparison. Resolving the centre through a subquery against `calls` would put a
correlated lookup on every row of the largest table in the system.

A request that skips authentication also skips the GUC binding, so RLS returns
**nothing** rather than everything. The failure mode is an empty result, not a
leak.

### Phone numbers have two representations, and neither is plaintext

`phone_hash` — HMAC-SHA256 under a server-side pepper — is deterministic, carries
a unique index, and is the *only* lookup key. `phone_enc` — AES-256-GCM under a
KMS-wrapped data key — is reversible, and decrypting is a privileged audited
operation the agent never performs. `phone_last4` exists so list views never
decrypt at all.

The envelope authenticates its own header, so a KMS ciphertext cannot be swapped
for a local-key one. The pepper is effectively permanent: rotating it orphans
every farmer row. Rotating the data key is supported, because the wrapped key
travels inside each ciphertext.

`Msisdn.__str__` returns the masked form, so an accidental f-string cannot leak a
number.

### The audit log is a hash chain over a sequence, not over time

`row_hash = SHA256(prev_hash ‖ canonical_payload)`. Ordering is by a Postgres
sequence rather than by timestamp, because two audit events can share a
millisecond and a chain needs an unambiguous predecessor. Appends take a
transaction-scoped advisory lock so two writers cannot fork the chain. The payload
is canonical JSON — sorted keys, fixed separators, UTC-normalised timestamps — so
a dict-ordering change or a server in a different timezone does not read as
tampering. `uaagro_app` has INSERT but not UPDATE or DELETE on the table.

### Language routing is a table, not control flow

`config/defaults.yaml` carries the whole §5.1 matrix. Adding a language is an
edit there. Bhojpuri and Awadhi delegate to the Hindi route via `routes_to` but
keep their own declared quality tier, so the admin panel reports what the caller
actually gets rather than borrowing Hindi's tier A.

Because it is a table, replacing the engines behind it was an edit to that
table plus two adapters — which is the claim §5.1 makes, and this is the first
time anything has tested it.

**Verified 31 August 2026:** Deepgram Flux covers exactly 10 languages —
English, Spanish, French, German, Hindi, Russian, Portuguese, Japanese, Italian,
Dutch. Marathi and Malayalam are not among them. That absence is what forced the
original split: Flux for hi/en, Sarvam plus a *separate* local turn detector for
everything else — two vendors, two failure modes, and a measurably different turn
feel between languages.

Pipecat **Smart Turn v3.1** (3 December 2025) is a drop-in ONNX replacement for
v3.0 — same 23 languages including Hindi and Marathi, materially better accuracy
(English 88.3% → 94.7% on the 8 MB variant). It remains the fallback for any
route left on a transcribe-only recogniser.

### The split is gone, and the tier letters deliberately did not move

**Verified 2 September 2026.** Soniox `stt-rt-v5` recognises every language this
helpline serves and carries its own semantic endpointing, so recognition and
end-of-turn come from one engine on every route. Malayalam, which had no
semantic turn model at all and endpointed on silence, now has one.

That is a real improvement and it is **not** a reason to relabel Malayalam tier
A. §5.1's tier is a *measured* claim about what a caller gets, and nothing has
been measured against these engines. Raising a letter on a vendor's
documentation is exactly the dishonesty the tier exists to prevent — so the
letters hold, and each `tier_note_en` now says which engine serves the language
and that the letter is pending §19.2's per-language turn-detection eval. The
notes changed because they had become false. The claims did not, because nothing
new is yet known.

Odia stays on Sarvam. Soniox's coverage of it is not confirmed, and routing a
language to an engine because the neighbouring ten are supported is how a
helpline finds out in production that it cannot hear a caller. Punjabi is
recognised by Soniox but spoken by Sarvam — Bakbak has no Punjabi voice.

Two consequences worth naming.

**Soniox publishes a commit, not a probability.** Flux emits an eager
end-of-turn at 0.45 and lets generation start while the caller is still
finishing; that overlap is roughly 300 ms on the ~80% of turns that commit,
which is most of the margin in §7's 1,200 ms budget. Soniox has no equivalent.
What it does publish is *text that stops changing*, so the eager signal here
is a timer armed when the words last changed: `eager_after_final_ms` (100 ms)
once everything is final, `eager_after_stable_ms` (500 ms) while text is still
provisional, because a pause between two words looks the same for a moment.
It is a heuristic and the code says so rather than dressing it as a vendor
signal. It is safe because it only ever triggers speculation: when the caller
carries on, the adapter emits `TURN_RESUMED`, §5.2 requires the speculative
generation be cancelled, and a false eager costs tokens rather than reaching
anyone's ear. Measured 3 September against Soniox at endpoint level 3, it
buys little: Soniox holds most of a short utterance's tokens until the
endpoint itself, so there is rarely stable text to speculate on. The
machinery stays because a longer utterance does produce it.

**§5.2 asks for 8 s of patience for a slow speaker; the vendor accepts 3 s.**
`max_endpoint_delay_ms` is clamped to the vendor's range rather than sent as
8000 and rejected at connect time, which would take the call with it. A knowing
reduction, not an oversight.

The pairing check that refuses a self-deciding recogniser behind a local
detector was extended: a vendor-decided strategy must also *match* its
recogniser. Crossing `soniox` with `flux_semantic` changes no audio at all — the
local detector stands down either way — so nothing raises and nothing sounds
wrong. What it changes is the engine name §5.1 puts next to a language's quality
tier, which is the fact someone will later use to judge whether that tier is
honest.

### Bakbak returns float samples, and that is not a resample

Raya's Bakbak synthesises at 8 kHz directly, so §23-8 holds on the way out: the
telephony leg never resamples. But its *streaming* endpoint returns PCM F32LE
whatever `codec` asks for — the format list belongs to the batch endpoint — and
the worker's audio bus is signed 16-bit. So the adapter converts.

That distinction matters and is easy to blur. Changing 32-bit floats to 16-bit
integers at an unchanged 8 kHz is a sample-*format* conversion; §23-8 forbids
*resampling*, which is the operation that costs quality and CPU. The rate is
asked for, and a rate the vendor will not produce is refused rather than fixed
up afterwards.

The conversion has two failure modes that both still *play*, which is why
`tests/test_bakbak_tts.py` asserts on sample values and not byte counts: a width
or endianness mistake gives a voice at the wrong pitch, and dropping the
sub-4-byte remainder of each chunk deletes one sample at every boundary — a
rising click track over a whole answer, with nothing downstream to raise.

Voice ids are account-scoped UUIDs with no default, so an unset one fails loudly
naming `BAKBAK_VOICE_HI`. §5.3 makes the choice a bake-off over a real phone
line; a documentation example quietly becoming production's voice is what that
rule exists to prevent. `scripts/bakbak_voices.py` lists the account's voices,
and `--sample` renders one Hindi line each at 8 kHz to listen to.

### Exotel and Twilio get separate serializers

Exotel carries base64 **linear16**; Twilio and Plivo carry **µ-law**. A shared
class with a codec flag is exactly how that gets conflated, and the result is
white noise on a live farmer's phone (§23-8). The codec is a class attribute, not
a constructor argument. Nothing in the pipeline resamples: the TTS is asked for
8 kHz in the provider's own codec.

### The telephony simulator is a Phase 1 deliverable

It speaks the real protocol, paces frames in real time (one 20 ms frame every
20 ms), injects DTMF and can abort the TCP transport to reproduce a dropped GSM
line. Blasting the file as fast as the socket accepts it would hide every timing
bug the pipeline can have, and timing bugs are the ones that matter.

A dropped line is classified as **interrupted**, not failed. On rural GSM it is a
routine outcome; calling it a system failure would poison the failed-call rate the
§15 dashboard shows and send an operator chasing an incident that never happened.

### The audio loop never blocks on I/O

Per-call database writes go through a bounded queue drained by a background task.
A slow query delays persistence, not the caller's audio. If the queue fills,
events are dropped **loudly** rather than growing memory without bound during a
peak.

### Tools are the only route to a fact, and refusing is a first-class result

Every tool in §6.3 goes through one contract: validate, run under a 400 ms
timeout, retry once, trim the payload. Three of its properties are load-bearing.

A malformed argument is a **refusal**, never a coercion. Turning `"do bori"` into
`2` at the tool boundary would put an invented quantity behind the audit trail
§18 depends on, and nothing downstream could tell it from a real one.

Nothing raises into the audio loop. A tool failure becomes a `ToolResult` the
agent can speak around, because §11.4 requires every failure path to end in an
answer, a human or a ticket — and an exception escaping the loop ends in none of
those. Domain errors keep their own message and code: "no approved
recommendation exists" and "this product needs a licence" are different
instructions to the agent, and flattening them to "unavailable" would turn a
safety signal into a shrug.

`grounded` is a field on every result. §1 N1 lets the agent state a fact only
from a result where it holds.

Measured warm p95 across the Tier-1 tools is 3–7 ms against the §6.3 budget of
150 ms. The first call of a worker's life measured ~300 ms — connection setup and
plan compilation, which is what §7.5's warm-up exists to absorb, not a property
of the queries.

### The knowledge layer is three tiers, and two of them are refusals

Tier 1 is SQL behind tools. Tier 2 is hybrid retrieval. Tier 3 is a curated
answer cache. What makes this work is less the retrieval than what is kept out of
it:

- **A price never comes from a document.** `search_knowledge` refuses the
  *question* and names `check_availability`. Nearest-neighbour search over prose
  will confidently return last month's price, and confidently is the problem.
- **A dose never comes from prose.** A retrieved chunk carrying a quantity is
  returned as context but flagged, and the agent is told to call
  `recommend_for_crop`. §23-3 and §16.2 make an unapproved dose unservable; a
  document is not an approval.
- **The chunker will not split a table or a dosage row.** A bisected dosage row
  returns "100 ml" with its per-acre basis and pre-harvest interval in the
  neighbouring chunk — every qualifier that made the number safe, gone. A table
  larger than the window is emitted whole and oversized instead.

Retrieval fuses **ranks, not scores** (RRF, k=60). A BM25 score and a cosine
distance are not on the same scale, and any normalisation between them is a guess
that shifts with the corpus.

Both the caller's language and English are searched on every query. This is not
belt-and-braces: it is the difference between working and not. BM25 cannot match
a Hindi question against an English document at all, and Hindi agronomy prose is
thin, so the dense half is what makes the corpus reachable for most callers.

`kb_chunks` is indexed under **both** the `simple` and `english` text-search
configurations, unioned, and queried the same way (migration 0003). Either alone
loses matches silently: `english` stems romanised Hindi apart — "khaad" and
"khad" — while `simple` gives up English morphology, so "services" stops matching
a document about a service. The original schema indexed with one and queried with
the other, which retrieved almost nothing and looked like an empty corpus.

BM25 **ORs its terms rather than ANDing them**. `websearch_to_tsquery` requires
every term, which is right for a search box and wrong for speech: a farmer asks a
whole sentence, and one word the corpus happens not to contain takes the query to
zero hits. Measured — "how to handle an interruption politely" matched nothing
while "interruption" alone matched, with the answer sitting in the index the
whole time. `ts_rank_cd` does the work AND was doing, ranking a chunk that
matches four terms above one that matches one, without discarding the second.

A chunk's **section heading is indexed with it**, weighted above the body
(migration 0004), and prepended to the text that gets embedded
(`Chunk.embedding_text`). A heading names the topic while the body underneath
demonstrates it, so a caller asking "how do I handle an interruption" is asking
in the heading's vocabulary and not the transcript's. Both halves of the hybrid
see the same text on purpose: fusing two ranked lists built from different
inputs makes the ranks incomparable, which is the one thing RRF assumes.

### Smart Turn takes a spectrogram, and the wrong input fails silently

The worst bug in this build, found only by downloading the weights.

Smart Turn v3's ONNX graph declares `input_features: [batch, 80, 800]` — an
80-bin log-mel spectrogram over an 8-second window. The adapter fed it raw
float32 samples. ONNX raised a shape error, the adapter's `except` caught it,
logged a warning and returned `None`, and the caller fell back to
VAD-plus-silence.

So the detector **never ran**, and every Marathi and non-Flux call behaved as
tier C while the admin panel reported tier B — which is exactly the failure
`smart_turn.py`'s own docstring warns about, written before the code that caused
it. Nothing in the logs looked like a bug: a warning that fires on every turn
reads as noise within a day.

`turn/features.py` now does Whisper's transform in numpy — 25 ms Hann window,
10 ms hop, 80 Slaney mel bins, log10 clamped 8 dB below the peak. Every constant
is from that pipeline and none is tunable: a filterbank that is nearly right
produces features that are plausible and wrong, which the model will happily
score. `test_the_real_model_accepts_the_features` runs the actual graph, which is
the only check that could have caught this.

Two smaller corrections came with it. The published filenames are
`smart-turn-v3.1-cpu.onnx` and `-gpu`, not a bare name — the earlier download
hint was wrong. And **v3.2 now exists**, which the 31 August verification did not
cover; the fetch script stays pinned to v3.1 and says why, because §19.5 makes a
model change something an eval decides rather than a version bump.

### G.711 is eight lines that are easy to get confidently wrong

The µ-law codec shipped at **-5 dB SNR** in its first draft — worse than the
signal — because it used the wrong segment table and skipped G.711's shift to 14
bits. It produced bytes of the right length that decoded to something
recognisably speech-shaped, so nothing about it looked broken.

The test asserts an SNR figure rather than comparing to a golden file, and that
is the whole reason it caught it: a golden file generated from the broken codec
would have blessed the breakage. It now measures 36.6 dB, flat across amplitude,
which is what G.711 does.

### The safety path never reaches the model, in either direction

§16.1 is the highest-severity route in the system, and two properties of it are
structural rather than procedural.

**Detection does not depend on a model call.** §16.1 requires a keyword list
*plus* LLM classification, in that order. The gateway is the component most
likely to be timing out at the moment a distressed caller is on the line, and a
safety path that needs it fails exactly when it is needed. The classifier can
*add* an emergency the list missed; it can never remove one, because the model
is the part that can be wrong, slow, or absent.

**The script is a fixed string reached by a cache key.** §16.1 forbids the agent
suggesting an antidote or "drink milk" — several folk remedies are actively
harmful for organophosphates — so there is no code path by which a model
composes a sentence on this branch. The forbidden-phrase assertions in the
golden conversations are checkable only because of that.

The keyword lexicon is split into **exposure** (something entered a person) and
**symptom** (a person is in distress), and a bare product mention is neither.
"दवा" appears in nearly every advisory call in the district; matching it would
route the day's dosage questions to the emergency line, and an emergency path
that fires on everything is one that gets switched off.

One shape in it is worth copying elsewhere: spill detection is a *pattern*
(`<body part> पर <spill verb>`), not a phrase list. The first version enumerated
"शरीर पर गिर" and "बदन पर गिर" and missed "हाथ पर गिर गई" — and a farmer says
whichever body part it landed on. Enumerating body parts is a losing game where
losing means someone with pesticide on their skin is read a dosage table.

### The validator can check grounding without checking truth

§16.3 puts a validator between the model and the TTS. The rule that carries the
most weight is the grounding one, and it is worth being precise about its scope:
it cannot verify that a sentence is *true*, but it can verify that a number the
agent is about to speak appeared in a tool result **this turn**. An invented
price has no matching tool result however fluently it is phrased.

Numbers are compared after folding Devanagari digits and thousands separators,
because the model writes `1,350` and `१३५०` where the tool returned
`Decimal('1350.00')`. A validator that rejected those would be rejecting correct
answers constantly, and the response to a validator that cries wolf is to switch
it off.

It is the *last* line, not the only one — the tools refuse to answer without
data and the prompt says not to guess. A validator doing all the work would mean
the layers above it were not.

### Escalation reasons are chosen for the person reading them

§12.2's triggers overlap: an intent goes unresolved twice *because* the audio is
bad, so both "repeated misunderstanding" and "low recognition confidence" fire on
the same call. Which one is reported is not a tie-break, it is what the centre
manager sees on the transfer — and the two lead to different actions. Repeated
misunderstanding says the farmer is being unclear, so re-ask. Low confidence says
the line is bad, so call back.

The confidence trigger therefore wins, and the repetition trigger is held back
for one turn while every confidence reading so far is below the floor. Without
that, the earlier threshold always won and the manager was always told the wrong
thing.

### Devanagari breaks the regex word boundary, asymmetrically

`\b` is defined against `\w`, and `\w` excludes combining marks. So `बोरी\b`
never matches -- the string ends on a matra -- while `ग्राम\b` does, because it
ends on a consonant. Half a keyword list works and the other half silently never
fires. This has now caused three separate bugs in this codebase: the danda inside
the letter block, the dose-unit list, and the Hindi price guard, which passed
every English price question to retrieval and let the Hindi ones through.

`text/script.py` owns this. `whole_word()` wraps a pattern in lookarounds over
the corrected character class. Any pattern that must match a whole Hindi word
goes through it.

---

## 4. What exists now (Phases 1-8)

| Area | State |
|---|---|
| Schema | 38 tables, partitioning, RLS, indexes incl. HNSW and trigram |
| Migrations | Alembic; initial migration builds extensions, tables, triggers, partitions, role, grants and policies |
| Seeds | Deterministic: 15 districts, 10 centres, 60 products, 12 crops, 40 **draft** recommendations, 200 farmers |
| Security | Argon2id, mandatory TOTP MFA, 15-min JWT, refresh rotation with family reuse detection, RBAC, Redis rate limits, hash-chained audit |
| Voice worker | Exotel protocol, call session, durable records, drain-not-kill shutdown |
| Simulator | WAV replay, DTMF, line drop, timing report |
| Local stack | All thirteen §20 services in compose: Postgres, Redis, MinIO, LiteLLM, API, worker, voice worker, admin, simulator, Prometheus, Grafana |
| Speech (Phase 2) | Deepgram Flux and Sarvam STT/TTS adapters, declarative language routing, Smart Turn v3.1, barge-in with context truncation, Hindi text normalisation |
| Tools (Phase 3) | All thirteen of §6.3, behind one validated contract with a 150 ms p95 budget and a 400 ms timeout |
| Knowledge (Phase 3) | Semantic chunker, `multilingual-e5-base` via ONNX, hybrid BM25+dense with RRF(k=60), Tier-3 answer cache, `uaagro-kb` ingest CLI |
| Inbound flow (Phase 4) | §11.1 state machine, the fifteen §11.2 intents, §16.1 safety path, §16.3 output validator, §12 escalation engine, §6.2 context builder, LiteLLM gateway with §6.1's fallback ladder |
| Evals (§19) | The sixty §19.3 golden conversations; WER scoring with Devanagari normalisation and per-condition reporting; turn-detection false-cut/dead-air metrics with the asymmetric regression gate; the versioned LLM-as-judge |
| Telephony (Phase 5) | Exotel plus Plivo and Twilio serializers, a hand-written G.711 codec, call control with a toll-fraud allowlist, webhook signature verification, two-channel recording with SSE-KMS |
| Outbound (Phase 6) | §13.1's eight-check compliance gate, the §13.2 conversation, §13.3's retry table, WhatsApp with §14's seven templates and its cost model |
| Admin (Phase 7) | Next.js 15 App Router, TS strict, English UI with content rendered in its own language, RBAC, sign-in with mandatory TOTP, fifteen of §15.1's sixteen screens, a source-level server-only boundary test |
| Hardening (Phase 8) | OTel/Prometheus instruments with cardinality guards, 12 alert rules, 2 Grafana dashboards provisioned as code, the load harness, the runbook, Terraform, an automated restore rehearsal |
| Assembly | `runtime/assembly.py` builds one call: published config, caller identity, §5.1 speech stack with §5.5 keyterms, tools, flow agent, turn loop |
| Voice path | Clause- and sentence-level streaming from model to synthesiser to line, a local voice gate for barge-in, resume-after-nod, catalogue vocabulary boosting, a shared pre-warmed audio cache |
| Background worker | Post-call pipeline, campaign dialer, partition roll, retention, audit-chain verify, embedding backfill, all under the `system` RLS role |
| Migrations added | 0002 orders, 0003 dual text-search config, 0004 section headings in the index, 0005 campaign compliance fields, 0006 system-role policies |
| Tests | 867 Python collected (2 load tests deselected) plus 16 in the admin panel; 7 xfail/xpass are the §21 retrieval-gate misses and two boundary-unstable queries, each recorded with its reason; ruff, ruff-format, `mypy --strict` and `tsc --strict` clean |

**All eight phases have code.** What remains open is the set of gates that need
a human, a vendor account or a live environment — listed under *Known gaps*
below rather than left to be discovered.

### The assembly, and how it was missing

Phases 2, 3 and 4 each built their layer and tested it against its own doubles.
Every one of those suites passed. Nothing imported them: the worker's media path
still played the Phase 1 placeholder tone, `pipelines/conversation.py` and the
whole of `flow/` were reachable only from tests, and no test could tell —
because each layer was verified against fakes rather than against the layer
above it.

`runtime/assembly.py` is the answer to "who constructs all of that, and in what
order", and `tests/test_end_to_end.py` is what makes it stay true: it assembles
the real session, serializer, pipeline, agent and tool registry against a real
database, doubling only the three paid edges (recogniser, synthesiser, model).
Unhooking `CallSession.on_audio` was tried deliberately; the test fails.

The equivalent gap on Flow B was that nothing ran a campaign. `worker/campaign.py`
now joins the compliance gate to the dialer to the telephony adapter, and
re-evaluates the gate per contact at dial time as §13.1 requires.

### The model was running unbounded

`config/defaults.yaml` set `max_output_tokens: 220` and `temperature: 0.3`,
`LlmSettings` held both, and the gateway's request payload was
`{"model", "messages", "stream"}` -- neither value was ever sent. The model ran
with no output cap at the provider's default temperature.

The cost is not tidiness. An uncapped model at temperature ~1.0 drifts past
§11.3's 35-word limit; §16.3's validator rejects the answer; the retry is a
second full generation inside the caller's turn. A missing request parameter
was showing up as a farmer waiting twice as long for a reply twice as long as
it should be -- and being billed for both (§8).

The cap is now per turn rather than per process, because §11.3 allows a dosage
answer sixty words against an ordinary answer's thirty-five: 122 tokens for an
ordinary turn, 210 for a dosage one. The multiplier is deliberately pessimistic
about how Devanagari tokenises, because the failure mode of a cap set too low
is a dosage answer truncated mid-sentence, losing the pre-harvest interval and
the precaution §16.2 requires be spoken.

The two §6.1 timeouts had the same shape of bug without the consequence: they
were module constants that happened to equal the configured values, so editing
`defaults.yaml` changed nothing. They are read from config now.

`tests/test_llm_generation_limits.py` asserts against the JSON actually sent
over HTTP. Every test that checked a function argument instead would have
passed throughout.

### There is now a path to the model that is not through LiteLLM

§6.1 puts LiteLLM in front of the model and the reasons hold — one place for
keys, one for spend, one for §6.4's model swap. It stays the default in staging
and production.

`LLM_GATEWAY=anthropic` selects a second `LlmTransport` that talks to the
Messages API directly. This is not a preference about vendors: LiteLLM is a
second process on the request path, §7 gives time-to-first-token 550 ms out of a
1,200 ms turn, and on a single-host deployment it is one more thing that has to
be up before the helpline can answer a call. §6.1's failure ladder, both
timeouts and the per-turn token budget are untouched, because they live in the
gateway above the transport.

One shape difference costs real money if it is got wrong. Anthropic takes
`system` as an array of blocks, and that array is where `cache_control` goes —
not on a message. The persona is roughly 900 of the ~2,000 input tokens on every
turn of every call, so a cache marker on the wrong object is a silent
several-fold increase in the input bill with byte-identical replies. Nothing
fails; §8's per-call cost simply rises. `message_start` carries
`cache_read_input_tokens`, which is the only signal that says the cache is
working at all, so it is logged.

An unrecognised `LLM_GATEWAY` raises rather than falling back to LiteLLM. A
silent default there is a deployment quietly not using the gateway it is being
billed and audited through.

### Four things the voice path was leaving on the table

**The pipeline waited for the whole answer before it spoke.** `_collect_response`
was a list comprehension over the model's stream, so nothing was synthesised
until the last token arrived, and `_synthesise` then collected every audio chunk
before playing the first. Two "wait for everything" stalls in series. It now
releases each sentence the moment it is complete and streams that sentence's
audio as it arrives -- measured on identical doubles at **398 ms → 209 ms** to
first audio. `first_token_at` was also being marked after the *last* token, so
`llm_ttft` reported the whole generation and blamed the model for time the
pipeline spent waiting.

Two caveats worth keeping straight. The gain scales with answer length, not
with any fixed number. And on the live path `Agent.respond` deliberately yields
the whole answer as one piece, because §16.3's grounding check cannot run on
half a sentence and a spoken sentence cannot be retracted -- so the streaming
machinery pays off for cached and speculative answers today, and the safety
constraint on the generated ones is correct rather than an oversight.

**Nothing gave the recogniser the catalogue.** §5.5 asks for the ~500-term
lexicon injected as keyterms. `Lexicon.keyterms()` produced them,
`build_speech_stack` accepted them, and the assembly passed none -- while the
Deepgram adapter sent no keyterm parameter at all. A mis-heard product name is
not a slightly wrong answer, it is a lookup miss, and the agent then tells a
farmer it cannot find the product they are holding. Now loaded once per worker
from `products`, brands and active ingredients included, longest terms first.

**The audio cache was per call.** Constructed inside `build_call_pipeline`, so
its in-process layer died with the call: the greeting was re-synthesised for
every caller, forever. §16.1 goes further and requires the poisoning script to
be spoken "from a cached recording" -- it was being synthesised on demand, with
a vendor round trip between a farmer with pesticide in their eyes and the words
telling them to get to a doctor. One cache per process now, pre-rendered at
startup.

That pre-warm runs **in the background**, which the first version did not. Every
other startup step is local and fast; this one calls a vendor, and a synthesiser
that is down answers each request with a timeout. Awaiting it turned "the voice
API is having a bad minute" into "the worker will not come up during a deploy".
The test that found it is `test_worker_startup.py`, which also closed a gap of
its own: the `worker_url` fixture that boots the app with its real lifespan
existed and no test had ever used it.

**The hold phrase is gone.** A version of this document argued for it: a slow
turn is silence, silence on a GSM line reads as a dropped call, so say "one
moment" after 750 ms. Through the configured model endpoint every turn is
slower than 750 ms, so the agent said "एक क्षण रुकिए, मैं देख रहा हूँ" on every
turn of the first live test, and the customer heard a tic, not reassurance.
The wait is now shortened rather than announced -- see *Where the latency
went, measured 3 September* -- and a farmer's "हैलो?" into the silence is
handled by the same rule as any other nod: it does not cancel an answer that
is about to arrive (`pipeline.nod_after_cut`).

### The panel opens in English; the data is never translated

§15 asks for a Hindi and English panel. Both are built and complete
(`messages/en.json`, `messages/hi.json`, the Hindi written for centre managers
in the districts), and the panel opens in English by the customer's decision.

**The language is a per-browser setting, not the address.** The switch in the
sidebar posts to `/api/locale`, which records the choice in a cookie and then
redirects; `lib/locale-choice.ts` decides every request from that cookie, and
the locale prefix follows it. A `/hi/...` link opened by a browser that has not
chosen Hindi lands on the English page, and an `/en/...` link opened by one
that has chosen Hindi lands on the Hindi page. That is the opposite of what
this section said when the panel shipped, and the reason is a real failure: the
default was already English and the panel still opened in Hindi, because the
address that had been given out and autocompleted ever since was `/hi/login`.
A default that any stale bookmark silently overrides is not a default. The
cookie is also the only signal -- automatic detection is off, since a browser
whose language list happens to start with Hindi is not a choice anyone made.

Everything the panel *displays* stays in the language it was
written or spoken in -- farmer names and villages in Devanagari, product names
as the catalogue holds them, prompts and greetings exactly as published, call
transcripts in the language of the call. Translating those in the view would
misrepresent both what is in the database and what the farmer actually heard.

Two details that make it more than a string swap:

- **Dates and numbers are formatted `en-IN`, not `en`.** US English renders
  1 September as "9/1/2026", which an operator in Lucknow reads as 9 January.
- **Devanagari cells carry `lang="hi"`.** Inside a page whose root is
  `lang="en"`, unmarked Hindi is read by a screen reader with English phonemes,
  and the browser may pick a font with no Devanagari coverage.

The locale prefixes also uncovered a **pre-existing middleware bug**: the
matcher `"/((?!api|_next|_vercel|.*\..*).*)"` does not mean what it reads as --
in a TypeScript string `"\."` collapses to `"."`, so the intended "a path
containing a dot" became "a path of at least one character", and the negative
lookahead excluded almost every route. The middleware had been running for `/`
alone, which is why locale negotiation appeared to work. It is now two literal
patterns that cannot fail the same way.

### The panel and the API were built to different auth contracts

Running the stack locally for the first time surfaced this: the panel sent
`x-user-id` / `x-user-role` / `x-centre-ids` headers and a `uaagro_session`
cookie, and the API authenticated from `Authorization: Bearer` and read neither.
Every admin call would have returned 401, and there was no sign-in screen at
all -- the cookie the panel looked for had nothing that could set it.

The admin API tests could not catch it, for the same reason the media-path gap
survived: they override `current_principal` with a fake, so the real header
contract was never exercised. The end-to-end check that found it was starting
the thing and trying to log in.

Two things were wrong beyond the missing screen. The headers were a *false*
affordance -- a comment claimed "the API binds RLS GUCs from these", which it
never did; had the API honoured them, a panel bug could have widened its own
scope. And the role now travels inside the signed token, where the control
plane reads it from the claims, so the panel has no way to assert one.

### Three things measurement changed

**The statement cache is per connection, and that is a latency budget.** The
first `lookup_farmer` in a fresh process takes 671 ms against 13 ms warm — the
connection handshake is only 47 ms of it; the rest is SQLAlchemy compiling the
ORM statement and Postgres planning it. The tool hard-times-out at 400 ms, so
the first farmer to call after every deploy was told the lookup was taking too
long. Worse, the driver prepares statements **per connection**: warming one
leaves the rest of the pool cold, measured at 268 ms p50 versus 21 ms once the
whole pool is warm — roughly the first `pool_size` callers, not just the first.
`ToolRegistry.warm()` therefore fans each statement across the pool, the worker
runs it at startup, and readiness reports 503 until it finishes so a load
balancer cannot route a call into a cold process.

**Background jobs saw an empty database.** RLS policies read
`current_setting('app.role', true)`; with nothing bound it is NULL, the
predicate is NULL, and the session sees no rows. The API binds the staff user's
role and the media path binds `voice_agent`, but the background worker acts for
no user and bound nothing — so the post-call pipeline would have reported "call
not found" for every call it was handed, and the dialer would have loaded a
contact list of zero and reported a clean run. Both fail silently *and
successfully*, which is the worst shape a security default can take. Migration
0006 adds a `system` role to the policies and `system_session()` binds it.

**Money was being summed in float.** `cost_total_inr` was the one `Numeric`
column in the schema annotated `float`, and the post-call pipeline added the
per-call components with it: ₹1.2 + ₹2.1 + ₹1.9 + ₹0.9 came to 6.100000000000001.
That figure is checked against `max_cost_per_call_inr` and accumulated into the
daily spend cap, so it is not cosmetic. Both are `Decimal` now.

### Two tables that are not in §10

`orders` and `order_items` were added in migration 0002. §6.3 requires
`get_order_status(farmer_id, order_ref?)` and §6.2 puts the farmer's last three
orders in the context block of every turn, but §10 defines nowhere to read either
from. §1 N1 forbids answering "मेरा ऑर्डर कहाँ है?" from anything but stored data, so
the choice was between adding the tables and shipping a tool that always says it
does not know.

They are deliberately thin — what a farmer asks about on the phone, and nothing
about billing. `orders` carries RLS on the same terms as `calls` and `farmers`,
because an order row carries a name, a centre and a purchase history.

### The reranker is specified, measured, and not enabled

§9 puts a cross-encoder between fusion and the final four. `Reranker` exists as
an interface and `HybridRetriever` calls it when one is supplied; none is. That
is a measurement, not a deferral.

`jinaai/jina-reranker-v2-base-multilingual` (int8 ONNX) was run over the seed
corpus against the twenty §21 gate queries plus six deliberately irrelevant ones:

- **396 ms per pair** on CPU, so §9's top-20 rerank is about **eight seconds** —
  five times the budget for an entire tool-calling turn.
- **No separation.** Worst real query scored −2.105; best nonsense scored −2.101.

It costs five turn budgets and buys nothing measurable. That is a statement about
eighteen chunks of a seed document, not about reranking, so the seam stays and
this should be re-measured against UA Agro's real agronomy corpus.

### Nothing can tell a relevant chunk from an irrelevant one — yet

The more uncomfortable half of the same measurement. A vector index always
returns its nearest neighbours however far away they are, so "nothing relevant
exists" and "here are four passages" are indistinguishable from the outside
unless something scores relevance. Over the same query set:

| Signal | real (min) | junk (max) | separation |
|---|---:|---:|---:|
| e5 cosine, absolute | 0.7861 | 0.7959 | **−0.0098** |
| e5 cosine − corpus median | 0.0079 | 0.0188 | **−0.0108** |
| jina-reranker-v2 logit | −2.105 | −2.101 | **−0.004** |

Every separation is negative: the worst real question scores below the best
nonsense one. Any threshold either rejects real questions or admits nonsense,
so picking one would encode a confidence the system does not have.

Part of this is the corpus — eighteen chunks of a document *describing what
content should exist* rather than being it, which §9 itself calls "a seed and a
schema, not a finished corpus". Every chunk is about how to answer, so nothing
is strongly about anything.

So `search_knowledge` uses **corroboration instead of confidence**. A chunk both
retrievers found is a match two independent methods agree on. A chunk only the
vector index found is returned with `weak_match: true` and an instruction to say
"I don't know" and offer a person if the passage does not actually answer the
question. That is honest: it never withholds a real answer, and it never presents
nearest-neighbour noise as established fact. The threshold work belongs with the
Phase 4 eval harness and a real corpus.

---

### Where the latency went, measured 2 September 2026

The LLM runs through a third-party Anthropic-compatible proxy that stays. Its
first token lands at ~1.7 s and nothing in this repository can change that, so
everything below is about the milliseconds *around* it — of which there turned
out to be a great many.

| | before | after | how |
|---|---|---|---|
| TTS first byte | 741 ms | **332 ms** | pooled connection |
| LLM first token | 1519 ms | **1251 ms** | pooled connection |
| STT handshake | 1149 ms on the critical path | **0** | overlapped with the greeting |
| First audio of an answer | 7121 ms | **1818 ms** | sentence streaming |

**`httpx` expires an idle connection after five seconds.** The gap between one
turn's request and the next is however long a farmer takes to hear an answer
and reply, which is reliably longer. So the default was not "reuse the
connection", it was "reuse it only within a burst", and the helpline paid a
fresh DNS lookup, TCP handshake and TLS negotiation on nearly every turn — about
700 ms per turn across two vendors, against a §7 budget of 1,200 ms for the
whole turn.

Worse, pooling was per-*instance* and the adapters are built per *call*:
`build_speech_stack` and `build_gateway` both run inside `build_call_pipeline`.
Every call had its own empty pool, so the keepalive settings could never have
helped. `adapters/http.py` now holds one client per endpoint for the life of the
process, keyed on the credential so two organisations cannot share a connection
carrying the other's key. Adapters no longer close what they do not own — the
audio pre-warm used to close the very connection it had just warmed.

**The STT socket was opened before the greeting.** 1,149 ms of silence, then
"नमस्ते". Nothing needs the recogniser until the caller speaks, and the caller
does not speak until the greeting has played, so the handshake now runs
concurrently and is awaited just before the greeting is handed back — still
raising at construction if it failed, rather than surfacing as a mute call.

### Where the latency went, measured 3 September 2026

The first live conversation through the browser page (thirteen turns, three
minutes) put numbers on what the customer had described. Speech-end to first
audio was **3.3-3.8 s** on every turn; the answers ran **6-11 s** each; the
validator's 35-word cap rejected two of them, and each rejection was a second
generation inside the caller's turn (one turn took 7.8 s); the agent greeted
the caller by name on every reply and said "एक क्षण रुकिए, मैं देख रहा हूँ"
before all of them; and `bargein.cut` never appeared in the log at all.

That last one was structural. `_commit` awaited playback inside the
recogniser's event loop, so while the agent was speaking nothing was reading
the recogniser -- a speech-start sat in the queue until the answer finished.
The answer is spoken in its own task now.

| | 2 Sep (live) | 3 Sep (socket, synthesised speech) | how |
|---|---|---|---|
| Speech-end to first audio | 3.3-3.8 s | **2.15-2.3 s** in the pipeline, 2.4-2.7 s including Soniox's endpoint | first clause released to the synthesiser; no regenerations for length; the catalogue tool finds a product *inside* a question |
| Answer length | 6-11 s | ~5 s | 26-word cap, prompt asks for 15-20; the streaming path stops at the cap instead of regenerating |
| Barge-in | never fired | cut **0.2 ms** after the gate's onset, 200-330 ms after the voice starts | local voice gate on the audio, answer spoken in its own task |
| A fan over the answer | -- | ignored | Silero VAD when its weights are present, else periodicity; a stationarity test on top of either |
| "हाँ जी" over the answer | a new reply | silence, or the rest of the interrupted answer | `pipeline.nod_after_cut`, resume from the unheard sentence |
| "एक क्षण रुकिए" | every turn | never | removed |
| Name in replies | every reply | greeting only | `flow/address.py`, a rule rather than a request |

**Where the remaining time is.** About 40 ms before the model is asked
(safety and intent rules 0.1 ms, the catalogue lookup 36 ms), **1.2-1.4 s to
the model's first token**, ~0.3 s for the first clause's tokens (three words
and a comma; Devanagari costs ~3.5 tokens a word), 0.3-0.5 s to the
synthesiser's first byte -- and, measured on the same afternoon, 1.5 s on
one call in four, which is the vendor's variance and not ours. That variance
no longer lands *between* sentences: the model is read by its own task and
every sentence after the first is rendered the moment it exists, while the
one before it is still playing (`_produce`, `_render_ahead`). Reading the
model from the speaking loop had throttled it to playback -- sentence two
was not even requested until sentence one had finished -- so every
sentence boundary paid the synthesiser's first byte in silence. The first-token figure is the endpoint's floor: a
ten-token prompt against `api.mwapi.dev` takes 1.1-1.5 s to answer, prompt
caching is not honoured there (`cache_read` is never reported), and the proxy
lists only Claude models, so there is no faster one to choose on it. A direct
Anthropic endpoint typically answers Haiku's first token in 0.3-0.5 s, which
is the difference between this and the brief's one second. Nothing in this
repository can close that gap; the switch is `LLM_BASE_URL` and a key.

**The voice gate runs Silero when it can.** `models/silero_vad.onnx` (v5,
MIT, 2.3 MB, from `github.com/snakers4/silero-vad`, `src/silero_vad/data/`)
is gitignored like the other model files, loaded once at worker start --
loading it inside the first call cost that call's greeting 1.7 s -- and the
startup log says which judge is running (`vad.judge`); the spectral rule
serves without the file. On a synthesised Hindi question Silero scores 0.78
on average and 0.84 over a fan; the fan alone scores 0.06. The harmonic tone
the unit tests use scores under 0.4 with it -- it was trained on speech --
so the Silero test uses a real clip, `fixtures/audio/hindi_question_8k.wav`.

Its one observed weakness came out of the socket test: after a sentence and
a stretch of digital silence the model reports **1.0 on pure zeros** and
stays above 0.5 for the first 200 ms of a fan switched on abruptly, and the
agent stopped for it. A fan has no pitch, so every frame Silero calls speech
must also pass the periodicity test (at a lower bar than the spectral rule
uses alone), the noise floor can no longer fall through the ground on
digital silence, and an onset's own voiced frames must swing at least 6 dB
-- a syllable does, a fan's do not.

**The Hindi is the farmers' Hindi.** The persona now says so in terms: the
English words a farmer uses every day -- रेट, स्टॉक, स्प्रे, पंप, सीड, बैग,
सेंटर, मैनेजर, ऑफर, डिलीवरी, टाइम, प्रॉब्लम, डोज़ -- written in Devanagari so
the synthesiser says them right, and the textbook words (उर्वरक, मूल्य,
उपलब्ध, मात्रा, प्रतीक्षा अवधि) banned. The greeting says सेंटर; the transfer
line says सेंटर मैनेजर.

**The call ends.** It did not, before 3 September: the inbound agent had no
notion of being finished, so a farmer's "बस, धन्यवाद" was answered with
another question and the line stayed open until they hung up. Three ways it
ends now, all landing on the same `on_call_over` the outbound script already
used. A goodbye -- `flow/closing.py` knows the ways farmers say it, and
knows that "ठीक है" alone or "नमस्ते" with a question after it is not one
-- is answered with the published `closing_template` straight from the
audio cache, without the model, because a second of silence before
"नमस्ते" is the one place a farmer would already have put the phone down.
A plain "नहीं" is a goodbye only when the agent had just asked whether
there was anything more. And §11.4's silence ladder now exists in the
pipeline: a prompt at 6 s of silence, another at 15 s, the goodbye and a
hang-up at 25 s, recorded as `abandoned_silence`; anything the caller does
-- a voice at the gate, a word from the recogniser -- starts it over. In
every case the line drops only after the goodbye has been paced out to its
last frame, plus the provider's jitter.

**What the browser page proved and what it cannot.** The hiss under every
word was the page, not the voice: an 8 kHz buffer handed to a 48 kHz audio
graph, which Chrome upsamples by linear interpolation. The context runs at
8 kHz now and the browser does both conversions with real filters. The page
still says nothing about a carrier leg, and `docs/VERIFICATION.md` still lists
the real call.

### The timeout that turned "slow" into "broken"

`llm.first_token_timeout_ms` was doing two incompatible jobs: §7's SLO ("are we
fast enough?") and §6.1's operational timeout ("is this attempt broken?"). They
were the same number by coincidence, and the coincidence was fatal.

Against a measured 1,251 ms first token, an 800 ms timeout meant **every turn**
abandoned a generation that was going to succeed, retried on the fallback model
— slower still, and timing out on two of three attempts — and then escalated.
A helpline where every call reaches a person is not degraded, it is broken, and
nothing about it looks like a timeout setting.

They are separated now. The operational timeout is 3,500 ms, sized against the
worst observed turn rather than the median because crossing it does not degrade
gracefully. §7's `llm_ttft` budget stays at 260/550, stays breached, and stays
failing CI — moving a target to meet a measurement is how a latency budget
becomes decoration.

### Sentence streaming, and why it is safe now

`Agent.respond` used to yield the whole answer. The note explaining why said
§16.3's grounding check could not run on half a sentence. That was the wrong
reading of its own validator: the check is local, stateless, and its rules are
**monotonic** — register, guarantees, filler and ungrounded numbers each condemn
the sentence they appear in and cannot be redeemed by a later one. And the tool
results it grounds against are complete *before* generation starts, so a
sentence can be grounded the moment it is complete. Only §11.3's word cap is
cumulative, and it is checked cumulatively.

What is genuinely not decomposable is handled by two rules:

**A dosage answer never streams.** §16.2 wants the dose, the pre-harvest
interval and the precaution spoken together; a truncated one is worse than a
slow one.

**The first sentence is spoken only after it validates**, which keeps §16.3's
retry intact for the common failure — nothing has been said, so the answer is
regenerated with the violation as feedback exactly as before. Once speech has
started it cannot be recalled, so a later failure stops there and hands over: a
partial true answer plus a handover, never a retraction.

That last path fired in the live measurement above. The model's third sentence
pushed the answer past 35 words, two valid sentences had already been spoken,
and the turn stopped and escalated rather than continuing.

The first implementation handled the streamed sentences and the buffer's final
flush in two separate branches, and the copies disagreed: a violation in the
*closing* sentence — exactly where a model tends to append an invented price —
was detected and then silently dropped. It is one loop over one source of
sentences now, which is why `tests/test_agent_streaming.py` exercises the
closing sentence specifically.

---

### The stack, and what each choice is worth

Every runtime dependency was reviewed against what it would be replaced with.
The conclusions are recorded with their measurements, because "we evaluated X"
is worth as much as "we adopted Y" to whoever suggests it next.

**Kept, and they are the mainstream choice in their domain.** FastAPI,
SQLAlchemy 2.0 async with asyncpg, Pydantic v2 (Rust core), httpx, websockets,
structlog, ARQ, pytest, ruff, `mypy --strict`, Next.js 15 App Router. None
appeared as a bottleneck in any measurement taken here.

**uvicorn over granian.** Granian's Rust HTTP core benchmarks faster. This
service's hot path is a long-lived WebSocket carrying 20 ms audio frames, not
HTTP request throughput, and uvicorn's WebSocket handling is the more heavily
exercised of the two on exactly that shape of traffic. Kept on the strength of
operational maturity rather than a benchmark that does not measure this
workload.

**orjson, adopted — with the honest number.** The media path decodes a frame
every 20 ms per call. orjson is 3.6x faster to decode and 18.8x faster to encode
on a real 578-byte frame; the absolute saving at 50 concurrent calls is
**17.7 ms of CPU per second, under 2% of one core**. It is not why calls are
fast. It is in because it is strictly better at no risk, and
`packages/domain/src/uaagro_domain/fastjson.py` says so in its own docstring
rather than implying a rescue.

The larger effect was incidental: orjson emits raw UTF-8 where the standard
library escapes non-ASCII to `\uXXXX`, three bytes per character instead of
six. §5.5 sends up to 6,000 characters of Devanagari keyterms at the start of
*every* call, and that message is now about half the size.

It is a shim and not an import alias because two API differences would
otherwise be silent protocol bugs. `orjson.dumps` returns **bytes**, and a
WebSocket `send()` treats bytes and str as *different frame types* — a control
message to Exotel would have become a binary frame, which some servers reject
and others drop. And its `JSONDecodeError` is a different class; it does
subclass the standard library's, which was verified rather than assumed,
because if it did not then one malformed vendor frame would stop being skipped
and start ending calls. Both are covered in `tests/test_fastjson.py`.

**HTTP/2 to the vendors, rejected.** Multiplexing helps when many requests
share a connection. This system makes one streaming request at a time per call,
so there is nothing to multiplex; the win that was available — not
re-handshaking — was already taken by connection reuse.

**The proxy stays**, by instruction. It sets a first-token floor of ~1.25 s
that nothing on this side of the socket can move, and every optimisation above
is about the milliseconds around it.

### §19 was never switched on

`telemetry.py` has carried a complete OpenTelemetry façade since Phase 1 — the
§19 metric names, §7's segments, OTLP exporters for both traces and metrics,
label-cardinality checks. `Instruments.setup()` was **called from nowhere**, and
the SDK was not installed. Every metric in this system went to an in-process
list that nothing read, and no trace was ever exported. The same
built-but-never-wired pattern as the media path, Flow B's campaign runner and
the LLM's generation limits.

Now installed and called from all three services, with two corrections:

**Metrics no longer require a tracing collector.** `setup()` returned early
whenever `OTEL_EXPORTER_OTLP_ENDPOINT` was unset — so a deployment that scraped
Prometheus and ran no collector, which is exactly what §20's compose ships, got
nothing at all. Prometheus and OTLP are independent sinks now, and `/metrics`
serves the §19 names with histogram buckets.

**`setup()` is idempotent.** Lifespans re-enter — uvicorn's reloader, a test
booting the app twice. OpenTelemetry refuses to replace an installed
MeterProvider and merely logs, while each `PrometheusMetricReader()` has
already registered a collector on prometheus_client's *global* registry: a
second call leaked a collector attached to a provider nothing would ever read.

The module moved from `voice_worker.runtime` to `uaagro_domain` in the process.
Wiring the API to it revealed the coupling — `apps/api` does not depend on
`apps/voice-worker`, so an API container built alone would have failed at
import. Telemetry is cross-cutting and now lives where all three services can
reach it.

### The event loop is asked for by name

`uvloop` is a libuv-backed drop-in for asyncio's selector loop and is faster at
what this system does: many small socket operations on a 20 ms cadence across
concurrent calls. Loop scheduling time is audio jitter.

It was already in production — as a transitive extra of `uvicorn[standard]`,
which auto-detects it. A good outcome reached by accident: nothing asked for it,
nothing tested for it, and uvicorn reorganising its extras would have removed it
with no failure anywhere, leaving only a system that got slower under load.

It is now an explicit dependency of all three services and installed by one
named function. The background worker gained it outright — ARQ has no
auto-detection, so `arq worker.tasks.WorkerSettings`, the dialer included, had
been running on the selector loop the whole time.

### The circuit breaker, and what it is worth

`adapters/resilience.py`. The retry ladders in this codebase are built for a
vendor having a bad moment — try, wait, try the other model, escalate. For a
vendor that is **down** they are exactly wrong, because every call pays the
whole wait again.

Measured against the configured timeouts, with the model unreachable:

| | per turn |
|---|---|
| ladder alone | 9,317 ms |
| after the breaker learns | **0.7 ms** |

Over ten turns of one call during an outage that is 79 seconds of dead air
removed, and seven requests the failing vendor did not have to absorb — which
matters as much as the latency, because a service shedding load cannot recover
while every worker keeps offering it the same load.

Three design points carry the weight:

**Only the vendor's faults count.** Timeouts, connection errors and 5xx open the
circuit. A 4xx does not. An unset `BAKBAK_VOICE_HI` returns 422 on every request
— exactly the failure this repository hit an hour before the breaker was written
— and counting those would take a healthy synthesiser out of service over one
unfilled line of config, then report it as a vendor outage and send whoever is
on call to the wrong dashboard.

**A success clears the count.** Without it the counter only rises, and three
unrelated blips hours apart open the circuit on a vendor that answered perfectly
in between.

**A failed probe restarts the cooldown.** The first implementation did not, and
the breaker stayed permanently half-open once a probe failed — admitting a fresh
probe on every subsequent call, which is the retry storm it exists to prevent,
arriving one call at a time. `tests/test_resilience.py` covers it.

An open breaker is deliberately **not** un-readiness. The worker still answers,
still speaks §16.1's cached phrases and still escalates to a person; draining it
would turn one vendor's outage into no helpline at all. The state is reported on
`/health/ready` for an operator to see, not acted on.

---

## 5. Testing without Docker

The tests that prove §10 and §17 -- RLS isolation, the audit chain, the advisory
safety control, seed idempotency -- are the ones most worth running, and making
them contingent on Docker would mean they got skipped on most machines.

`pgserver` ships **PostgreSQL 16.2 with pgvector** as a pip wheel: no root, no
daemon, same major version as production. `make test` starts one, runs the real
Alembic migration and the real seed loader against it, and asserts behaviour
through the `uaagro_app` role so the RLS policies genuinely apply.

One fidelity gap, made explicit rather than papered over: that build ships no
contrib, so **`pg_trgm` is absent** and the migration skips the two trigram
indexes with a warning. `test_trigram_index_state_is_explicit` asserts the
index count matches the extension's availability, so an index existing without
its extension would still fail. On the Docker/RDS stack both are present.

This is test tooling. Production is Postgres 16 on RDS; the compose stack is
unchanged.

---

### What the eval harness can and cannot measure

§19's five pieces are built except the corpus. The scoring is exercised by
`tests/test_evals.py`; what is missing is 120 recorded utterances, which needs
speakers rather than code.

Two decisions inside the harness are worth stating, because both are places
where the obvious implementation produces a number that looks fine and means
nothing.

**WER is measured after Devanagari normalisation, and that is most of the
work.** Nukta composition, zero-width joiners, danda-versus-full-stop and
Devanagari-versus-ASCII digits all make two transcripts of the same sound
compare unequal. Counting those as errors inflates Hindi WER unevenly *across
vendors*, which turns a bake-off into a measurement of transcription
conventions. The fold is aggressive about form and never merges two words that
sound different. (NFC, incidentally, *decomposes* `क़` rather than composing it:
U+0958..U+095F are composition exclusions. Both spellings converge, which is
what is needed, but not by the mechanism it first appears.)

**The turn-detection gate is deliberately asymmetric.** A candidate may trade
dead air for promptness; it may not buy that with more false cuts, in any
language, even when the composite score improves. §19.2 says false cuts are the
more damaging error and the reason is concrete: a dead-air error costs a second
of silence, while a false cut talks over a farmer who cannot tell whether they
were heard, and the agent then answers half a question confidently.

The judge scores register, brevity and helpfulness. **Grounding is computed,
not judged** -- §16.3's validator answers it deterministically, and asking a
model for an opinion about something with a determinate answer is how the
dimension §1 N1 cares most about becomes the least reliable one.

## 6. Known gaps

- **`make dev` has not been run.** Docker is not installed on the development
  machine, so the compose stack itself is unexercised. All thirteen §20
  services are defined, the YAML parses and every path it references exists,
  but no container has been built.

  `make dev-local` is the fallback that *has* run: it starts the same
  embedded PostgreSQL 16 + pgvector the tests use, migrates and seeds it, and
  the API and admin panel run natively against it. A full sign-in --
  password, TOTP enrolment, bearer token -- and every admin screen have been
  exercised end to end that way. What it does not prove is the containers,
  the LiteLLM gateway, or the voice worker under compose.
- **The admin screens are read-mostly, and three §15.1 features inside them
  are absent rather than approximated.** Fifteen of the sixteen screens exist
  with the API routes behind them. What is not built, and why each was left
  out rather than sketched:

  - *Live Calls* lists calls in flight and their turn counts, but has no
    streaming transcript and no listen, barge-in or force-transfer. Each
    needs a live channel out of the media path that does not exist, and each
    is an intervention on a call in progress -- the wrong thing to
    approximate with a control that looks similar and does less.
  - *Knowledge Base* shows documents, chunk counts and embedding progress,
    but has no upload flow and no retrieval playground. A playground that
    showed plausible rankings without running the real hybrid retrieval
    would mislead precisely the non-engineer it exists for.
  - *Flows & Prompts* lists versions, shows one, and publishes -- which is
    what makes the agent able to take calls at all. It has no side-by-side
    version diff and no sandbox test call, so editing is still a database
    operation. A prompt editor that saved without a diff is how an
    unreviewed change reaches every caller.
  - *Catalogue* renders the per-centre grid but not inline editing, CSV
    import/export or price history.
  - *Users & Roles* is a read: no invite, deactivate or session revocation.

- **§15.1's "Call Routing & Numbers" has no schema behind it.** §10 defines
  no table for DIDs, number-to-flow mapping, business hours, holiday
  calendars or transfer chains, so the screen is absent from the navigation
  rather than linking to an empty page. Adding it means adding tables first,
  which is a schema decision rather than a UI one.
- **Deepgram RTT from Mumbai is unmeasured.** §7.1 makes this the deciding
  number for whether Hindi stays on Flux. Phase 2 gate item.
- **No real audio fixtures.** The simulator generates a two-tone placeholder and
  says so. The recorded corpus — noisy Hindi, elderly speech, code-mixing — is
  the Phase 2 evaluation work (§19).
- **All seeded advisory content is `draft`,** so no dosage answer will work until
  an agronomist approves rows. That is the §9 safety control functioning, not a
  defect, but it means the advisory path is inert until real content lands.
- **The §21 Phase 3 retrieval gate stands at 14–15/20 typical, 13 guaranteed —
  not 20/20.** Five misses are recorded in `tests/retrieval_queries.py` with the
  reason for each and `xfail`ed rather than deleted;
  `test_the_gate_does_not_regress` holds the floor so it can only move up. All
  five are broad or paraphrastic questions whose answer the corpus
  *demonstrates* rather than *states*.

  Two queries sit exactly on the top-4 boundary and flip between runs of
  identical code. They are classified by *measured rank*, not by whichever one
  failed most recently: a case sitting at rank four is unstable by construction,
  whether or not it happened to fail today. That is HNSW: an approximate index whose graph is built with
  randomised level assignment, so a database rebuilt from scratch ranks a
  marginal candidate either side of the cut. Raising `hnsw.ef_search` from
  pgvector's default 40 to 200 reduced it to one; embeddings were verified
  bit-identical first, so the index was the only remaining source. At production
  scale the graph is far better connected than it is over eighteen vectors, so
  this should shrink on a real corpus rather than grow.

  Worth being precise about why this stopped at fifteen. Four independently
  correct retrieval fixes landed while the number did not move — the BM25
  AND/OR bug, the index/query configuration mismatch, the missing section
  headings, the wrong section paths on merged chunks. Each was a real defect,
  each changed *which* five missed, none changed how many. That is a corpus
  ceiling, and the remaining move available was tuning against a placeholder,
  which produces a number rather than a working system. Re-measure when UA
  Agro's agronomy content lands.
- **The vendor contract fixtures are written from the protocol
  documentation, not captured from a live session.** They pin our *parsing* of
  the shape and would catch a renamed field -- which otherwise fails silently,
  because every parser correctly ignores messages it does not recognise, so a
  rename makes the recogniser go quiet with no exception anywhere. They cannot
  prove the vendor still sends that shape. One consequence is already visible:
  the Sarvam adapter accepts both `language` and `language_code` for the
  detected language, because which one arrives is unverified and guessing wrong
  means §11.1's LANG_LOCK silently never fires.

- **The knowledge tests need ~1.1 GB of free RAM to run.** They load the e5
  model into an ONNX session, and on a machine short of memory the session
  fails to initialise with `bad allocation` -- 25 errors that look alarming and
  are environmental. `onnxruntime` raises rather than degrading, which is the
  right behaviour; it just reads like a code failure. Free memory and re-run
  before investigating.

- **Retrieval needs a 1.1 GB model download.** `intfloat/multilingual-e5-base`
  is fetched, not vendored (`E5_MODEL_DIR`, `models/` is gitignored). Without it
  retrieval runs BM25-only, and BM25 **cannot** match a Hindi question against an
  English document — measured, not assumed: every Hindi query in the §21 gate set
  returns nothing on the lexical half alone. The result says `degraded: true`
  rather than looking like an empty corpus.
- **A chunker change re-chunks; a re-ingest otherwise does not.** `CHUNKER_VERSION`
  participates in the document hash. Without it, hashing the input alone means an
  improvement to chunking never reaches a corpus that is already loaded, and
  "unchanged" — the expected result of a re-ingest — looks identical whether the
  corpus is current or two versions stale. This bit the test suite before it was
  caught.
- **The knowledge base ingests as a draft.** `uaagro-kb ingest` never publishes;
  `uaagro-kb publish` needs a named approver. §9 says the seed corpus carries
  `[VERIFY]` sections the agronomy team must confirm, so nothing in it reaches a
  caller until someone signs for it.
- **The golden conversations run against a stub gateway, not a model.** 53 of
  the 60 are driven; the other 7 need a dropped socket, a DTMF frame, a live TTS
  failure, or the model's own wording. They are reported as *not run*, never as
  passing — §19.5 makes the pass rate a merge gate, and a gate that scores an
  unimplemented case as green measures nothing.

  What this does and does not prove is worth stating plainly. It asserts the
  deterministic machinery around the model: safety detection, intent routing,
  escalation triggers, tool plans, the output validator. It does not assert the
  model's Hindi, which needs §19.4's LLM-as-judge and vendor keys. The half that
  is covered is the half carrying the consequences — an awkwardly phrased answer
  is a bad call; a safety path that does not fire is what the system exists to
  prevent.
- **No real call has been placed.** §21's Phase 5 gate — a call to the DID,
  answered, resolved, transferred and recorded end to end — needs the Exotel
  account wired and is a human step. Everything below it is exercised: the
  protocol, both codecs, the transfer chain, the recording format.
- **The Plivo and Twilio adapters are unexercised against a live account.**
  Written from the published APIs as §21's portability proof; their request
  shapes are unverified. The *serializers* are tested, because those are pure.
- **Six of the sixteen §15.1 admin screens are built.** Dashboard, Crop
  Advisory, Campaigns (the compliance gate), Settings, plus the navigation and
  RBAC shell. The other ten — Live Calls, Call Explorer, Farmers, Catalogue,
  Knowledge Base, Flows & Prompts, Offers, Routing, Spam, Analytics, Users —
  are not started. §23-12 forbids scaffolding, so they are absent rather than
  stubbed. The ones built are the ones where being wrong has consequences past
  UX: advisory approval is §9's safety control, the compliance gate is §13.1's,
  and vendor keys are §17's.
- **Terraform has never been validated.** No AWS account and no Terraform binary
  on the build machine, so `infra/terraform/main.tf` has not been through
  `terraform validate`, let alone `plan`. The file says so at the top.
- **The restore rehearsal has not run.** §21's Phase 8 gate requires a restore
  *performed and documented*; there is no production database to restore. The
  procedure is in `docs/RUNBOOK.md` and is a go-live prerequisite.
- **The load test stubs the vendors.** 50 concurrent sustained: 50/50 connected,
  first audio p95 283 ms; the ramp stayed healthy to 100 and ran out of rungs
  before the worker degraded. But STT, TTS and the LLM are stubbed, so this is a
  floor on our own overhead and not a prediction of production latency — the
  vendor round trips are most of §7's budget and they are absent.
- **§19.4's LLM-as-judge is not built.** Response quality — correctness,
  register, brevity, grounding — needs a model to rate a model, and both need
  keys. The judge prompt is a Phase 4 deliverable that stays open.
- **`log_intent` does not write.** §6.3 has it called every turn; a database
  round trip per turn spends §7's budget on analytics. It returns the intent to
  the conversation loop, which persists it with the turn it belongs to. The tool
  is honest about this; the write lands in Phase 4 with the turn record.

## Where the panel's time went, measured 2 September 2026

"The website is slow" was reported against the development server, and most of
it was the development server. The same pages, warm, on the same machine:

| Page | `next dev` | `next start` |
|---|---|---|
| Live | 194 ms | 32 ms |
| Calls | 339 ms | 70 ms |
| Centres | 440 ms | 68 ms |
| Flows | 228 ms | 26 ms |
| Data | 4360 ms | 4050 ms |

Every route also pays a one-off compile in development -- Calls was 1.7 s the
first time. None of that exists in the built image, which is what the customer
runs, so the honest answer to most of the report is "look at the build". `make
admin-build` then `npx next start` is the way to see the panel as it will be.

The Data page was the exception: slow in the build too, and slow for reasons
worth fixing.

**Four seconds of it was a probe of an object store that was not answering.**
`/admin/data/connection` pinged Redis and S3 in sequence and returned only when
both had replied or given up. So the page took as long as the slowest dead
service, and the database facts an operator actually came for -- host, pool,
version, all a millisecond away -- waited behind them. The probes now live at
`/admin/data/health`, run at the same time, and are given two seconds each;
the page renders without them and fills the two chips in when they arrive
(`<Suspense>` around `HealthRows`). Table visible at 41 ms against 4050 ms.

**Half a second was `pg_total_relation_size`.** It is not a lookup: it stats
every file of the relation, its indexes and its TOAST. Asking it about every
relation in the schema -- eighteen labelled tables, twenty-one monthly
partitions and everything else -- was enough to hit the in-call statement
timeout on a cold file cache. It now asks only about the tables the page
lists, the exact row counts are one `UNION ALL` rather than eighteen round
trips, and the whole report is measured at most once a minute (the page has
always printed the moment it was counted).

**One round trip per component was the session.** `currentSession()` fetches
`/auth/me`, and the layout, the page and each Server Action called it
separately -- three sequential requests before a page fetched anything it
would show. It is wrapped in React's `cache` now: one render, one lookup. On a
deployment where the panel and the control plane are different containers,
that is three network hops saved per navigation rather than three loopback
calls.

**The Devanagari display face** was loaded on every page for one word on the
sign-in screen. It is imported by that screen now.

## One knowledge base, two kinds of call

The outbound agent could always answer questions -- press 2 on an offer call
hands the farmer to the same agent, with the same tools, as the helpline
(§13.2). The panel offered the knowledge base under Inbound alone, which made
it look as though offer calls had none, and left no way to say that a
campaign's own material is not helpline material.

`kb_documents.scope` says where a document is used: `inbound`, `outbound`, or
`both`, which is the default and what every existing row was given. The filter
is in retrieval, not in the panel -- `HybridRetriever.search(scope=...)`
narrows both the lexical and the dense query, and the direction reaches it
through `ToolContext.direction`, set when the agent is built for a call. A
document out of scope is not merely hidden from a list; the agent cannot reach
it. `tests/test_knowledge.py` asserts that in both directions, and asserts that
`both` stays reachable from either, because getting that default wrong would
quietly narrow every document ever uploaded.

The panel shows the same corpus from both sides: Inbound → Knowledge base and
Outbound → Knowledge, each listing what its calls can reach and counting what
is kept for the other. Uploads default to `both` from either side. Defaulting
to the side the operator happens to be standing on would make the product
catalogue helpline-only because that is where somebody uploaded it, and the
failure would surface much later, as an offer call that cannot answer a
question about a product.

## Contact lists arrive as spreadsheets

The campaign form takes a pasted list, and that is still the only thing a
campaign is created from -- one validated path, one place where a number
becomes an MSISDN. But nobody keeps farmer numbers as pasted text. A CSV used
to be read in the browser; spreadsheets could not be read at all, so the
operator was expected to open Excel, select a column and paste, which is how
numbers get lost a row at a time.

`POST /admin/campaigns/contacts/extract` reads `.xlsx`, `.csv`, `.tsv` and
plain text in Python (`services/contact_import.py`) and hands back the same
`name, number` lines for the operator to look at before anything is created.
Reading it on the server rather than in the browser is deliberate: a workbook
is a zip of XML, and "which cell is the phone number" is the same judgement
the pasted-list parser already makes -- one implementation, in one language,
with tests.

Three things real spreadsheets do, each of them a farmer who would not be
called:

- **A number stored as a number.** Excel writes 9876543210 as a float, so the
  obvious `str(value)` yields "9876543210.0" and the last digit silently
  becomes a zero.
- **A header row.** Dropped by not matching, never by position: a file whose
  first row is data would otherwise lose a contact.
- **A name with a comma in it.** "Yadav, Ramesh" would split the line the
  parser reads, so the comma inside the name goes rather than the name.

`.xls`, the pre-2007 binary format, is refused with instructions to save as
`.xlsx` -- openpyxl cannot read it, and "that file is broken" would be a lie.

## The operations panel, and what it forced into the media path

The panel was rebuilt to six sections -- Live, Calls, Outbound, Inbound, Flows,
Data -- against a written contract, `docs/ADMIN_API.md`. Building it exposed
three things the media path had never done, each of the "built but never
wired" shape this document keeps recording.

**Turns were never persisted.** `call_turns` existed from the first migration
and nothing wrote to it. The post-call pipeline "consolidated" an empty table
and the transcript lived only in the worker's memory. The conversation
pipeline now reports every finished turn to the session (`on_turn`), which
writes the farmer's half and the agent's half as two rows -- the agent's
carrying the §7 latency breakdown -- off the audio path through the same drain
queue that persists events.

**The outbound script was never spoken.** `OutboundAgent` had tests and no
caller; every call, placed or received, ran the inbound LLM agent. The session
now decides the direction from the provider's start frame
(`runtime/direction.py`: the dialer's custom field first, our own numbers
second) and the assembly builds an `OutboundResponder` in place of the LLM
agent. The responder walks the panel-edited script; the LLM agent stands
behind it to answer questions the farmer asks (press two, or anything that is
not a yes, a no or an opt-out), then the script resumes. A "no" is accepted
the first time. Opt-out writes `internal_dnc` before the confirmation is
spoken. When the closing line has played, the session ends the call itself.

**There was no live channel out of the media path.** The Live page needs the
transcript as it happens. `uaagro_domain.livefeed` publishes call, turn,
keypress and end events through a bounded in-process queue to a Redis channel;
the API relays them over server-sent events, filtered to the caller's centres,
after sending a database snapshot. Publishing never blocks the audio loop: a
full queue drops and counts. The dialer and the post-call pipeline publish
contact and campaign events on the same channel, which is what turns a card
amber and then green.

Two smaller findings from the same work: the dense half of retrieval was never
loaded in the worker (the registry got a retriever with no embedder, so every
production search was BM25-only and said so); it now loads in the background
at start. And promotional campaigns need a DLT template id the settings did
not carry; `DLT_TEMPLATE_ID` joins `DLT_ENTITY_ID`, and the gate refuses a
campaign until both are set, which the panel names.

The knowledge base grew an ingestion path from the panel: upload or website
address, stored in the bucket, extracted (`knowledge/extract.py`: PDF, Word,
CSV, text, a bounded same-site crawl), chunked and embedded by the background
worker, with the document row as the progress report.

## The hand-over, and what was missing from it

Until 2 September 2026 the escalation engine decided that a caller needed a
person, the transfer tool decided which person, and nothing joined the two:
the caller heard "मैं आपको जोड़ रहा हूँ" and then silence, because no code path
reached the provider's call control, `calls.was_transferred` was never set,
and the panel's "handed to a person" count could only ever read zero. The
pieces existed; the seam between them did not.

The seam is three small parts, each testable alone (`tests/test_transfer.py`):

- **The agent asks the tool who takes the call.** An immediate escalation
  (§12.1) and the safety script (§16.1) now run `transfer_to_human` before
  the line is spoken, so the caller hears the tool's own line -- which names
  the centre -- rather than a placeholder. The tool's answer rides on the
  turn as a `TransferRequest`; when nobody is reachable the caller hears a
  callback commitment instead (§12.3-6) and the turn carries no request.
- **The pipeline waits for the line.** `ConversationPipeline.on_transfer`
  fires only after the transfer line has been played (§12.3-1), with the
  request. A pipeline also has `announce()` for a line that answers nothing:
  the fallback spoken when the provider refuses.
- **The session owns the phone line.** `CallSession._on_transfer` builds
  call control for exactly one approved destination (§17), asks the provider
  to transfer with the whisper, marks the call transferred with
  `transfer_completed` true, publishes `call.transfer` for the Live page and
  sets the outcome. A refusal marks the call transferred-but-not-completed,
  which is the condition the post-call job already treats as "open the
  follow-up", and the caller hears the commitment.

The destination number never enters the model context or `call_turns`:
`ToolResult.to_dict` drops `target_number` before either sees it. Whether
Exotel accepts the transfer request as written is a Phase 5 item -- the
shape is from the published API and a real hand-over is the check
(`docs/VERIFICATION.md`, step 3).

## The turns the model never sees, measured 3 September 2026 (afternoon)

The morning's work had cut a turn from 3.5 s to about 2.5 s and the proxy's
1.3–1.7 s first token was the wall. The afternoon's transcripts (persisted at
last; see below) showed that the wall was not the worst of it. A farmer asked
about potato seed, then "स्टॉक है क्या?", and the agent said the seed was
out of stock and would arrive next week. It had looked nothing up: the stock
tool had never been called (only `search_products` ever ran, and it returns
no stock), and could not have answered anyway because no centre had been put
on the tool context. The model was given a question and no facts, and it
did what models do.

Three changes, in the order they matter.

### A centre for every call

`assembly.build_call_pipeline` now resolves the centre the agent answers
*for*: the farmer's own when the caller is known and assigned, else the
organisation's **primary centre** (`centres.is_primary`, one per organisation,
enforced by a partial unique index; Lucknow in the seed). It travels on
`ToolContext.centre_id`, on `CallerContext.centre`, and as `CentreFacts` on
the agent, which says so when it quotes the head office's stock rather than
the caller's.

### Focus, and answers from data (`flow/focus.py`, `flow/direct.py`)

`ConversationFocus` is three slots — the product, the kind of product, the
crop — filled from each user turn by the catalogue vocabulary and a short
crop list, so that "स्टॉक है क्या?" two turns after "आलू का बीज" is a
question about the potato seed. Crop names were also removed from the
lexicon's *spoken forms*: "potato" answered to a seed, a fungicide and a
spray at once, and resolved to whichever came first by SKU.

`DirectAnswers` handles the intents whose answer is a row: price, stock,
composition, the centre's address and hours, "what do you have", "can you
hear me", "are you a machine". It resolves the product (words → catalogue
vocabulary → kind + crop → the focus), calls the same tools the model would
(`check_availability` with the centre code, `get_product_details`,
`find_nearest_centre`), and composes the reply from templates in the
farmers' register: whole rupees, "50 किलो का बैग", "लखनऊ सेंटर" rather than
the registered name, a restock date or an alternative when out of stock, a
question back when the product cannot be resolved, and a hand-over offer
("जोड़ दूँ?") for a licensed product — a "हाँ" on the next turn is then a
request for a person. Nothing in it is a model, so nothing in it can be wrong
in a new way each time, and it cannot invent a stock-out.

Measured on a persisted five-turn socket call (speech end → first reply
audio): price 912 ms, crop + kind 683 ms, bare stock question 395 ms, centre
605 ms, goodbye 776 ms. `calls.latency_stats` for the call: p50 321 ms, p95
558 ms. What remains in those numbers is Soniox's endpoint and Bakbak's
first byte; the model is not in them.

### The register, after the model (`text/register.py`)

For the turns that still need the model — advice, problems, schemes,
anything from the knowledge base — the persona asks for the Hindi farmers
speak and the model mostly complies. Mostly is not enough, so
`farmers_register` runs on every generated sentence: a fixed table of the
textbook words a model reaches for and the everyday word ("उर्वरक" → "खाद",
"मूल्य" → "रेट", "उपलब्ध है" → "स्टॉक में है", "केंद्र प्रबंधक" → "सेंटर
मैनेजर", "प्रतीक्षा" → "इंतज़ार" …), and stage directions in asterisks,
brackets or parentheses removed — a synthesiser had been reading "(मैनेजर से
कनेक्ट करने का प्रयास)" out loud. The filler check runs first, on the model's
own words; the swap runs on what survives. The persona gained a "knowledge
boundary" section: rate and stock are not the model's to state, advice comes
only from `<reference>`, and what is not there is "पक्की जानकारी अभी मेरे पास
नहीं है" plus the offer of a person.

### Recordings exist now (`runtime/recording.py`, `uaagro_db/storage.py`)

`RecordingBuffer` had been written and never wired. The session now appends
both legs as frames pass (a few bytes per frame, on the audio path) and, once
the line has closed, writes a two-channel WAV to the object store and puts the
key on `calls.recording_object_key` — before the post-call job is queued, so
the job finds it. The store has two backends behind one contract:
`STORAGE_BACKEND=s3` (the bucket, SSE-KMS when a key is configured) and
`STORAGE_BACKEND=local` (a directory, `STORAGE_LOCAL_DIR`; development,
tests, a pilot without a bucket). Production refuses `local`. The panel's
recording route streams from either; no URL is ever produced.

### Browser calls are simulator calls; simulated dials are answered in the browser

The test page announces itself (`?source=browser`) and its calls are recorded
with `provider = simulator`: kept, listed, playable, and counted apart from
real traffic. `TELEPHONY_PROVIDER=simulator` selects a `SimulatorAdapter`
whose `originate` dials nothing and returns a synthetic id; the campaign
contact stays ringing, the panel shows an "answer in browser" link
(`/dev/call?answer=<contact id>`), and the page sends the contact reference in
the start frame's `custom_parameters` exactly where Exotel would echo it. From
there the worker runs what it would run for a real line: the outbound script,
the farmer identified from the contact when the number is not usable, the
recording, the live feed, the contact's outcome. A transfer is refused rather
than faked, so the callback commitment is what gets rehearsed.

### The client's database as a source

UA Agro keeps stores, products and stock in MySQL. The platform stays on
PostgreSQL — row-level security, vector search and the monthly partitions
depend on it — and *pulls*: `data_sources` names the server and a mapping
from their tables to ours, `data_source_runs` records each pull, and a sync
upserts centres by code, products by SKU and stock by (centre, variant),
never deleting. The password is ciphertext under the platform data key. The
routes and the mapping's field lists are in `docs/ADMIN_API.md`.
