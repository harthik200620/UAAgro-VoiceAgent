# Running the agent on Twilio

Twilio is wired as a third telephony provider beside Exotel and Plivo: the
same media socket, the same agent, the same panel. What differs is that
Twilio is *told what to do* rather than pointed at a configuration — the
dialer sends TwiML with `<Connect><Stream>` inline, so there is no webhook to
host for outbound calls and nothing to keep in sync with the dialer.

## What is already set

In `.env`, which is not committed and which the secret scan refuses to let
anyone commit:

```
TELEPHONY_PROVIDER=twilio
TWILIO_API_KEY_SID=SK…
TWILIO_API_KEY_SECRET=…
```

## What is still missing

Three things, and no call can be placed without them.

| Variable | What it is | Where to find it |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | Starts `AC…`. Not a secret, but every REST path contains it, and an API key cannot say which account it acts on. | Twilio Console home |
| `TWILIO_FROM_NUMBER` | The Twilio number the call comes from, in E.164 (`+1…`, `+91…`) | Console → Phone Numbers |
| `PUBLIC_BASE_URL` | The address **Twilio** can reach this worker on, from the public internet | see below |

`PUBLIC_BASE_URL` is the one people get wrong. Twilio opens the media stream
itself, from its own network, so `http://localhost:8080` cannot work: there is
nothing at that address as far as Twilio is concerned, and the call connects
to silence and hangs up. It needs a public HTTPS origin, which becomes `wss://`
on the stream. For a test from this machine, a tunnel is enough:

```bash
cloudflared tunnel --url http://localhost:8080
```

Take the `https://…trycloudflare.com` address it prints and set
`PUBLIC_BASE_URL` to it, then restart the voice worker. `ngrok http 8080` does
the same job.

Also set `TELEPHONY_WS_TOKEN` to any random string. It is the shared secret on
the socket, it goes into the stream URL automatically, and without it the
worker accepts any connection that finds the address.

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
