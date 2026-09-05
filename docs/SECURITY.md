# Security: what is protected, how, and what was found (5 September 2026)

This platform holds the phone numbers, locations and buying history of
farmers (§17), places and records phone calls, and lets staff change what
an automated agent says. The controls below are the ones in place; the
audit findings at the end are what a review of the whole application found
and fixed on 5 September 2026, with what remains outside the software.

## The model

| Who | Reaches | Through |
|---|---|---|
| The public | nothing | Only Caddy listens (80/443). Everything else is on the compose network. |
| The telephony provider | `wss://voice.<domain>/ws/voice`, `/telephony/*` | A shared token on the handshake **and** a source address in `TELEPHONY_IP_ALLOWLIST`. |
| Staff | `https://panel.<domain>` | Password (Argon2id) + mandatory TOTP; a 15-minute access token; a rotating refresh token in an `httpOnly; Secure; SameSite=Lax` cookie with reuse detection revoking the family. Six roles; row-level security forced in Postgres, the API connecting as a non-owner role. |
| The admin panel | the API | Server-side only, on the compose network, with the staff member's token. No browser ever holds an API credential (`server-only` env, no `NEXT_PUBLIC_`). |
| The API | the voice worker's `/internal` | A shared internal token, compared in constant time. |
| Anyone | `/metrics`, `/docs` | Closed outside development; metrics need the internal token. |

## Controls, by layer

**Transport.** TLS from Let's Encrypt, renewed by Caddy; HTTP redirected;
HSTS for a year; HTTP/3. Body ceilings at the edge (32 MB on the panel host,
1 MB on the voice host; the media travels on the WebSocket). Hardening
headers on both hosts; the `Server` header removed.

**Requests.** The API refuses a body above 32 MB before any route reads it
(413) and a mutating request with no declared length (411). Every address
is held to 600 requests a minute across the API (`API_RATE_LIMIT_PER_MINUTE`); sign-in is held to 8 per
address and 5 per account per window, MFA and refresh to their own limits.
Uploads are capped per route (knowledge 25 MB, contact lists 10 MB) and
read only up to the cap. The voice worker caps a WebSocket message at
256 KB and refuses new calls above `MAX_CONCURRENT_CALLS`.

**Outbound connections the operator can steer.** The knowledge crawler and
the client-database connector take an address from the panel and connect
to it from inside the deployment. Both resolve the name first and refuse
anything that is not public: loopback, link-local (the cloud metadata
range), multicast, reserved and unspecified addresses, the deployment's own
service names, and -- for the crawler -- private networks. The crawler
follows redirects by hand and checks every hop; page bodies are streamed
and cut at 2 MB. The connector may reach a private network only when
`SOURCES_ALLOW_PRIVATE_NETWORKS=true` says the client's database lives on
one; its check applies to deployments (staging, production), because a
developer's own machine is the one place a database on 127.0.0.1 is the
client's. A refusal never echoes the address it resolved to.

**Data.** Phone numbers are HMAC-indexed under a permanent pepper and
encrypted with AES-256-GCM under a data key held in KMS, or -- on a host
with no KMS, by explicit setting -- in the environment, said loudly at
boot. TOTP seeds are encrypted the same way. Recordings go to the bucket
with server-side encryption and a delete-after date. Logs carry no phone
number, recording address or vendor key: masking happens in the logger.
The audit log is hash-chained. SQL identifiers are allowlisted or resolved
from the server's own catalogue before they are interpolated; every value
is a bound parameter.

**The agent.** It never invents a product, price, stock figure, dose or
scheme: figures come from tool results and the validator rejects an answer
that contradicts them; unapproved crop recommendations are unservable;
a licensed product is handed to a person. Knowledge-base text and the
client's database rows reach the model as data, never as instructions.

**Processes.** Every container runs as a non-root user with no
capabilities and no way to gain privileges; the Python services and the
panel run on a read-only root filesystem with `tmpfs` for `/tmp` (and the
panel's cache), the model volume the only other writable path. Postgres
uses scram-sha-256; Redis requires a password; MinIO's console is not
started. Log files rotate. The voice worker drains live calls for up to
ten minutes on stop.

**Boot-time refusal.** A production process will not start without the
pepper, the signing key, the WebSocket token, the internal token, a data
key, `SESSION_COOKIE_SECURE=true`, `STORAGE_BACKEND=s3` and the four-eyes
rule on. `make preflight` catches the same, plus placeholders and plaintext
URLs, before Docker is involved. The pre-commit secret scan refuses a key,
token or password in the tree.

**Dependencies.** `pip-audit` over the frozen production closure: no known
vulnerabilities. `npm audit --omit=dev` for the panel: none after the
5 September upgrade (below).

## What the review of 5 September 2026 found

| Finding | Severity | Fix |
|---|---|---|
| The knowledge crawler followed any URL and any redirect, including addresses inside the deployment and the cloud metadata endpoint (SSRF). | High | `uaagro_domain.netsafety`; public addresses only, redirects checked per hop, bodies streamed to a cap. |
| The client-database connector accepted any host, including the deployment's own services. | Medium | The same check; private networks only by explicit setting. |
| The contact-list upload read the whole file into memory with no ceiling. | Medium | 10 MB cap, read up to the cap. |
| No request-size or per-address rate ceiling on the API as a whole; nothing at the edge either. | Medium | 413/411 middleware, 600/min per address, Caddy `request_body` limits. |
| Containers ran with default capabilities, writable roots and the ability to gain privileges. | Medium | `cap_drop ALL`, `no-new-privileges`, read-only roots with `tmpfs`, scram for Postgres, a WebSocket message ceiling. |
| Three vulnerable panel dependencies: `next-intl` ≤4.9.1 (open redirect, prototype pollution), `postcss` ≤8.5.22 via `next` (arbitrary `.map` file read). | High / Moderate | `next-intl` 4.9.2, `next` 15.5.25, `postcss` pinned to 8.5.28 by override. |

Verified and not changed: no string-built SQL reaches user input; every
route under `/admin` and `/auth` requires a principal and a role; TOTP is
mandatory, seeds encrypted; refresh reuse revokes the family; the worker's
socket needs token and allowlist; tokens compare in constant time; the
panel holds nothing secret in the browser; PII is masked in the logger.

## Outside the software

- **DDoS, WAF, DNS**: the cloud provider's layer. Put the panel hostname
  behind their proxy if you want them; the voice hostname must stay direct
  (the provider's media ranges are allowlisted by address).
- **Key management**: use a KMS where the cloud has one; the local data key
  is for hosts that do not.
- **Backups**: `infra/deploy/backup.sh` nightly; a restore is rehearsed, not
  assumed (`docs/RUNBOOK.md`).
- **People**: one account per person, roles by job, the admin account used
  only to create the others. Revoke on departure from the panel.
- **A content security policy on the panel** is not set: Next.js's inline
  runtime needs nonces the panel does not yet plumb through. The panel
  serves only same-origin content and sets `X-Frame-Options DENY`; a CSP is
  the next step, not a gap in what is protected today.
