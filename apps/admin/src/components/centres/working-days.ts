/**
 * ["mon", ..., "sat"] as "Mon–Sat", or "Mon–Fri, Sun" when the week has a
 * gap. Pure, so the table and the editor agree on the wording.
 */
export const WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

export type Weekday = (typeof WEEK)[number];

export function describeDays(days: readonly string[], name: (day: Weekday) => string): string {
  const chosen = WEEK.filter((day) => days.includes(day));
  if (chosen.length === 0) return "—";

  const runs: Weekday[][] = [];
  for (const day of chosen) {
    const last = runs[runs.length - 1];
    if (last && WEEK.indexOf(day) === WEEK.indexOf(last[last.length - 1] as Weekday) + 1) last.push(day);
    else runs.push([day]);
  }
  return runs
    .map((run) => {
      const first = run[0] as Weekday;
      const last = run[run.length - 1] as Weekday;
      return run.length === 1 ? name(first) : `${name(first)}–${name(last)}`;
    })
    .join(", ");
}
