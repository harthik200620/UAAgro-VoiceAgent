import type { CampaignCounts, CampaignDetail, CampaignSummary, Contact } from "./contract";

/**
 * The campaign page's fold over its event stream: a snapshot, then contacts
 * changing one at a time and the summary changing behind them. Counts are
 * recomputed from the contacts on every contact change rather than trusted
 * from the last `campaign.updated`, so the wall and the numbers above it can
 * never disagree on the same screen.
 */

type StreamEvents = {
  snapshot: CampaignDetail;
  "contact.updated": Contact;
  "campaign.updated": CampaignSummary;
};

type CampaignEvent = {
  [K in keyof StreamEvents]: { name: K; data: StreamEvents[K] };
}[keyof StreamEvents];

export const CAMPAIGN_EVENT_NAMES = [
  "snapshot",
  "contact.updated",
  "campaign.updated",
] as const satisfies readonly (keyof StreamEvents)[];

export function parseCampaignEvent(name: string, data: unknown): CampaignEvent | null {
  if (!(CAMPAIGN_EVENT_NAMES as readonly string[]).includes(name)) return null;
  if (typeof data !== "object" || data === null) return null;
  return { name, data } as CampaignEvent;
}

export function applyCampaignEvent(state: CampaignDetail, event: CampaignEvent): CampaignDetail {
  switch (event.name) {
    case "snapshot":
      return event.data;

    case "contact.updated": {
      const updated = event.data;
      const known = state.contacts.some((contact) => contact.id === updated.id);
      const contacts = known
        ? state.contacts.map((contact) => (contact.id === updated.id ? updated : contact))
        : [...state.contacts, updated];
      return { ...state, contacts, counts: countContacts(contacts) };
    }

    case "campaign.updated":
      return { ...state, ...event.data, contacts: state.contacts };
  }
}

export function countContacts(contacts: readonly Contact[]): CampaignCounts {
  const counts: CampaignCounts = {
    total: contacts.length,
    done: 0,
    inCall: 0,
    noAnswer: 0,
    waiting: 0,
    pressed1: 0,
    pressed2: 0,
    talked: 0,
    optedOut: 0,
    removed: 0,
  };
  for (const contact of contacts) {
    switch (contact.status) {
      case "done":
        counts.done += 1;
        break;
      case "in_call":
        counts.inCall += 1;
        break;
      case "no_answer":
        counts.noAnswer += 1;
        break;
      case "waiting":
        counts.waiting += 1;
        break;
      case "removed":
        counts.removed += 1;
        break;
    }
    switch (contact.outcome) {
      case "pressed_1":
        counts.pressed1 += 1;
        break;
      case "pressed_2":
        counts.pressed2 += 1;
        break;
      case "talked":
        counts.talked += 1;
        break;
      case "opted_out":
        counts.optedOut += 1;
        break;
    }
  }
  return counts;
}
