import { describe, expect, it } from "vitest";

import type { LiveCall, LiveSnapshot, TurnEvent } from "@/lib/contract";
import {
  applyLiveEvent,
  initialLiveState,
  parseLiveEvent,
  type LiveEvent,
  type LiveState,
} from "@/lib/live-reducer";

const call = (overrides: Partial<LiveCall> = {}): LiveCall => ({
  id: "c1",
  callRef: "CALL-1",
  startedAt: "2026-09-02T06:10:00Z",
  direction: "inbound",
  centreCode: "NKSK-STP-01",
  centreName: "Sitapur",
  language: "hi",
  farmerName: "राम सिंह",
  callerLast4: "8412",
  elapsedSeconds: 10,
  turnCount: 0,
  lastIntent: null,
  activity: "listening",
  lastReplyMs: null,
  campaignId: null,
  ...overrides,
});

const snapshot = (overrides: Partial<LiveSnapshot> = {}): LiveSnapshot => ({
  capacity: 30,
  calls: [],
  today: {
    calls: 10,
    inbound: 6,
    outbound: 4,
    firstReplyP50Ms: 640,
    firstReplyP95Ms: 1100,
    handledByAgentPct: 90,
    transferred: 1,
    offersAccepted: 2,
    offersPitched: 4,
  },
  recent: [],
  ...overrides,
});

const turn = (overrides: Partial<TurnEvent> = {}): TurnEvent => ({
  callId: "c1",
  turnIndex: 0,
  role: "agent",
  text: "नमस्ते",
  at: 0,
  latency: { totalMs: 48, fromCache: true },
  tools: [],
  ...overrides,
});

const fold = (state: LiveState, ...events: LiveEvent[]) => events.reduce(applyLiveEvent, state);

describe("the live reducer", () => {
  it("adds a call when it starts and ignores a duplicate start", () => {
    const state = fold(
      initialLiveState(snapshot()),
      { name: "call.started", data: call() },
      { name: "call.started", data: call() },
    );
    expect(state.calls.map((c) => c.id)).toEqual(["c1"]);
  });

  it("grows the transcript turn by turn and records the last reply", () => {
    const state = fold(
      initialLiveState(snapshot({ calls: [call()] })),
      { name: "call.turn", data: turn({ turnIndex: 1, role: "farmer", text: "हाँ", at: 6, latency: null }) },
      { name: "call.turn", data: turn({ turnIndex: 0 }) },
      { name: "call.turn", data: turn({ turnIndex: 2, at: 8, latency: { totalMs: 612, fromCache: false } }) },
    );
    expect(state.transcripts.c1?.turns.map((t) => t.turnIndex)).toEqual([0, 1, 2]);
    expect(state.calls[0]?.turnCount).toBe(3);
    expect(state.calls[0]?.lastReplyMs).toBe(612);
  });

  it("follows identification, activity and a hand-over", () => {
    const state = fold(
      initialLiveState(snapshot({ calls: [call({ farmerName: null, callerLast4: null })] })),
      {
        name: "call.identified",
        data: {
          callId: "c1",
          farmerName: "गीता देवी",
          callerLast4: "2907",
          centreCode: "NKSK-BBK-01",
          centreName: "Barabanki",
          language: "hi",
          campaignId: null,
        },
      },
      { name: "call.activity", data: { callId: "c1", activity: "speaking" } },
      { name: "call.transfer", data: { callId: "c1", to: "Sitapur agronomist line", reason: "asked" } },
    );
    expect(state.calls[0]).toMatchObject({
      farmerName: "गीता देवी",
      callerLast4: "2907",
      centreName: "Barabanki",
      activity: "transferring",
    });
    expect(state.transcripts.c1?.notes).toEqual([
      { kind: "transfer", at: null, to: "Sitapur agronomist line", reason: "asked" },
    ]);
  });

  it("moves an ended call to the finished list and tallies the day", () => {
    const state = fold(
      initialLiveState(snapshot({ calls: [call({ direction: "outbound" })] })),
      { name: "call.dtmf", data: { callId: "c1", digit: "1", at: 34 } },
      {
        name: "call.ended",
        data: { callId: "c1", status: "completed", outcome: "offer_accepted", durationSeconds: 58, firstReplyMs: 612 },
      },
    );
    expect(state.calls).toEqual([]);
    expect(state.recent[0]).toMatchObject({
      id: "c1",
      outcome: "offer_accepted",
      durationSeconds: 58,
      firstReplyMs: 612,
      dtmf: "1",
    });
    expect(state.today).toMatchObject({
      calls: 11,
      outbound: 5,
      inbound: 6,
      offersAccepted: 3,
      offersPitched: 5,
    });
    // The transcript stays so the operator can finish reading it.
    expect(state.transcripts.c1?.ended).toBe(true);
  });

  it("counts a transferred call against the agent's share", () => {
    const state = fold(
      initialLiveState(snapshot({ calls: [call()] })),
      { name: "call.transfer", data: { callId: "c1", to: "manager", reason: "" } },
      { name: "call.ended", data: { callId: "c1", status: "completed", outcome: "transferred", durationSeconds: 90, firstReplyMs: 600 } },
    );
    expect(state.today.transferred).toBe(2);
    expect(state.today.handledByAgentPct).toBe(Math.round((9 / 11) * 100));
  });

  it("keeps a live call's transcript across a fresh snapshot and drops the rest", () => {
    const before = fold(
      initialLiveState(snapshot({ calls: [call(), call({ id: "c2" })] })),
      { name: "call.turn", data: turn() },
      { name: "call.turn", data: turn({ callId: "c2" }) },
    );
    const after = applyLiveEvent(before, { name: "snapshot", data: snapshot({ calls: [call()] }) });
    expect(Object.keys(after.transcripts)).toEqual(["c1"]);
    expect(after.calls.map((c) => c.id)).toEqual(["c1"]);
  });

  it("merges history under the turns that arrived live", () => {
    const state = fold(
      initialLiveState(snapshot({ calls: [call()] })),
      { name: "call.turn", data: turn({ turnIndex: 2, at: 8, latency: { totalMs: 612, fromCache: false } }) },
      {
        name: "history",
        data: {
          callId: "c1",
          turns: [turn({ turnIndex: 0 }), turn({ turnIndex: 2, at: 8, latency: null })],
          events: [{ at: 4, type: "dtmf", text: "Pressed 1" }],
        },
      },
    );
    const turns = state.transcripts.c1?.turns ?? [];
    expect(turns.map((t) => t.turnIndex)).toEqual([0, 2]);
    // The live copy of turn 2 wins: it carries the latency the row lacked.
    expect(turns[1]?.latency?.totalMs).toBe(612);
    expect(state.transcripts.c1?.notes).toEqual([{ kind: "event", at: 4, type: "dtmf", text: "Pressed 1" }]);
  });

  it("parses only the events it knows, with an object payload", () => {
    expect(parseLiveEvent("call.turn", turn())).toEqual({ name: "call.turn", data: turn() });
    expect(parseLiveEvent("heartbeat", { at: "now" })).toBeNull();
    expect(parseLiveEvent("call.turn", "not an object")).toBeNull();
  });
});
