// Talk to the agent from a browser, with no telephony account at all.
//
// This is not a simulator. The page opens the same media socket a provider
// opens, speaks the same Exotel AgentStream frames, and is answered by the
// same session, pipeline, recogniser, model and voice. The only thing
// standing in for the phone network is the browser: a microphone where the
// handset would be, and AudioContext where the carrier would be.
//
// The audio context runs at 8 kHz, the telephony rate. At the browser's
// 48 kHz both conversions happened in script -- an average on the way in,
// which aliases, and an 8 kHz buffer handed to a 48 kHz graph on the way
// out, which Chrome upsamples by linear interpolation: a steady hiss under
// every word. At 8 kHz the browser does both with a proper filter.

const body = document.body;
const SOCKET = body.dataset.socket;        // injected by the server, token included
const answering = body.dataset.answer;     // a contact id when picking up an outbound call
const RATE = 8000, FRAME = 160;            // 20 ms of 8 kHz mono
const JITTER_S = 0.06;                     // playback runs this far behind arrival
const $ = (id) => document.getElementById(id);
const logEl = $("log");
let ws, audioCtx, micStream, captureNode, started = false;
let playAt = 0, sources = [], agentTalking = false, lastAgentAudioAt = 0;
let lastLoudAt = 0, quietAnnounced = false, framesIn = 0;

function say(line) {
  const t = new Date().toLocaleTimeString();
  logEl.textContent += `\n${t}  ${line}`;
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

  ws = new WebSocket(SOCKET);
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
        // Answering an outbound call the dialer placed in simulator mode:
        // the contact reference rides in the same field a real provider
        // echoes back, so the worker treats this as the call it dialled.
        custom_parameters: answering ? { custom_field: "contact:" + answering } : {},
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
