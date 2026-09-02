"""Talk to the agent from a browser, with no telephony account at all.

**TEMPORARY.** This exists because both telephony trials refuse to place a
real call until their compliance checks clear -- Exotel wants KYC, Twilio
wants an upgrade -- and waiting on either is a bad reason to be unable to hear
whether the agent works. Delete this module, the `devtools` package and the
guarded block in `main.py` once a real call can be placed.

It is not a simulator. The page opens the same `/ws/voice` socket a provider
opens, speaks the same Exotel AgentStream frames, and is answered by the same
session, pipeline, recogniser, model and voice. The only thing standing in for
the phone network is the browser: a microphone where the handset would be, and
`AudioContext` where the carrier would be.

**The audio context runs at 8 kHz**, the telephony rate. The first version ran
at the browser's 48 kHz and converted both ways in script: a six-sample
average on the way in, which aliases and made the recogniser's job harder, and
an 8 kHz buffer handed to a 48 kHz graph on the way out, which Chrome
upsamples by linear interpolation -- audible as a steady hiss under every
word. At 8 kHz the browser does both conversions itself with a proper filter,
the microphone arrives already at the rate the recogniser wants, and the
hiss is gone. Capture is an AudioWorklet (128-sample quanta, 16 ms) with a
ScriptProcessor fallback.

Two things it therefore *does* prove: that the whole media path works end to
end, and roughly how fast it feels. Two it does not: anything about the
carrier leg -- codec transcoding on a real GSM line, jitter, or the 200-400 ms
a mobile network adds -- and anything about the provider's own applet
configuration. Those need a real call, and `docs/VERIFICATION.md` still lists
them.

Served only when ``APP_ENV`` is development, so it cannot be reachable from a
deployed worker even if someone forgets to delete it.
"""

from __future__ import annotations

from urllib.parse import quote

import structlog
from fastapi import FastAPI, Response

from uaagro_domain.settings import get_settings

log = structlog.get_logger(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>UA Agro - talk to the agent</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; font: 15px/1.55 "IBM Plex Sans", system-ui, sans-serif;
         background: #F7F5EF; color: #17160F; }
  main { max-width: 720px; margin: 0 auto; padding: 40px 24px 64px; }
  h1 { font: 400 34px/1.2 "Instrument Serif", Georgia, serif; margin: 0 0 6px; }
  .sub { color: #6B6759; margin: 0 0 28px; }
  .card { background: #fff; border: 1px solid #E6E1D6; border-radius: 12px;
          padding: 20px 22px; margin-bottom: 16px; }
  label { display: block; font-size: 13px; color: #6B6759; margin-bottom: 6px; }
  input { width: 100%; box-sizing: border-box; padding: 9px 12px; font: inherit;
          border: 1px solid #E6E1D6; border-radius: 8px; background: #fff; }
  button { font: inherit; font-weight: 600; padding: 11px 22px; border-radius: 8px;
           border: 1px solid transparent; cursor: pointer; }
  #call { background: #1E5B3A; color: #fff; }
  #hang { background: #fff; color: #8E2B17; border-color: #E6E1D6; }
  button[disabled] { opacity: .45; cursor: default; }
  .row { display: flex; gap: 10px; align-items: center; margin-top: 18px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #D9D4C8;
         display: inline-block; margin-right: 8px; }
  .live .dot { background: #3C9D5D; }
  .status { font-size: 14px; color: #6B6759; }
  .meter { height: 6px; background: #F1EEE6; border-radius: 3px; overflow: hidden;
           margin-top: 16px; }
  .meter i { display: block; height: 100%; width: 0; background: #1E5B3A;
             transition: width .06s linear; }
  #log { font: 12px/1.6 "IBM Plex Mono", ui-monospace, monospace; color: #6B6759;
         white-space: pre-wrap; max-height: 300px; overflow-y: auto; margin: 0; }
  .note { font-size: 13px; color: #6B6759; border-left: 3px solid #E6E1D6;
          padding-left: 12px; margin-top: 22px; }
</style>
</head>
<body>
<main>
  <h1>Talk to the agent</h1>
  <p class="sub">Your microphone stands in for the phone. Same socket, same
     agent, same voice &mdash; no telephony account involved.</p>

  <div class="card">
    <label for="from">Calling as (used to greet you by name, if we know it)</label>
    <input id="from" value="+919993338278" spellcheck="false">
    <div class="row">
      <button id="call">Start the call</button>
      <button id="hang" disabled>Hang up</button>
      <span class="status" id="state"><span class="dot"></span>not connected</span>
    </div>
    <div class="meter"><i id="level"></i></div>
  </div>

  <div class="card"><p id="log">Ready.</p></div>

  <p class="note">Speak normally and pause &mdash; the agent answers when it
    hears you stop. Talk over it to test barge-in: it should stop within a
    fraction of a second, and carry on where it left off if all you said was
    "haan". The log shows how long each reply took after you went quiet.</p>
</main>
<script>
const RATE = 8000, FRAME = 160;           // 20 ms of 8 kHz mono
const JITTER_S = 0.06;                    // playback runs this far behind arrival
const $ = (id) => document.getElementById(id);
const logEl = $("log");
let ws, audioCtx, micStream, captureNode, started = false;
let playAt = 0, sources = [], agentTalking = false, lastAgentAudioAt = 0;
let lastLoudAt = 0, quietAnnounced = false, framesIn = 0;

function say(line) {
  const t = new Date().toLocaleTimeString();
  logEl.textContent += `\\n${t}  ${line}`;
  logEl.scrollTop = logEl.scrollHeight;
}
function state(text, live) {
  $("state").innerHTML = '<span class="dot"></span>' + text;
  $("state").parentElement.classList.toggle("live", !!live);
}

// --- the wire ---------------------------------------------------------- //
// Exotel AgentStream frames, because that is what the worker's serializer
// reads. base64 of raw little-endian 16-bit PCM at 8 kHz.

function b64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}
function unb64(text) {
  const raw = atob(text), out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function start() {
  $("call").disabled = true;
  const sid = "browser-" + Math.random().toString(36).slice(2, 10);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/ws/voice__SOCKET_QUERY__`;

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  } catch (err) {
    say("microphone refused: " + err.message);
    $("call").disabled = false;
    return;
  }

  // At the telephony rate. The browser resamples the microphone down and
  // the output up with real filters; doing either in script was the hiss.
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: RATE, latencyHint: "interactive",
    });
  } catch (err) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  await audioCtx.resume();
  if (audioCtx.sampleRate !== RATE) {
    say("browser would not open an 8 kHz context (" + audioCtx.sampleRate +
        " Hz); resampling in script instead");
  }

  ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";

  ws.onopen = async () => {
    state("connected", true);
    say("socket open, sending start frame");
    ws.send(JSON.stringify({ event: "connected" }));
    ws.send(JSON.stringify({
      event: "start",
      stream_sid: sid,
      start: {
        stream_sid: sid,
        call_sid: sid,
        from: $("from").value.trim(),
        to: "browser-test",
        account_sid: "browser",
      },
    }));
    started = true;
    $("hang").disabled = false;
    try {
      await capture();
    } catch (err) {
      say("capture failed: " + err.message);
      stop(true);
    }
  };

  ws.onmessage = (event) => {
    let frame;
    try { frame = JSON.parse(event.data); } catch { return; }
    if (frame.event === "media" && frame.media && frame.media.payload) {
      play(unb64(frame.media.payload));
    } else if (frame.event === "clear") {
      flush();
      say("agent stopped talking (barge-in)");
    }
  };

  ws.onclose = () => { say("socket closed"); stop(false); };
  ws.onerror = () => say("socket error");
}

// --- microphone -------------------------------------------------------- //

const WORKLET = `
class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(${FRAME}); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === ${FRAME}) {
        const out = new Int16Array(${FRAME});
        let peak = 0;
        for (let j = 0; j < ${FRAME}; j++) {
          const s = Math.max(-1, Math.min(1, this.buf[j]));
          if (Math.abs(s) > peak) peak = Math.abs(s);
          out[j] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage({ pcm: out.buffer, peak }, [out.buffer]);
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("capture", Capture);
`;

async function capture() {
  const source = audioCtx.createMediaStreamSource(micStream);
  const ratio = audioCtx.sampleRate / RATE;

  if (audioCtx.audioWorklet && ratio === 1) {
    const url = URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" }));
    await audioCtx.audioWorklet.addModule(url);
    captureNode = new AudioWorkletNode(audioCtx, "capture", {
      numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1,
    });
    captureNode.port.onmessage = (e) => sendFrame(new Uint8Array(e.data.pcm), e.data.peak);
    source.connect(captureNode);
    captureNode.connect(audioCtx.destination);   // silent; keeps the node rendering
    say("microphone live at " + audioCtx.sampleRate + " Hz (worklet), 20 ms frames");
    return;
  }

  // Fallback: a ScriptProcessor, resampling by averaging when the context
  // could not be opened at 8 kHz.
  let pending = [];
  captureNode = audioCtx.createScriptProcessor(ratio === 1 ? 256 : 2048, 1, 1);
  captureNode.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    let peak = 0;
    for (let i = 0; i < input.length; i++) peak = Math.max(peak, Math.abs(input[i]));
    for (let i = 0; i + ratio <= input.length; i += ratio) {
      let sum = 0, n = 0;
      for (let j = Math.floor(i); j < Math.floor(i + ratio); j++) { sum += input[j]; n++; }
      pending.push(n ? sum / n : 0);
    }
    while (pending.length >= FRAME) {
      const chunk = pending.splice(0, FRAME);
      const pcm = new Uint8Array(FRAME * 2);
      const view = new DataView(pcm.buffer);
      for (let i = 0; i < FRAME; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      }
      sendFrame(pcm, peak);
    }
  };
  source.connect(captureNode);
  captureNode.connect(audioCtx.destination);
  say("microphone live at " + audioCtx.sampleRate + " Hz (script processor)");
}

function sendFrame(bytes, peak) {
  if (!started || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ event: "media", media: { payload: b64(bytes) } }));
  framesIn++;
  if (framesIn % 5 === 0) $("level").style.width = Math.min(100, peak * 180) + "%";
  const now = performance.now();
  if (peak > 0.06) { lastLoudAt = now; quietAnnounced = false; }
}

// --- playback ---------------------------------------------------------- //
// Chunks are scheduled back to back on the audio clock rather than played on
// arrival, so a late frame does not leave a gap in the middle of a word. The
// context runs at 8 kHz, so the buffer plays as it is -- no interpolation.

function play(bytes) {
  if (!audioCtx) return;
  const samples = bytes.length / 2;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const buffer = audioCtx.createBuffer(1, samples, RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples; i++) channel[i] = view.getInt16(i * 2, true) / 0x8000;

  const now = performance.now();
  if (!agentTalking || now - lastAgentAudioAt > 400) {
    agentTalking = true;
    if (lastLoudAt && !quietAnnounced && now - lastLoudAt < 10000) {
      quietAnnounced = true;
      say("reply started " + Math.round(now - lastLoudAt) + " ms after you went quiet");
    }
  }
  lastAgentAudioAt = now;

  const src = audioCtx.createBufferSource();
  src.buffer = buffer;
  src.connect(audioCtx.destination);
  const t = audioCtx.currentTime;
  if (playAt < t + 0.005) playAt = t + JITTER_S;
  src.start(playAt);
  playAt += buffer.duration;
  sources.push(src);
  src.onended = () => { sources = sources.filter((s) => s !== src); };
}

function flush() {
  for (const src of sources) { try { src.stop(); } catch (e) {} }
  sources = [];
  playAt = 0;
  agentTalking = false;
}

function stop(sendStop) {
  started = false;
  if (sendStop && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ event: "stop" }));
  }
  flush();
  if (captureNode) { try { captureNode.disconnect(); } catch (e) {} captureNode = null; }
  if (micStream) { micStream.getTracks().forEach((t) => t.stop()); micStream = null; }
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  if (ws && ws.readyState === WebSocket.OPEN) ws.close();
  $("call").disabled = false;
  $("hang").disabled = true;
  $("level").style.width = "0";
  state("not connected", false);
}

$("call").onclick = start;
$("hang").onclick = () => { say("hanging up"); stop(true); };
</script>
</body>
</html>
"""


def register_browser_call(app: FastAPI) -> None:
    """Serve the page at ``/dev/call``, in development only.

    The guard is on the request rather than on registration so that the route
    reports why it is unavailable instead of returning a bare 404 that reads
    like a broken deployment.
    """

    @app.get("/dev/call")
    async def browser_call() -> Response:
        if get_settings().app_env != "development":
            log.warning("devtools.browser_call_refused", app_env=get_settings().app_env)
            return Response(
                "The browser test page is development-only.",
                status_code=404,
                media_type="text/plain",
            )
        # The socket checks `TELEPHONY_WS_TOKEN` on every connection, and this
        # page is served by the process that holds it. Substituting it here
        # means opening the page is the whole setup -- the alternative was the
        # operator pasting a token into a query string, and getting a bare 403
        # from the socket when they forgot.
        token = get_settings().telephony_ws_token
        query = f"?token={quote(token, safe='')}" if token else ""
        return Response(
            PAGE.replace("__SOCKET_QUERY__", query), media_type="text/html; charset=utf-8"
        )


__all__ = ("PAGE", "register_browser_call")
