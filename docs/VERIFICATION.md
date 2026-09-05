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
6. Say "बस, धन्यवाद" (or answer "नहीं" when it asks "और कुछ पूछना है?").
   Expect the closing line -- "धन्यवाद! और कुछ पूछना हो तो कभी भी फ़ोन
   कीजिए। नमस्ते।" -- played to the end, and then the call ending from our
   side without you hanging up. On Exotel that is the Voicebot applet
   finishing and the Hangup applet after it running; `call.hangup_by_agent`
   appears in the worker log. Then, on another call, say nothing after an
   answer: expect "जी, मैं सुन रहा हूँ" after 6 s, "और कुछ पूछना हो तो बताइए"
   after 15 s, and the closing line and a hang-up at 25 s, recorded as
   `abandoned_silence`.
7. Hang up yourself on a third call. Within a minute the call page shows
   the transcript, every turn with its latency, the time to first reply,
   and the recording.

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

## 7. The demo, end to end (added 3 September 2026)

Everything here runs locally with the browser test page; nothing dials a
phone. Start the four processes (API 8000, panel 3000, voice worker 8080,
background jobs `uv run arq worker.tasks.WorkerSettings`) with
`.localdev/env` sourced.

1. **Price and stock, without the model.** Open `/dev/call`, start a call,
   ask "डीएपी का रेट क्या है?". Expect the pack, the rupee figure and the
   centre inside a second of going quiet, and "यह लखनऊ सेंटर का रेट है" once
   (the caller has no centre of their own). Then "आलू का बीज है क्या?" and,
   after the answer, just "स्टॉक है क्या?" — the second question is about the
   potato seed. Then "आपका सेंटर कहाँ है?" — the address, the hours and the
   phone number in words.
2. **The call is on record.** Hang up. On the panel's Overview the call is
   counted under *test calls*, not in the day's numbers. Open it from Calls:
   every turn, the reply times, the tool each turn used, and a recording that
   plays (both legs; the caller left, the agent right). A summary appears
   within a minute if the background jobs are running.
3. **The knowledge base speaks only what it knows.** On Knowledge, add a note
   (for instance a paragraph on drip irrigation subsidy with a date). Wait
   for *indexed* (the status strip shows the worker's heartbeat). On a new
   call ask about it — expect the note's facts, in plain words. Ask about
   something the note does not cover — expect "इसकी पक्की जानकारी अभी मेरे पास
   नहीं है" and the offer of a person, never an invented figure.
4. **Call now.** On Outbound, paste your own number, tick the consent
   attestation, press *Call now*. The campaign starts; the contact card turns
   to *ringing* with *Answer in browser*. Open it: the outbound script plays
   (disclosure first), press 2 and ask a product question, press 1, hang up.
   The card turns green with the outcome; the call is under Calls with its
   transcript and recording.
5. **The client's database.** On Data, add the MySQL source (host, port,
   database, user, password), *Test connection*, map the stores, products and
   stock tables to our fields, *Sync now*. The run lists what it wrote; the
   centres, catalogue and stock pages show it; the agent quotes it on the next
   call.
6. **Changing the words.** On Flows → the inbound helpline, edit the prompt or
   the greeting, save as a new version, publish. The next call speaks it.

## 8. Understanding, on the browser page (added 4 September 2026)

Open the demo backend's call page, `http://127.0.0.1:8090/call` (`make demo`; the worker's old `/dev/call` address redirects there), and say these, in this order. Each
line names what the agent must do; anything else is a regression.

| Say | The agent must |
|---|---|
| "मुझे धान का बीज चाहिए" or "I want rice seeds" | name the two paddy seeds and ask which |
| "पहला वाला" / "the first one" | give that seed's stock and price |
| "मसूरी का चाहिए" | give मसूर के 75's stock and price (one matra off the listed name) |
| "पशु की आहार चाहिए" | list the cattle feeds -- not ask what you want |
| "गाय का फीड चाहिए" or "cow feed", then "गाय के लिए क्या-क्या दे सकते हैं?" twice | list the four cattle feeds each time -- never "पहला या दूसरा बोलिए", never a hand-over; "दूसरा" after that still picks the second |
| "बीज चाहिए", then "और क्या-क्या है?" | four seeds with "और भी हैं", then the next ones -- not the same four; "और क्या है" once everything is read: "बस यही हैं" |
| "खाद में क्या-क्या है?" after a seed list | the fertilisers, not the seeds again |
| "कौ का फीड चाहिए" | "गाय के लिए हमारे पास ..." -- the word गाय in the reply, not "पशु आहार" |
| then "वो गाय का ही है न?" / "is it for cows?" / "cow and buffalo?" | "हाँ, ये सब गाय के लिए ही हैं ..." -- a yes about the animal, never the list again; asked a second way, a shorter yes |
| "दूसरा", then "ये भैंस को भी दे सकते हैं?" | "हाँ, पशु आहार दाना भैंस के लिए ही है। रेट और स्टॉक बताऊँ?", and "हाँ" reads the price |
| "बकरी का दाना है क्या?" / "hen ka feed" | "गाय-भैंस के लिए है, बकरी के लिए नहीं" and an offer of the manager -- not a price |
| "यूरिया गाय को खिला सकते हैं?" | "यूरिया खाद है, पशुओं को खिलाने की चीज़ नहीं। गाय के लिए पशु आहार बताऊँ?" -- and "हाँ" lists the feed |
| "that cattle feed can be used for cow?" (English) | a yes about cows -- never Calcium Ammonium Nitrate |
| the same question twice | the second answer starts "जी, दोबारा बता देता हूँ" -- never the identical sentence as if new |
| "गेहूँ के लिए क्या-क्या है?" | the wheat seeds and the wheat sprays, named by kind, then "बीज या दवा — क्या देखूँ?" |
| "गेहूँ के लिए खाद है क्या?" | the fertilisers -- never "गेहूँ का खाद नहीं है" (the catalogue does not tag fertiliser by crop) |
| "कार्बेन्डाज़िम गेहूँ में डाल सकते हैं?", then "और धान में?" | "हाँ, ... गेहूँ के लिए है (गेहूँ और चना)", then "धान के लिए नहीं" with the paddy sprays offered |
| "यूरिया गेहूँ में डाल सकते हैं?" | the model answers from the knowledge base -- not a stock figure |
| "बाजरा के लिए क्या है?" | "बाजरा के लिए अभी कुछ नहीं है ..." -- the crop is recognised even with nothing in the catalogue |

## 9. Security, on the deployed host (added 5 September 2026)

`docs/SECURITY.md` says what is protected. These are the checks that prove it
from outside, once the stack is up:

| Do | Expect |
|---|---|
| `curl -I http://panel.<domain>/` | a 308 to `https://` |
| `curl -sI https://panel.<domain>/en \| grep -i strict-transport` | `max-age=31536000; includeSubDomains` |
| `curl -s https://voice.<domain>/internal/speech` | 404 (the edge does not route it) |
| `curl -s -o /dev/null -w '%{http_code}' https://voice.<domain>/ws/voice` | 403 or 1008 close: no token, wrong address |
| `curl -X POST -H 'Content-Length: 40000000' https://panel.<domain>/api/…` | 413 at the edge |
| In the panel, add a website `http://169.254.169.254/` to the knowledge base | refused, naming the rule, before any fetch |
| In the panel, test a data source at host `postgres` or `127.0.0.1` | refused |
| `docker compose … exec api touch /x` | `Read-only file system` |
| `docker compose … exec api cat /proc/1/status \| grep Cap` | `CapEff: 0000000000000000` |
| `nmap -p- <host>` from outside | 22, 80, 443 only |
| `make preflight` with one `FILL_ME` left | one error naming it, exit 1 |
| "बीज चाहिए", then "धान" | list four seeds, then the two paddy seeds -- no hand-over |
| "सबका रेट बता दो" after a list | read every price, then still accept "दूसरा" |
| "उसका प्राइस" after a list | read every price |
| anything unintelligible, three times | ask once, ask differently and offer a person, then hand over -- never the same line three times |
| "आप मशीन हो?" | admit it and offer a person -- not a tools listing |
| a whole turn in English | answer in English; the English voice if `BAKBAK_VOICE_EN` is set, else the Hindi one |
| a whole turn in Marathi with `BAKBAK_VOICES=mr=<voice>` set | the same facts in Marathi, or Hindi if the rendering failed validation (`agent.render_rejected` in the log) |

The log lines to watch in `.localdev/worker.log`: `agent.direct` now carries
`resolved` and `misses`; `agent.handover_after_misses` marks the third miss;
`pipeline.language_followed` and `pipeline.language_unvoiced` show what the
recogniser heard and whether a voice existed for it.

