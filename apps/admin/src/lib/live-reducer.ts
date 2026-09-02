import type {
  Activity,
  CallEvent,
  LiveCall,
  LiveSnapshot,
  LiveToday,
  RecentCall,
  TurnEvent,
} from "./contract";

/**
 * The Live page's state machine, as a pure function.
 *
 * The contract sends a snapshot first and deltas after it, and the page is a
 * fold of those deltas over the snapshot. Keeping the fold pure means it can
 * be unit-tested with a handful of events and no browser, and it means the
 * SSE hook, the server action that back-fills history, and a reconnect all
 * feed the same function rather than three slightly different ones.
 */

export type CallNote = { at: number | null } & (
  | { kind: "dtmf"; digit: string }
  | { kind: "transfer"; to: string; reason: string }
  | { kind: "ended"; outcome: string | null }
  /** Already-worded history from GET /admin/calls/{id}. */
  | { kind: "event"; type: string; text: string }
);

export type CallTranscript = {
  turns: TurnEvent[];
  notes: CallNote[];
  ended: boolean;
};

export type LiveState = LiveSnapshot & {
  /** By call id. Kept after a call ends so the operator can finish reading. */
  transcripts: Record<string, CallTranscript>;
};

type Identified = {
  callId: string;
  farmerName: string | null;
  callerLast4: string | null;
  centreCode: string | null;
  centreName: string | null;
  language: string;
  campaignId: string | null;
};

type Ended = {
  callId: string;
  status: string;
  outcome: string | null;
  durationSeconds: number | null;
  firstReplyMs: number | null;
};

type StreamEvents = {
  snapshot: LiveSnapshot;
  "call.started": LiveCall;
  "call.identified": Identified;
  "call.activity": { callId: string; activity: Activity };
  "call.turn": TurnEvent;
  "call.dtmf": { callId: string; digit: string; at: number };
  "call.transfer": { callId: string; to: string; reason: string };
  "call.ended": Ended;
};

export type LiveEvent =
  | { [K in keyof StreamEvents]: { name: K; data: StreamEvents[K] } }[keyof StreamEvents]
  /** Local, not from the stream: the transcript of a call that began before the page opened. */
  | { name: "history"; data: { callId: string; turns: TurnEvent[]; events: CallEvent[] } };

export const LIVE_EVENT_NAMES = [
  "snapshot",
  "call.started",
  "call.identified",
  "call.activity",
  "call.turn",
  "call.dtmf",
  "call.transfer",
  "call.ended",
] as const satisfies readonly (keyof StreamEvents)[];

/** A stream message into an event, or nothing when the name is unknown or the payload is not an object. */
export function parseLiveEvent(name: string, data: unknown): LiveEvent | null {
  if (!(LIVE_EVENT_NAMES as readonly string[]).includes(name)) return null;
  if (typeof data !== "object" || data === null) return null;
  // The shape is trusted from here: it is our own API's, checked by the
  // contract types at the other end of the wire.
  return { name, data } as LiveEvent;
}

const EMPTY_TRANSCRIPT: CallTranscript = { turns: [], notes: [], ended: false };

export function initialLiveState(snapshot: LiveSnapshot): LiveState {
  return { ...snapshot, transcripts: {} };
}

export function applyLiveEvent(state: LiveState, event: LiveEvent): LiveState {
  switch (event.name) {
    case "snapshot": {
      // A fresh snapshot after a reconnect replaces whatever drifted while the
      // stream was down. Transcripts of calls still in progress are kept: the
      // snapshot does not carry turns, and the operator was reading them.
      const live = new Set(event.data.calls.map((call) => call.id));
      const transcripts = Object.fromEntries(
        Object.entries(state.transcripts).filter(([id]) => live.has(id)),
      );
      return { ...event.data, transcripts };
    }

    case "call.started": {
      if (state.calls.some((call) => call.id === event.data.id)) return state;
      return { ...state, calls: [...state.calls, event.data] };
    }

    case "call.identified": {
      const { callId, ...fields } = event.data;
      return patchCall(state, callId, fields);
    }

    case "call.activity":
      return patchCall(state, event.data.callId, { activity: event.data.activity });

    case "call.turn": {
      const turn = event.data;
      const transcript = transcriptOf(state, turn.callId);
      const turns = [...transcript.turns.filter((t) => t.turnIndex !== turn.turnIndex), turn].sort(
        (a, b) => a.turnIndex - b.turnIndex,
      );
      const next = withTranscript(state, turn.callId, { ...transcript, turns });
      const patch: Partial<LiveCall> = { turnCount: turns.length };
      if (turn.role === "agent" && turn.latency) patch.lastReplyMs = turn.latency.totalMs;
      return patchCall(next, turn.callId, patch);
    }

    case "call.dtmf":
      return addNote(state, event.data.callId, {
        kind: "dtmf",
        at: event.data.at,
        digit: event.data.digit,
      });

    case "call.transfer": {
      const { callId, to, reason } = event.data;
      const noted = addNote(state, callId, { kind: "transfer", at: null, to, reason });
      return patchCall(noted, callId, { activity: "transferring" });
    }

    case "call.ended": {
      const { callId, outcome, durationSeconds, firstReplyMs } = event.data;
      const transcript = transcriptOf(state, callId);
      const noted = withTranscript(state, callId, {
        ...transcript,
        ended: true,
        notes: [...transcript.notes, { kind: "ended", at: durationSeconds, outcome }],
      });
      const call = state.calls.find((c) => c.id === callId);
      if (!call) return noted;

      const dtmf = lastDigit(transcript.notes);
      const finished: RecentCall = {
        id: call.id,
        startedAt: call.startedAt,
        direction: call.direction,
        farmerName: call.farmerName,
        callerLast4: call.callerLast4,
        centreCode: call.centreCode,
        outcome,
        durationSeconds,
        firstReplyMs,
        dtmf,
      };
      return {
        ...noted,
        calls: noted.calls.filter((c) => c.id !== callId),
        recent: [finished, ...noted.recent.filter((r) => r.id !== callId)].slice(0, 25),
        today: tallyEnded(noted.today, call, transcript, dtmf),
      };
    }

    case "history": {
      const { callId, turns, events } = event.data;
      const transcript = transcriptOf(state, callId);
      // Live turns win over history for the same index: they arrived later
      // and carry the latency the stored row may still be missing.
      const known = new Set(transcript.turns.map((t) => t.turnIndex));
      const merged = [...turns.filter((t) => !known.has(t.turnIndex)), ...transcript.turns].sort(
        (a, b) => a.turnIndex - b.turnIndex,
      );
      const notes: CallNote[] = [
        ...events.map((e) => ({ kind: "event" as const, at: e.at, type: e.type, text: e.text })),
        ...transcript.notes,
      ];
      return withTranscript(state, callId, { ...transcript, turns: merged, notes });
    }
  }
}

function transcriptOf(state: LiveState, callId: string): CallTranscript {
  return state.transcripts[callId] ?? EMPTY_TRANSCRIPT;
}

function withTranscript(state: LiveState, callId: string, transcript: CallTranscript): LiveState {
  return { ...state, transcripts: { ...state.transcripts, [callId]: transcript } };
}

function addNote(state: LiveState, callId: string, note: CallNote): LiveState {
  const transcript = transcriptOf(state, callId);
  return withTranscript(state, callId, { ...transcript, notes: [...transcript.notes, note] });
}

function patchCall(state: LiveState, callId: string, patch: Partial<LiveCall>): LiveState {
  if (!state.calls.some((call) => call.id === callId)) return state;
  return {
    ...state,
    calls: state.calls.map((call) => (call.id === callId ? { ...call, ...patch } : call)),
  };
}

function lastDigit(notes: CallNote[]): string | null {
  for (let i = notes.length - 1; i >= 0; i -= 1) {
    const note = notes[i];
    if (note?.kind === "dtmf") return note.digit;
  }
  return null;
}

/**
 * Today's figures move as calls end. Only what an ended call states for
 * certain is counted here -- the percentiles wait for the next snapshot,
 * because a median cannot be updated from one sample.
 */
function tallyEnded(
  today: LiveToday,
  call: LiveCall,
  transcript: CallTranscript,
  dtmf: string | null,
): LiveToday {
  const outbound = call.direction === "outbound";
  const calls = today.calls + 1;
  const transferred =
    today.transferred + (transcript.notes.some((note) => note.kind === "transfer") ? 1 : 0);
  return {
    ...today,
    calls,
    inbound: today.inbound + (outbound ? 0 : 1),
    outbound: today.outbound + (outbound ? 1 : 0),
    transferred,
    handledByAgentPct: Math.round(((calls - transferred) / calls) * 100),
    offersPitched: today.offersPitched + (outbound ? 1 : 0),
    offersAccepted: today.offersAccepted + (outbound && dtmf === "1" ? 1 : 0),
  };
}
