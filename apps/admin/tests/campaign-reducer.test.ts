import { describe, expect, it } from "vitest";

import { applyCampaignEvent, countContacts, parseCampaignEvent } from "@/lib/campaign-reducer";
import type { CampaignDetail, Contact } from "@/lib/contract";

const contact = (overrides: Partial<Contact> = {}): Contact => ({
  id: "k1",
  farmerName: "राम सिंह",
  last4: "4127",
  status: "waiting",
  outcome: null,
  dtmf: null,
  callId: null,
  attempts: 0,
  lastAttemptAt: null,
  removedReason: null,
  answerUrl: null,
  ...overrides,
});

const campaign = (contacts: Contact[]): CampaignDetail => ({
  id: "camp1",
  name: "डीएपी ऑफ़र",
  status: "running",
  flowId: "f1",
  flowName: "Offer",
  flowVersion: 3,
  createdAt: "2026-09-02T04:32:00Z",
  createdByName: "Ops",
  startedAt: "2026-09-02T04:34:00Z",
  maxConcurrent: 10,
  windowStart: "10:00",
  windowEnd: "18:00",
  counts: countContacts(contacts),
  firstReplyP50Ms: 690,
  canApprove: false,
  blockedBy: [],
  contacts,
});

describe("the campaign reducer", () => {
  it("updates a contact in place and recounts", () => {
    const before = campaign([contact(), contact({ id: "k2" })]);
    const after = applyCampaignEvent(before, {
      name: "contact.updated",
      data: contact({ status: "done", outcome: "pressed_1", dtmf: "1", callId: "c9" }),
    });
    expect(after.contacts.map((c) => c.id)).toEqual(["k1", "k2"]);
    expect(after.counts).toMatchObject({ total: 2, done: 1, waiting: 1, pressed1: 1 });
  });

  it("keeps the contacts when only the summary changes", () => {
    const before = campaign([contact()]);
    const after = applyCampaignEvent(before, {
      name: "campaign.updated",
      data: { ...before, status: "paused", maxConcurrent: 5 },
    });
    expect(after.status).toBe("paused");
    expect(after.maxConcurrent).toBe(5);
    expect(after.contacts).toHaveLength(1);
  });

  it("counts every status and outcome", () => {
    const counts = countContacts([
      contact({ status: "done", outcome: "pressed_1" }),
      contact({ id: "2", status: "done", outcome: "pressed_2" }),
      contact({ id: "3", status: "done", outcome: "talked" }),
      contact({ id: "4", status: "done", outcome: "opted_out" }),
      contact({ id: "5", status: "no_answer", outcome: "no_answer" }),
      contact({ id: "6", status: "in_call" }),
      contact({ id: "7", status: "removed", removedReason: "dnd" }),
      contact({ id: "8" }),
      // A ringing phone is a line in use, so it counts with the calls in progress.
      contact({ id: "9", status: "ringing", answerUrl: "http://127.0.0.1:8080/dev/call?answer=k9" }),
    ]);
    expect(counts).toEqual({
      total: 9,
      done: 4,
      inCall: 2,
      noAnswer: 1,
      waiting: 1,
      removed: 1,
      pressed1: 1,
      pressed2: 1,
      talked: 1,
      optedOut: 1,
    });
  });

  it("ignores heartbeats and turns", () => {
    expect(parseCampaignEvent("heartbeat", { at: "now" })).toBeNull();
    expect(parseCampaignEvent("call.turn", { callId: "c1" })).toBeNull();
    expect(parseCampaignEvent("contact.updated", contact())).toEqual({
      name: "contact.updated",
      data: contact(),
    });
  });
});
