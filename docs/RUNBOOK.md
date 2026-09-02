# Runbook

For the person on call. Written to be read at 3am by someone who did not build
this, so every procedure states what to check, what it means, and what to do —
in that order, and without assuming you remember the architecture.

Each section is the target of an alert's `runbook` annotation in
`infra/prometheus/alerts.yml`. If an alert fires and there is no section here,
that is a gap worth filing.

**The first question in every incident is the same:** are farmers being served?
The agent degrades in layers, and most failures leave it partially working.
Knowing which layer is down tells you whether this is a page or a morning job.

| Layer | If it fails | Callers still get |
|---|---|---|
| Telephony | Nothing | Nothing. This is the only total outage. |
| STT / TTS | The pipeline | A busy tone. Page. |
| LLM gateway | Generated answers | Safety detection, transfers, the answer cache. Degraded, not down. |
| Retrieval (dense) | Cross-lingual recall | BM25 answers and every Tier-1 tool. Morning job. |
| Embedding model absent | Dense retrieval entirely | As above, and the result says `degraded: true`. |
| Postgres | Everything factual | Nothing useful. The agent cannot ground a single claim. Page. |

---

## latency

**Alert:** `TurnLatencyP95Breach`, `TurnLatencyWithToolP95Breach`

§7 makes the p95 ceiling a build-failing gate, so a sustained breach in
production is a real regression rather than a slow afternoon.

1. **Open the per-segment panel first** (Live Operations → *Latency by
   segment*). One segment over its own ceiling narrows this to one vendor in
   about ten seconds. All segments up together is a resource problem on our
   side — check CPU and the concurrency panel.
2. **`llm_ttft` high** → check the fallback rate. If turns are landing on the
   secondary model, the primary is the problem and calls are still being
   served; this is a warning, not a page. If both are slow, the gateway or the
   network to it is the problem.
3. **`stt_final` high on Hindi only** → the recogniser's round trip from
   Mumbai. Soniox is not India-hosted; Sarvam is. §7.1 anticipated exactly this
   and names the remedy: move the affected route to Sarvam STT in
   `config/defaults.yaml`. Note that Sarvam does *not* decide the turn, so the
   route also needs `turn: { strategy: smart_turn_v3 }` — `build_speech_stack`
   refuses the pair otherwise, which is the check doing its job rather than an
   obstacle. It is a config change, not a deploy.
4. **`tool_execution` high** → Postgres. Check `pg_stat_activity` for the
   in-call engine's 120 ms `statement_timeout` firing; a query that is timing
   out repeatedly is usually a missing index after a data-shape change.

**What not to do:** do not raise the ceilings to clear the alert. The numbers
in `telemetry.P95_CEILING_MS` are the same ones the CI gate uses, so raising
them silently disables the regression test as well.

---

## vendor-outage

**Alert:** `SttVendorFailing`, `LlmRunningOnFallbackModel`

1. **Check the vendor's own status page before anything else.** Most of these
   are not ours.
2. **STT down** → the pipeline cannot run. Calls will fail. There is no
   graceful degradation for this one. Every language except Odia is on Soniox,
   so a Soniox outage is a full outage: switch the affected routes in
   `config/defaults.yaml` to a vendor that is up and restart the workers
   (rolling; see *scaling*). Deepgram Flux covers hi/en and keeps
   `flux_semantic`; Sarvam covers the rest and needs a local turn strategy with
   it. Both adapters stay wired and tested for this reason — but neither key is
   required to run, so check that the fallback vendor's key is actually set
   before switching a route to it.

   **TTS down** is survivable for longer than it looks: §16.1's safety script,
   the hold phrase, the fallback line and the greeting are pre-rendered into the
   process-wide audio cache at boot, so a worker that is already up keeps
   speaking those. Anything generated goes silent. Do not restart the workers to
   "fix" it — a restart is what loses the cache.
3. **LLM down entirely** → the agent still detects safety emergencies,
   recognises a request for a human, and serves the Tier-3 answer cache. It
   cannot answer anything else, and §11.4 sends those calls to a person.
   Warn the centre managers that transfer volume is about to rise.
4. **WhatsApp down** → §13.2 has the agent read the offer aloud and raise a
   ticket, so no farmer is left uninformed. The ticket queue absorbs it. Check
   the queue depth is being worked.

---

## scaling

**Alert:** `ConcurrencyCeilingApproaching`

§3 expects a 5–10× seasonal peak, so this fires during sowing and harvest and
is expected rather than surprising.

1. Workers are stateless per call (§4.1): durable state is in Postgres,
   ephemeral state in Redis keyed by `call_id`. **Adding a worker needs no
   coordination.**
2. Scale out, then confirm on the concurrency panel that new capacity is being
   taken up.
3. Shutdown is drain-not-kill: a worker stops accepting new calls and finishes
   the ones it has. Do not `SIGKILL` a worker with calls on it — those callers
   are mid-conversation.
4. **Measured ceiling:** at least 100 concurrent calls per worker (see *Load
   test results*), with vendors stubbed. Scale before 85% of it, not at it: a
   farmer who gets a busy tone during sowing does not call back. Treat 100 as
   optimistic until the vendor round trips are in the measurement.

---

## compliance-breach

**Alert:** `CallOutsideCallingWindow`

This is a legal boundary, not a quality metric, and every occurrence is a
TCCCPR violation per call.

1. **Stop the running campaigns.** `PATCH /admin/campaigns/{id}` with
   `status=paused`, or the Pause control on the campaign screen. Pausing stops
   new dials and lets in-flight calls finish.
2. The dialer enforces the window at *dial time* (`outbound.compliance
   .within_calling_window`). An occurrence means that check was bypassed —
   which should be impossible through the gate, so look for a code path that
   dials without it.
3. Record the incident: how many calls, over what period, to how many distinct
   numbers. That is what a regulator asks for.
4. Do not resume until the bypass is found. "It has stopped happening" is not a
   diagnosis.

---

## audit-chain

**Alert:** `AuditChainBroken`

§17 makes the audit log tamper-evident: `row_hash = SHA256(prev_hash ‖
canonical_payload)`, ordered by a Postgres sequence. A break is either
corruption or someone editing history.

1. **Do not repair it.** A repaired chain is a chain that proves nothing, and
   repairing it destroys the evidence of what changed.
2. `uv run uaagro-db verify-audit` reports the first sequence number where the
   chain diverges. Everything before it is intact.
3. `uaagro_app` has INSERT but not UPDATE or DELETE on `audit_log`, so a break
   implies either direct database access with another role, or storage
   corruption. Check which by looking at whether the row's own hash is
   self-consistent: a corrupt row usually fails its own hash, an edited one
   usually does not.
4. Escalate to whoever owns the database credentials. This is a security
   incident until shown otherwise.

---

## safety-emergency

**Alert:** `SafetyEmergencyTriggered`

**This is not a system fault.** It means a farmer reported a poisoning,
ingestion or chemical exposure on a call and §16.1's path fired correctly. The
alert exists so a human sees it inside the hour §16.1 requires.

1. Find the call: Call Explorer, filter outcome = transferred, reason =
   `safety_emergency`. There will be a P0 ticket linked to it.
2. **Confirm the transfer actually completed.** §16.1 bypasses the hours check,
   the busy check and the daily cap — but if the whole chain was unreachable,
   the caller got a callback commitment instead, and someone needs to make that
   call now rather than tomorrow.
3. Listen to the recording. §16.1 requires mandatory human review within one
   hour, and this is it.
4. If the agent said anything resembling medical advice, that is a serious
   defect: the script is a fixed string reached by a cache key and the model is
   not on that path. File it immediately.

---

## Deploy and rollback

Blue/green (§20). The worker holds live WebSocket connections, so a deploy is
never a restart.

```bash
make deploy            # terraform plan, reviewed by a human
```

1. Bring up green alongside blue. Both connect to the same Postgres and Redis.
2. Shift the telephony webhook to green. **New** calls land on green; calls in
   flight stay on blue.
3. Watch the latency and error panels for one full call length (about three
   minutes) before draining blue.
4. Drain blue: stop accepting, let calls finish, then stop the process.

**Rollback** is the same procedure in reverse and takes as long as the longest
call in flight. There is no faster path that does not drop somebody
mid-sentence.

**Migrations run before the deploy, never during**, and must be
backward-compatible with the running version — blue and green are live
simultaneously. A migration that drops a column blue still reads will take blue
down while it is serving calls.

---

## Restore rehearsal

§21's Phase 8 gate requires a restore to have been *performed and documented*,
not merely configured. A backup nobody has restored is a hypothesis.

```bash
# 1. Restore into a scratch database, never over the live one.
pg_restore --dbname=uaagro_restore_test --clean --if-exists <dump>

# 2. The schema arrived.
psql uaagro_restore_test -c "\dt" | wc -l

# 3. The RLS policies came with it. A restore that drops these is a restore
#    that silently removes centre isolation.
psql uaagro_restore_test -c \
  "SELECT tablename FROM pg_policies ORDER BY tablename"

# 4. The audit chain still verifies across the restore boundary.
DATABASE_URL=...uaagro_restore_test uv run uaagro-db verify-audit

# 5. Partitions exist for the current month. An insert with no matching
#    partition fails, and that failure lands in the audio path.
psql uaagro_restore_test -c \
  "SELECT relname FROM pg_class WHERE relname LIKE 'calls_%' ORDER BY relname"
```

Record the date, the dump's age, the wall-clock restore time and the row counts
in `docs/COMPLIANCE.md`. The restore *time* is the number that matters during an
incident, and it is the one nobody measures until they need it.

### Status: rehearsed against a seeded database, 1 September 2026

`tests/test_restore_rehearsal.py` performs the whole cycle on every CI run:
`pg_dump --format=custom` of the seeded database, `pg_restore` into a fresh
one, then the five checks above. Measured on the development machine, a seeded
database restored in **2.3 seconds**.

That number is not the one that matters for an incident -- a seeded database is
a few thousand rows and production will be millions -- but the *procedure* is
now exercised continuously rather than annually, and the checks are the ones
that catch a silently wrong restore:

- Row counts match table for table.
- The RLS policy count matches, **and** `relforcerowsecurity` survived. A dump
  taken with the wrong flags restores the rows and drops the policies; the
  result looks perfect and a centre manager can read every centre's calls.
- `pg_partitioned_table` is non-empty, so `calls` came back partitioned. A
  plain-table restore works until the first insert past the month boundary,
  which happens in the audio path.
- The audit chain verifies across the restore boundary.

**Still outstanding for go-live:** the same rehearsal against a production-sized
snapshot, to get a real restore time. Record the date, the dump's age, the
wall-clock time and the row counts in `docs/COMPLIANCE.md` when that runs.

---

## Load test results

```bash
uv run pytest tests/load -m load -s
```

**Measured 31 August 2026**, single machine, worker and harness in one process:

| Concurrency | Connected | First audio p50 | p95 | Verdict |
|---|---|---|---|---|
| 10 | 10/10 | 29 ms | 32 ms | OK |
| 25 | 25/25 | 75 ms | 101 ms | OK |
| **50 sustained 60 s** | **50/50** | **160 ms** | **283 ms** | **OK — §21's gate** |
| 75 | 75/75 | 233 ms | 471 ms | OK |
| 100 | 100/100 | 386 ms | 765 ms | OK |

**The ceiling is at least 100 concurrent calls, not exactly 100.** The ramp ran
out of rungs before the worker degraded — nothing failed and p95 was still
inside §7's 1,200 ms at the top of the ladder. Extend the ladder in
`tests/load/test_concurrency.py` if a real number is needed; the honest
statement today is "≥100, untested above".

Within §7's ceiling with a wide margin — **but read the next paragraph before
quoting that number.**

The harness stubs STT, TTS and the LLM. Fifty concurrent live calls against
Deepgram and Sarvam costs real money on every run and would measure their
capacity rather than ours. So this is a floor on our own overhead and not a
prediction of production latency: the vendor round trips in §7's budget are
absent, and they are most of it. §7's real gate needs vendors in the loop and a
live account.

The harness also runs in the same process as the worker, so its own send pacing
is dominated by asyncio scheduling and Windows' ~15 ms timer resolution. That
number is printed and deliberately not asserted — a first draft asserted 80 ms
and failed at 87 ms, which was a property of the test rather than of the system.

---

## Known operational gaps

Listed so nobody discovers them during an incident.

- **`make dev` is unexercised.** Docker was not available on the build machine.
  Everything the stack would prove about the schema was verified against an
  embedded Postgres instead (see `docs/ARCHITECTURE.md`).
- **No real call has been placed.** §21's Phase 5 gate — a call to the DID,
  answered, resolved, transferred and recorded — needs the Exotel account wired
  and is a human step.
- **The Plivo adapter is unexercised.** Written as §21's portability proof from
  the published API; no Plivo credentials exist for this project. Its request
  shapes are unverified.
- **The restore rehearsal runs in CI against a seeded database** (2.3 s).
  What has not happened is a rehearsal at production scale, which is where
  the restore *time* becomes a real number. See above.
- **Terraform has never been applied.** `infra/terraform` describes the
  intended `ap-south-1` footprint and has been validated but not planned
  against a real account.
