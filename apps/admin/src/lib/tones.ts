import type {
  Activity,
  AttentionSeverity,
  CampaignStatus,
  Contact,
  IngestStatus,
  SyncRun,
} from "./contract";

/**
 * Meaning to colour, decided in one place.
 *
 * Amber is a call in progress, green is done or fine, red is not reached or
 * failed, grey is waiting or switched off, ink is a hand-over to a person.
 * Every chip that uses a tone also carries a text label, so colour never
 * carries meaning alone -- the tone is emphasis, the words are the message.
 */
export type Tone = "green" | "amber" | "red" | "grey" | "ink";

const GOOD = new Set([
  "resolved",
  "offer_accepted",
  "pressed_1",
  "pressed_2",
  "wanted_details",
  "talked",
  "answered",
  "completed",
  "callback_created",
  "handled",
]);
const BAD = new Set([
  "no_answer",
  "busy",
  "failed",
  "system_failure",
  "wrong_person",
  "error",
  "dropped",
  "spam",
]);
const HANDED = new Set(["transferred", "handed_to_manager", "transfer"]);

/** The outcomes the panel knows, for the Calls filter's suggestions. */
export const KNOWN_OUTCOMES: readonly string[] = [
  ...GOOD,
  ...HANDED,
  ...BAD,
  "opted_out",
  "cancelled",
  "voicemail",
];

export function outcomeTone(outcome: string | null): Tone {
  if (!outcome) return "grey";
  if (GOOD.has(outcome)) return "green";
  if (BAD.has(outcome)) return "red";
  if (HANDED.has(outcome)) return "ink";
  return "grey";
}

/**
 * A translation key for an outcome the API names. Dots are next-intl's path
 * separator, so anything that is not a plain identifier is folded into an
 * underscore; the label falls back to `humanize` when no translation exists.
 */
export function messageKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9_]+/g, "_");
}

/** "no_answer" -> "No answer": the fallback for a value with no translation. */
export function humanize(value: string): string {
  const text = value.replace(/[_-]+/g, " ").trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function campaignTone(status: CampaignStatus): Tone {
  switch (status) {
    case "running":
    case "pending_approval":
      return "amber";
    case "completed":
      return "green";
    case "cancelled":
      return "red";
    case "draft":
    case "approved":
    case "scheduled":
    case "paused":
      return "grey";
  }
}

export function activityTone(activity: Activity): Tone {
  switch (activity) {
    case "speaking":
    case "thinking":
      return "amber";
    case "transferring":
      return "ink";
    case "listening":
      return "grey";
  }
}

export function ingestTone(status: IngestStatus, isPublished: boolean): Tone {
  if (!isPublished && status === "indexed") return "grey";
  switch (status) {
    case "indexed":
      return "green";
    case "failed":
      return "red";
    case "pending":
    case "indexing":
      return "amber";
  }
}

/** The word on a contact card: the outcome once there is one, else the state. */
export function contactLabelKey(contact: Contact): string {
  if (contact.status === "done" || contact.status === "no_answer") {
    return contact.outcome ?? contact.status;
  }
  return contact.status;
}

/** Red for what needs someone now, amber for today, grey for when there is time. */
export function severityTone(severity: AttentionSeverity): Tone {
  switch (severity) {
    case "high":
      return "red";
    case "medium":
      return "amber";
    case "low":
      return "grey";
  }
}

export function syncTone(status: SyncRun["status"]): Tone {
  switch (status) {
    case "running":
      return "amber";
    case "ok":
      return "green";
    case "failed":
      return "red";
  }
}
