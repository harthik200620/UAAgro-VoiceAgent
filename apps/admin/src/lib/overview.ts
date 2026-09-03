import type { AttentionItem, AttentionSeverity, ByHourRow, Overview } from "./contract";

/**
 * The Overview's arithmetic, kept pure so it can be tested without a browser:
 * what order the attention list is read in, how the by-hour chart is scaled,
 * and how the outbound funnel narrows.
 */

const SEVERITY_RANK: Record<AttentionSeverity, number> = { high: 0, medium: 1, low: 2 };

/** Most urgent first; within a severity the API's order stands. */
export function sortAttention(items: readonly AttentionItem[]): AttentionItem[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort(
      (a, b) =>
        SEVERITY_RANK[a.item.severity] - SEVERITY_RANK[b.item.severity] || a.index - b.index,
    )
    .map(({ item }) => item);
}

/** 7 -> 10, 23 -> 25, 140 -> 200: the next clean ceiling for an axis. */
export function niceCeiling(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const leading = value / magnitude;
  const step = leading <= 1 ? 1 : leading <= 2 ? 2 : leading <= 2.5 ? 2.5 : leading <= 5 ? 5 : 10;
  return step * magnitude;
}

export type ChartScale = { max: number; ticks: number[] };

/** The y-axis of the by-hour chart: a clean top and four gridlines under it. */
export function chartScale(rows: readonly ByHourRow[]): ChartScale {
  const largest = rows.reduce((max, row) => Math.max(max, row.inbound, row.outbound), 0);
  const max = niceCeiling(Math.max(1, largest));
  const ticks = [0.25, 0.5, 0.75, 1].map((share) => share * max);
  return { max, ticks };
}

export type Totals = { inbound: number; outbound: number };

export function byHourTotals(rows: readonly ByHourRow[]): Totals {
  return rows.reduce(
    (sum, row) => ({ inbound: sum.inbound + row.inbound, outbound: sum.outbound + row.outbound }),
    { inbound: 0, outbound: 0 },
  );
}

/**
 * Which bars get a value written on them: the tallest of each series, and
 * only when it is not zero. A number on every bar is noise; one on the peak
 * is the figure people ask about.
 */
export function labelledBars(rows: readonly ByHourRow[]): Record<keyof Totals, number | null> {
  const peak = (series: keyof Totals): number | null => {
    let best: number | null = null;
    rows.forEach((row, index) => {
      if (row[series] > 0 && (best === null || row[series] > (rows[best]?.[series] ?? 0))) {
        best = index;
      }
    });
    return best;
  };
  return { inbound: peak("inbound"), outbound: peak("outbound") };
}

/** How often to write an x-axis label so 24 or 30 columns do not collide. */
export function labelEvery(count: number): number {
  if (count <= 8) return 1;
  if (count <= 14) return 2;
  if (count <= 24) return 3;
  return 5;
}

export type FunnelStepKey = "dialled" | "reached" | "pressed1" | "pressed2" | "optedOut";

export type FunnelStep = { key: FunnelStepKey; value: number; share: number };

/**
 * Dialled → reached → what they pressed, each as a share of the numbers
 * dialled, so the bars narrow the way the calls did. With nothing dialled
 * every share is zero rather than a division by zero.
 */
export function funnelSteps(outbound: Overview["outbound"]): FunnelStep[] {
  const base = outbound.contactsDialled;
  const share = (value: number) => (base > 0 ? Math.min(1, value / base) : 0);
  return [
    { key: "dialled", value: base, share: base > 0 ? 1 : 0 },
    { key: "reached", value: outbound.reached, share: share(outbound.reached) },
    { key: "pressed1", value: outbound.pressed1, share: share(outbound.pressed1) },
    { key: "pressed2", value: outbound.pressed2, share: share(outbound.pressed2) },
    { key: "optedOut", value: outbound.optedOut, share: share(outbound.optedOut) },
  ];
}

/** Whole percentages for the stat cards; null when there is nothing to divide by. */
export function percent(part: number, whole: number): number | null {
  if (whole <= 0) return null;
  return Math.round((part / whole) * 100);
}
