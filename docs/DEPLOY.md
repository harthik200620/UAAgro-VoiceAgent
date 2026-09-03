# Deploying to the customer

One virtual machine runs the whole platform for up to 30 concurrent calls.
Everything below is what an engineer does once; after that the customer runs
the business from the panel and never touches a server.

## 1. What you need before you start

| Item | Where it goes |
|---|---|
| A Linux VM (Ubuntu 24.04, 4 vCPU, 8 GB RAM, 60 GB disk) in `ap-south-1` or another Indian region | the host |
| Two DNS names pointing at the VM: `panel.<domain>` and `voice.<domain>` | `PANEL_HOST`, `VOICE_HOST` |
| Docker Engine 27+ with the compose plugin | the host |
| Soniox, Bakbak and Anthropic keys | `SONIOX_API_KEY`, `BAKBAK_API_KEY`, `ANTHROPIC_API_KEY` |
| Exotel account: SID, key, token, the inbound DID and a 140-series promotional CLI | `EXOTEL_*`, `INBOUND_DID`, `OUTBOUND_CLI_*` |
| DLT registration: the principal entity id and the approved promotional template id | `DLT_ENTITY_ID`, `DLT_TEMPLATE_ID` |
| A KMS key (AWS) for the phone-number data keys, or, for a single-host install, a locally generated data key | `KMS_KEY_ID` or `LOCAL_DEK_BASE64` |

Generate the secrets that are yours to choose:

```bash
openssl rand -hex 32      # PHONE_HASH_PEPPER
openssl rand -hex 32      # JWT_SIGNING_KEY
openssl rand -hex 32      # ADMIN_SESSION_SECRET
openssl rand -hex 24      # TELEPHONY_WS_TOKEN
openssl rand -hex 24      # INTERNAL_API_TOKEN (same value on API and voice worker: one variable)
openssl rand -hex 16      # POSTGRES_PASSWORD, REDIS_PASSWORD, S3_SECRET_ACCESS_KEY
```

## 2. Configure

```bash
git clone <this repository> uaagro && cd uaagro
cp .env.example .env
```

Fill every `FILL_ME` in `.env`. Set `APP_ENV=production`, `SESSION_COOKIE_SECURE=true`,
`PUBLIC_BASE_URL=https://voice.<domain>`, and the two hostnames. The database and
Redis URLs for the compose stack are:

```
DATABASE_URL=postgresql+asyncpg://uaagro_app:<POSTGRES_PASSWORD>@postgres:5432/uaagro
DATABASE_URL_MIGRATOR=postgresql+asyncpg://uaagro:<POSTGRES_PASSWORD>@postgres:5432/uaagro
REDIS_URL=redis://:<REDIS_PASSWORD>@redis:6379/0
S3_ENDPOINT=http://minio:9000
```

The secret scan refuses a commit that carries any of these values; `.env` is
ignored by git and must stay that way.

## 3. First start

```bash
docker compose --env-file .env -f infra/docker/docker-compose.prod.yml build
docker compose --env-file .env -f infra/docker/docker-compose.prod.yml up -d
docker compose --env-file .env -f infra/docker/docker-compose.prod.yml run --rm migrate uaagro-db seed
```

The seed creates the organisation, the ten centres, the catalogue, one user per
role and an unpublished script per direction. It is idempotent.

Then, in the panel at `https://panel.<domain>`:

1. Sign in as `admin@uaagro.in` with the password printed by the seed, scan the
   two-step key into an authenticator app.
2. **Flows → Inbound**: read the greeting, publish v1.
3. **Flows → Outbound**: write the first message, publish.
4. **Inbound → Knowledge base**: upload the product catalogue and the crop
   documents, or give the company website address.
5. **Inbound → Centres**: confirm each manager's number.

The worker downloads the embedding model (about 1.1 GB) on first start; until it
is loaded the knowledge base answers from word matching only and the panel's
"try a question" says so.

## 4. Point the telephony at it

**Before any of this: the Voicebot applet has to be switched on for the
account.** It is not available by default — a new account has no Stream or
Voicebot applet in the list, and there is no way to add one from the console.
Exotel enables it after KYC, on request:

- Submit the company documents through standard onboarding.
- Email `hello@exotel.com`, subject **"Enable Stream/Voicebot Applet for
  &lt;ACCOUNT SID&gt;"**, with a line on the use case.

The two can run in parallel. Until it is done, the flow below cannot be built,
so it is the first thing to start and the longest to wait for.

Then, in Exotel, create a flow in **App Bazaar** containing a **Voicebot** applet pointed at
`wss://voice.<domain>/ws/voice?token=<TELEPHONY_WS_TOKEN>`, followed by a Hangup
applet, and attach it to the inbound DID.

Outbound calls use the same flow, and this is the step people miss: Exotel's
connect API is given the address of the **flow**, not of the socket. Take the
flow's numeric id from its URL — `my.exotel.com/<sid>/exoml/start_voice/<id>`
— and set it as `EXOTEL_APP_ID`. A dial that is handed the stream address
instead is accepted, rings, connects to silence and bills; the dialer refuses
to place a call without the flow id for that reason.

Set `TELEPHONY_IP_ALLOWLIST` to Exotel's published media IP ranges so the socket
accepts nobody else.

Make one call to the DID. In the panel's Live page the call appears within a
second of ringing; the transcript grows turn by turn; the recording is on the
call page a minute after hang-up. That call is §21's Phase 5 gate;
`docs/VERIFICATION.md` lists what to check on it and on the first outbound call.

## 5. Day two

| Task | How |
|---|---|
| Deploy a new version | `git pull && docker compose ... build && docker compose ... up -d` — the voice worker drains live calls for up to ten minutes before stopping |
| Roll back | `git checkout <previous tag>` and the same two commands; migrations are backward-compatible one step |
| Back up | Nightly `pg_dump` of the `postgres` volume to object storage: `docker compose ... exec postgres pg_dump -U uaagro uaagro | gzip > uaagro-$(date +%F).sql.gz`, then `mc cp` to the bucket. Recordings are already in the bucket. |
| Restore | `docker compose ... exec -T postgres psql -U uaagro uaagro < dump.sql` on an empty database; see `docs/RUNBOOK.md` for the rehearsal |
| Scale to 100 calls | A second voice-worker replica behind Caddy (`deploy.replicas: 2`), managed Postgres and Redis (`infra/terraform`), and `MAX_CONCURRENT_CALLS=100` |
| Change the database | Test the new address on the panel's Data page, then set `DATABASE_URL` in `.env` and `up -d` |

## 6. What is not automated

- **The DLT template.** Promotional calls need a template registered with the
  telecom regulator under the customer's entity. Until `DLT_TEMPLATE_ID` is set
  the compliance gate refuses every campaign and the panel names the reason.
- **Certificates** are Caddy's job and need ports 80 and 443 open to the world.
- **The Bakbak voice ids** (`BAKBAK_VOICE_HI`, `BAKBAK_VOICE_EN`) are chosen in
  the §21 Phase 2 bake-off, not from documentation.

## Model files

Three files live under `models/`, which is gitignored; each is fetched once
per host and mounted or copied into the worker's working directory:

| file | what | from |
|---|---|---|
| `models/multilingual-e5-base/` | the retrieval embedder (§9) | `uv run huggingface-cli download intfloat/multilingual-e5-base` |
| `models/smart-turn-v3.1.onnx` | Smart Turn, for routes without vendor endpointing (§5.2) | `uv run huggingface-cli download pipecat-ai/smart-turn-v3` |
| `models/silero_vad.onnx` | Silero VAD v5, the barge-in voice gate (§5.4) — optional | `github.com/snakers4/silero-vad`, `src/silero_vad/data/silero_vad.onnx` (MIT, 2.3 MB) |

Without the Silero file the gate falls back to its spectral rule and says so
at startup (`vad.judge` is absent). With it, `vad.judge judge=silero` appears
once when the first call is built.

## Storage, the demo line and the background worker (added 3 September 2026)

| Variable | Development | Production |
|---|---|---|
| `STORAGE_BACKEND` | `local` — recordings and uploads under `STORAGE_LOCAL_DIR` (default `.localdev/objects`) | `s3` (required; the process refuses to start on `local`) |
| `TELEPHONY_PROVIDER` | `simulator` — the panel's *Call now* is answered from the browser test page, nothing is dialled | `exotel` (or `twilio`, `plivo`) |
| `OUTBOUND_QUICK_DIAL_SELF_APPROVE` | `true` — a quick dial is approved by the person who placed it | must be `false`; the four-eyes rule holds |

The background worker (`arq worker.tasks.WorkerSettings`, the `worker`
service in the compose file) is not optional: it indexes knowledge documents,
writes the post-call summary, runs the dialer and the scheduled MySQL syncs,
and refreshes the heartbeat the panel's Knowledge page shows. Without it,
uploads stay *pending* and the Overview's attention list says so.

Recordings are written by the voice worker at the end of each call (both
legs, WAV) and streamed by the API from the configured store; in the bucket
they carry `delete-after` metadata for the lifecycle rule in `docs/RUNBOOK.md`.
