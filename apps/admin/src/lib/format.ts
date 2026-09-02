import { FORMAT_LOCALE } from "@/i18n/routing";

/**
 * Dates, times and numbers the way the panel shows them.
 *
 * Everything is `en-IN` in IST whichever language the labels are in: every
 * timestamp here is about when a farmer's phone rang in Uttar Pradesh, and a
 * browser-local rendering would show a manager travelling outside the state
 * the wrong calling window. The builders return the mockups' exact shapes
 * ("1:52", "612 ms", "Tuesday 2 September 2026") so the same value looks the
 * same in every table.
 */

const TIME_ZONE = "Asia/Kolkata";

const clock = new Intl.DateTimeFormat(FORMAT_LOCALE, {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: TIME_ZONE,
});
const dayMonth = new Intl.DateTimeFormat(FORMAT_LOCALE, {
  day: "numeric",
  month: "short",
  timeZone: TIME_ZONE,
});
const longParts = new Intl.DateTimeFormat(FORMAT_LOCALE, {
  weekday: "long",
  day: "numeric",
  month: "long",
  year: "numeric",
  timeZone: TIME_ZONE,
});
const isoDay = new Intl.DateTimeFormat("en-CA", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  timeZone: TIME_ZONE,
});
const integer = new Intl.NumberFormat(FORMAT_LOCALE, { maximumFractionDigits: 0 });

function toDate(value: string | Date): Date {
  return value instanceof Date ? value : new Date(value);
}

/** "11:40" */
export function formatTime(value: string | Date): string {
  return clock.format(toDate(value));
}

/** "2 Sep" */
export function formatDate(value: string | Date): string {
  return dayMonth.format(toDate(value));
}

/**
 * "Tuesday 2 September 2026". Joined by hand because `en-IN` puts a comma
 * after the weekday and the design does not.
 */
export function formatLongDate(value: string | Date, withYear = true): string {
  const parts = longParts.formatToParts(toDate(value));
  const pick = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  const text = `${pick("weekday")} ${pick("day")} ${pick("month")}`;
  return withYear ? `${text} ${pick("year")}` : text;
}

/** "Tuesday 2 September, 11:37" */
export function formatDateTime(value: string | Date): string {
  return `${formatLongDate(value, false)}, ${formatTime(value)}`;
}

/** "1:52", or a dash when the call never connected. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  const whole = Math.max(0, Math.round(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** "01:24": transcript timestamps and the elapsed counter on a live call. */
export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${String(Math.floor(whole / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
}

/** "612 ms" under a second, "1.1 s" above it. */
export function formatMs(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${integer.format(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** "1,884", and lakh grouping above a lakh. */
export function formatCount(value: number): string {
  return integer.format(value);
}

export function formatBytes(bytes: number | null): string {
  if (bytes === null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

/** Which day a timestamp falls on relative to now, in IST. */
export function dayOf(value: string | Date, now: Date): "today" | "yesterday" | "earlier" {
  const day = isoDay.format(toDate(value));
  if (day === isoDay.format(now)) return "today";
  if (day === isoDay.format(new Date(now.getTime() - 86_400_000))) return "yesterday";
  return "earlier";
}

/** Seconds since a timestamp, never negative. */
export function secondsSince(value: string, now: Date): number {
  return Math.max(0, (now.getTime() - toDate(value).getTime()) / 1000);
}

/** "Ops Manager" -> "OM", for the avatar tile. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word.charAt(0).toUpperCase())
    .join("");
}
