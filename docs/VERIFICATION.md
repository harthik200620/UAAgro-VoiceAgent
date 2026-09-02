# Verifying the build with real keys and a real line

Nothing in this document was run by the build. Every step here spends vendor
credit or needs a phone line, so it is done by the person who holds the keys,
in this order, each step proving one thing. The whole list costs a few minutes
of speech and under ten calls.

Where a step says "expect", that is what the code was written to do. If the
real thing differs, the difference is a finding, not something to work around:
note it and stop at that step.

## 0. Before spending anything

Run on the machine that will hold the keys, with `.env` filled in:

```bash
make check
```

That is lint, `mypy --strict`, the secret scan and the test suite. It uses no
vendor key. Then start the stack (`docs/DEPLOY.md` §3) and open the panel: the
Data page must show Postgres, Redis and object storage as reachable before any
call is placed.

## 1. Keys, one request each

| Vendor | Variable | Cheapest proof | Expect |
|---|---|---|---|
| Soniox (speech to text) | `SONIOX_API_KEY` | `uv run uaagro-sim --wav <ten seconds of your own Hindi, 8 kHz mono> --url ws://localhost:8080/ws/voice --out out.wav` against a running voice worker | the worker log shows your words transcribed within a second of you finishing them; `out.wav` holds the reply |
| Bakbak (Hindi voice) | `BAKBAK_API_KEY`, `BAKBAK_VOICE_HI` | Flows → Inbound → **Listen** on the greeting | the greeting plays in the panel in under two seconds; every number is spoken as a Hindi word |
| Anthropic (the agent) | `ANTHROPIC_API_KEY` | Inbound → Knowledge base → **Try a question** ("गेहूं में पीला रतुआ का इलाज?") | an answer that cites the uploaded document; with nothing uploaded, an honest "no source" |
| Exotel | `EXOTEL_SID`, `EXOTEL_API_KEY`, `EXOTEL_API_TOKEN`, `EXOTEL_SUBDOMAIN` | step 3 below | |
| WhatsApp (Meta) | `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID` | step 4, the press-1 message | |

The voice worker and the API must share one `INTERNAL_API_TOKEN`; if they do
not, "Try a question" and "Listen" answer 502 with a remedy naming the
variable. That message is deliberate.

## 2. The voice, before any call

`BAKBAK_VOICE_HI` is a choice, not a default. Pick it by listening: Flows →
Inbound → **Listen** with two or three candidate ids, on a phone speaker, not
headphones. The greeting, a price ("दो सौ पचास रुपये"), a date and a centre
name ("मनकापुर") are the four things to judge. Write the winner into `.env` and
restart the voice worker.

## 3. The first inbound call

Point Exotel at the socket as in `docs/DEPLOY.md` §4, then call the DID from
a mobile.

Expect, in order:

1. The greeting starts within a second of the call connecting.
2. Live page: the call appears as **In call** with the last four digits of the
   number, never the full number, within a second of ringing.
3. Ask "लखनऊ में आपका सेंटर कहाँ है?" Expect the nearest centre with its
   address and hours, read from the Centres page, not invented.
4. Interrupt the agent mid-sentence. Expect it to stop within about a third
   of a second and listen. Say only "हाँ जी" over it: expect it to stop and
   then carry on where it was, or stay quiet if it had finished -- never to
   answer the nod. Let a fan or the road run: expect it not to stop. This is
   the barge-in check; the voice gate's thresholds (`runtime/vad.py`) are the
   thing to tune on a real line, and `bargein.source` in the worker log says
   which signal cut the agent.
5. Ask for the manager. Expect a hold line naming the centre and a transfer to
   the number on the Centres page; if that number does not answer, the
   fallback number from Centres → Transfer rules. If instead you hear the
   callback commitment ("चौबीस घंटे के अंदर फ़ोन आएगा"), the provider refused the
   transfer request: the voice worker log line `call.transfer_failed` names
   the error, and the request shape in `adapters/telephony/control.py` is the
   thing to check against Exotel's current API.
6. Hang up. Within a minute the call page shows the transcript, every turn
   with its latency, the time to first reply, and the recording.

Write down the **time to first reply** and the **p95 turn latency** the call
page shows. The target from the brief is 500 ms to 1 s including the
telephony leg. The panel measures from the moment the farmer stops speaking to
the first audio byte handed to Exotel; the last hop, Exotel to the handset, is
not visible to the software and adds what the network adds. If the p95 is
above one second, the per-turn breakdown on the call page says which stage
(hearing, thinking, speaking) took it.

## 4. The first outbound call

Outbound → **New campaign**: paste your own number, attest consent, pick the
published outbound flow, save, approve as a second user (or as super admin),
start.

Expect:

1. Your phone rings from the promotional CLI within the calling window.
2. The agent asks for you by name, asks whether it may take a minute, gives
   the message, and offers: press 1 for the offer, press 2 for other details.
3. Press 2 and ask a product question. Expect an answer from the knowledge
   base and a return to the offer.
4. Press 1. Expect the acceptance line and, if WhatsApp is configured, the
   message on your phone within a minute.
5. The number card on the Outbound page turns amber while the call runs and
   green when done; clicking it opens the transcript, recording and summary.
6. Say "मुझे दोबारा मत कॉल करना" on a second attempt. Expect the agent to
   confirm and end, and the number to appear in Do-not-call before the call
   is over.

One assumption needs this call to confirm it: which side of Exotel's `start`
frame carries our number on an outbound call. The worker checks both sides
and also reads the `contact:<id>` custom field the dialer sets, so the call is
classified correctly either way; but if the greeting you hear is the helpline
greeting rather than the outbound one, that classification is wrong and the
start frame should be saved from the voice worker log (it is logged with the
number masked) and sent back.

## 5. Load, without spending on speech

`make load` places 30 simulated calls through the telephony simulator against
a voice worker configured with the fixture adapters. It proves the socket,
the session bookkeeping and the panel's live feed hold at 30, and costs
nothing. It does not prove vendor rate limits at 30 concurrent streams; that
is a question for each vendor's plan, and the answer goes into
`docs/COST_MODEL.md`.

## 6. What the software cannot decide

- The DLT template id: promotional calls are refused until `DLT_TEMPLATE_ID`
  is set, and the panel names that reason on the campaign page.
- The Bakbak voice (step 2).
- Exotel's media IP ranges for `TELEPHONY_IP_ALLOWLIST`, from their console.
- Whether the calling window (`CALLING_WINDOW_START`/`END`) matches the
  customer's licence conditions.
