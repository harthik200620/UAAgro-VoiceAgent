"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { DirectionLabel } from "@/components/status/direction-label";
import { Last4 } from "@/components/status/last4";
import { Chip } from "@/components/ui/chip";
import type { Activity, LiveCall } from "@/lib/contract";
import { formatClock, formatMs, secondsSince } from "@/lib/format";
import { activityTone } from "@/lib/tones";

/** The activity chip: what the agent is doing on this call right now, in words and a tone. */
function ActivityChip({ activity }: { activity: Activity }) {
  const t = useTranslations("live.activity");
  return <Chip tone={activityTone(activity)}>{t(activity)}</Chip>;
}

/**
 * One call in the "In progress" list. A button, because choosing it shows
 * its transcript on the right; `aria-pressed` says which one is open.
 */
export function CallCard({
  call,
  selected,
  now,
  onSelect,
}: {
  call: LiveCall;
  selected: boolean;
  now: Date;
  onSelect: (id: string) => void;
}) {
  const t = useTranslations("live");

  return (
    <li>
      <button
        type="button"
        aria-pressed={selected}
        onClick={() => onSelect(call.id)}
        className={clsx(
          "flex w-full flex-col gap-2 rounded-panel border px-4 py-3.5 text-left transition-colors",
          selected ? "border-line bg-inset" : "border-transparent bg-surface hover:bg-paper",
        )}
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <span aria-hidden="true" className="h-2 w-2 shrink-0 animate-live rounded-full bg-amber" />
            <span lang="hi" className="truncate text-name font-semibold">
              {call.farmerName ?? t("unknownFarmer")}
            </span>
            <Last4 value={call.callerLast4} className="text-label" />
          </div>
          <span className="font-mono text-body">{formatClock(secondsSince(call.startedAt, now))}</span>
        </div>
        <div className="flex items-center justify-between gap-2 text-small">
          <DirectionLabel
            direction={call.direction}
            detail={call.centreName ?? call.centreCode ?? t("noCentre")}
          />
          <ActivityChip activity={call.activity} />
        </div>
        <div className="text-label text-muted">
          {t("lastReply")}{" "}
          <span className="font-mono text-label text-ink">{formatMs(call.lastReplyMs)}</span>
        </div>
      </button>
    </li>
  );
}
