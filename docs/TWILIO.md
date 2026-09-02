# Running the agent on Twilio

Twilio is wired as a third telephony provider beside Exotel and Plivo: the
same media socket, the same agent, the same panel. What differs is that
Twilio is *told what to do* rather than pointed at a configuration — it is
given TwiML, and `<Connect><Stream>` is the verb that opens a two-way media
stream.

That document is **served by the worker** at `/telephony/twiml`, not sent
inline on the dial request. Inline is tidier and was tried first, but a trial
account refuses it: any request carrying `Twiml` comes back
*"Invalid or disallowed parameters provided - trial accounts have limited
parameter access"*, and the message names no parameter. `Timeout` and
`TimeLimit` are refused the same way. Three parameters — `To`, `From`, `Url`
— work on every account, so those are the three the dialer sends.

The route is gated on `TELEPHONY_WS_TOKEN`, because whoever can fetch that
document is handed the media socket's token.

## What is already set

In `.env`, which is not committed and which the secret scan refuses to let
anyone commit: `TELEPHONY_PROVIDER=twilio`, the API key and its secret, the
account SID, `TELEPHONY_WS_TOKEN` (generated), and `PUBLIC_BASE_URL` pointing
at a tunnel.

**The account SID is the one the key reports, not the one first supplied.**
An API key can only act on the account it was created under, and
`GET /2010-04-01/Accounts.json` — which any key may call — names it. The SID
first given returned 404 for that reason. If a call ever fails with 20404,
this is the first thing to re-check.

The key has been confirmed able to place calls: `POST /Calls.json` with no
parameters answers `400 Required parameter is missing`, which is the answer a
credential that *may* create calls gives. A credential that may not answers
401. Nothing is dialled by that probe, which is why it is the one to use.

## What is still missing

One thing: a phone number.

The account has none, and a trial account cannot search for one over the
API — `AvailablePhoneNumbers` answers *"This feature is not available on a
Trial account"*. It has to be claimed in the Console: **Phone Numbers → Manage
→ Buy a number**, with Voice capability. A trial balance covers one. Then set
`TWILIO_FROM_NUMBER` to it in E.164 and restart the voice worker.

## The tunnel

Twilio opens the media stream itself, from its own network, so
`http://localhost:8080` cannot work: there is nothing at that address as far
as Twilio is concerned, and the call connects to silence and hangs up.
`PUBLIC_BASE_URL` must be a public HTTPS origin, which the dialer turns into
`wss://` on the stream.

Either tunnel does the job. `cloudflared tunnel --url http://localhost:8080`
prints a `trycloudflare.com` address; `ngrok http 8080` prints an
`ngrok-free.dev` one. Whichever is used, the address **changes every time the
tunnel restarts**, and `PUBLIC_BASE_URL` has to be updated and the worker
restarted with it — a call placed against a stale address connects to nothing.

`TELEPHONY_WS_TOKEN` is the shared secret on that socket. It is generated
rather than obtained, it goes into the stream URL automatically, and without
it the worker accepts any connection that finds the address.

## Testing on your own number

**Use Flows → Outbound script → "Test on my phone".** That places one real
call to a number you type, using the published outbound script, and it is the
path built for exactly this.

If you want the agent to greet you by name, create the campaign first with
your name and number in it:

1. Outbound → New campaign → paste `Your Name, 9xxxxxxxxx`, attest consent,
   save. The campaign will be **blocked** — see the next section — but saving
   it records you as a farmer with that name, which is where the agent reads
   the name from.
2. Flows → the outbound script → **Test on my phone** → your number.

The call should: state that it is an automated call from UA Agro, ask whether
it is speaking to you *by name*, ask for two minutes, give the offer, and
offer press 1 for the offer, press 2 for questions, press 9 to be left alone.
Everything appears on the Live page as it happens.

## Why a campaign will not run on a Twilio number

The compliance gate blocks every promotional campaign that does not have:

- **A 140-series caller ID.** Indian regulation (TCCCPR) requires promotional
  voice traffic to come from the 140 series. A Twilio number is not one, and
  the gate refuses the campaign rather than placing calls that are in
  violation on every one of them.
- **A registered DLT entity and template** (`DLT_ENTITY_ID`,
  `DLT_TEMPLATE_ID`).

This is not a bug to work around, and the gate has no override. A campaign to
real farmers needs a 140-series CLI from an Indian provider — which is what
the Exotel path is for — plus the DLT registrations under UA Agro's entity.
Twilio is well suited to **development, the inbound helpline and single test
calls**; the promotional campaign itself is Exotel's job unless Twilio can
supply a 140-series CLI on this account.

Two more things to know before pointing Twilio at Indian numbers:

- A **trial account** can only call numbers you have verified in the console.
- Calls terminating in India have carrier restrictions on foreign caller IDs.
  Twilio's own guidance for India is worth reading before buying a number for
  this.

## What the worker does with a Twilio call

- **Audio** is µ-law at 8 kHz, not the linear16 Exotel sends. Both are
  implemented as separate serializers on purpose (§4.3): one class with a
  codec flag is how a provider ends up sent the wrong bytes.
- **Identity**: Twilio does not put the caller's number in the stream. The
  dialer passes the campaign contact as a `<Parameter>`, which comes back in
  the start frame's `customParameters` and is where the call is matched to its
  contact card. An inbound call arrives without one and is treated as a
  helpline call.
- **Hang-up and transfer** go back over REST: the agent ending a call
  completes it, and a hand-over redirects the live call to the centre
  manager's number.

One gap, recorded rather than hidden: the **whisper** — the line played to the
manager before the farmer is connected — is not sent on Twilio. It needs a
TwiML document served at the moment the manager answers, and nothing serves
one yet. The transfer itself works; the manager simply gets no preamble.
