# Deploying to the customer

One virtual machine runs the whole platform for up to 30 concurrent calls.
Everything below is what an engineer does once; after that the customer runs
the business from the panel and never touches a server.

## 0. The short version (added 4 September 2026)

Any cloud, one Ubuntu 24.04 VM, two DNS names, one afternoon:

```bash
git clone <this repository> uaagro && cd uaagro
cp infra/deploy/production.env.example .env      # fill every FILL_ME
make preflight                                   # refuses placeholders, plaintext URLs, missing controls
sudo infra/deploy/bootstrap.sh                   # Docker, firewall, build, start, migrate, seed, TLS
```

`bootstrap.sh` ends by fetching `https://panel.<domain>/en` and
`https://voice.<domain>/health/ready` over the certificates Caddy just
obtained, and prints the next steps. Every later release is
`infra/deploy/deploy.sh` (or `make deploy`): pull, preflight, build, roll,
migrate, health. Nightly `infra/deploy/backup.sh` puts a database dump in the
recordings bucket.

### What "secure" means here

| Layer | What is in place |
|---|---|
| Transport | Caddy terminates TLS for both hostnames with Let's Encrypt certificates it obtains and renews itself; HTTP redirects to HTTPS; HSTS for a year; HTTP/3 on 443/udp. The media stream is `wss://`. |
| Exposure | Only Caddy publishes ports (80, 443). The API, Postgres, Redis, MinIO and the worker's `/internal` routes are on the compose network only. The voice hostname answers `/ws/voice`, `/telephony/*` and the two probes; everything else is 404 at the edge. The firewall (`ufw`) allows SSH, 80 and 443 in and nothing else. |
| Headers | `Strict-Transport-Security`, `X-Content-Type-Options`, `X-Frame-Options DENY`, `Referrer-Policy`, `Permissions-Policy` at the edge; a `default-src 'none'` CSP and `no-store` from the API; the `Server` header removed. |
| Sessions | Argon2id passwords, mandatory TOTP, 15-minute access tokens, rotating refresh tokens in `httpOnly; Secure; SameSite=Lax` cookies with reuse detection; the panel's own session cookie is `httpOnly` and `Secure` in production. |
| Telephony | The media WebSocket requires the shared token *and* a source address in `TELEPHONY_IP_ALLOWLIST` (the provider's media ranges). The worker reads the real client address from Caddy's forwarded headers. |
| Data | Phone numbers are HMAC-indexed with a pepper and AES-256-GCM encrypted under a data key from KMS (or, on a host with no KMS, from `.env` with `ALLOW_LOCAL_DEK_IN_PRODUCTION=true`, said loudly at boot). Row-level security is forced in Postgres and the application connects as a non-owner role. Recordings go to the bucket with server-side encryption. |
| Boot-time refusal | A production process refuses to start without the pepper, the signing key, the WebSocket token, the internal token, a data key, `SESSION_COOKIE_SECURE=true`, `STORAGE_BACKEND=s3` and the four-eyes rule on; `make preflight` catches the same before Docker is even involved. |
| Containers | Non-root users, no shell tools beyond Python, log rotation, the voice worker drains live calls for ten minutes on stop. |

What is deliberately not here: a WAF, DDoS protection and DNS are the cloud
provider's layer (put the panel hostname behind their proxy if you want them);
audit-log shipping and alert routing are in `docs/RUNBOOK.md`.

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
cp infra/deploy/production.env.example .env
```

Fill every `FILL_ME` in `.env`; the template already carries `APP_ENV=production`,
`SESSION_COOKIE_SECURE=true`, `STORAGE_BACKEND=s3`, `PUBLIC_BASE_URL=https://…`
and the compose-internal addresses. Then `make preflight`. The database and
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
sudo infra/deploy/bootstrap.sh
```

That is Docker, the firewall, unattended security updates, the preflight, the
build, the start, the migration, the seed and a wait for both hostnames to
answer over TLS. By hand, the same thing is:

```bash
docker compose --env-file .env -f infra/docker/docker-compose.prod.yml build
docker compose --env-file .env -f infra/docker/docker-compose.prod.yml up -d
docker compose --env-file .env -f infra/docker/docker-compose.prod.yml run --rm migrate uaagro-db seed
```

Certificates need the two DNS names to already point at the host and ports 80
and 443 open; until they do, Caddy retries and `docker compose … logs caddy`
says why. `ACME_EMAIL` receives expiry notices.

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
| Deploy a new version | `infra/deploy/deploy.sh` (pull, preflight, build, roll, migrate, health) — the voice worker drains live calls for up to ten minutes before stopping |
| Roll back | `infra/deploy/deploy.sh <previous tag>`; migrations are backward-compatible one step |
| Back up | `infra/deploy/backup.sh` nightly from cron: a compressed `pg_dump` into `backups/` in the recordings bucket, keeping the last 30. Recordings are already in the bucket. |
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
