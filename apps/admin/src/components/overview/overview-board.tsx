"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { Chip } from "@/components/ui/chip";
import { Icon } from "@/components/ui/icon";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { usePolling } from "@/hooks/use-polling";
import { useNow } from "@/hooks/use-now";
import { Link } from "@/i18n/routing";
import type { Overview, OverviewRange } from "@/lib/contract";
import { formatCount, formatLongDate, formatTime } from "@/lib/format";
import { percent } from "@/lib/overview";

import { AttentionList } from "./attention-list";
import { ByHourChart } from "./by-hour-chart";
import { Funnel } from "./funnel";
import { RangeSwitch } from "./range-switch";
import { RecentCalls } from "./recent-calls";
import { SpeedCard } from "./speed-card";
import { TopQuestions } from "./top-questions";

const REFRESH_MS = 30_000;

/**
 * The first page: what is happening, what matters, how many calls came in
 * and went out. The server draws it once with the figures it fetched; from
 * then on the board asks the panel's own `/api/overview` every thirty
 * seconds and redraws in place, holding the last good figures if a refresh
 * fails and saying so.
 */
export function OverviewBoard({
  initial,
  range,
  renderedAt,
}: {
  initial: Overview;
  range: OverviewRange;
  /** The server's clock at render, so the first client frame matches the HTML. */
  renderedAt: string;
}) {
  const t = useTranslations("overview");
  const { data, refreshedAt, stale } = usePolling<Overview>(
    `/api/overview?range=${range}`,
    initial,
    REFRESH_MS,
  );
  const now = useNow(renderedAt, 60_000);
  const { calls } = data;
  const share = (part: number) => {
    const value = percent(part, calls.total);
    return value === null ? t("stats.noCalls") : t("stats.ofCalls", { percent: value, total: formatCount(calls.total) });
  };

  return (
    <>
      <PageHeader title={t("title")} subtitle={`${formatLongDate(now)} · ${t("subtitle")}`}>
        {stale ? <Chip tone="grey">{t("stale")}</Chip> : null}
        <RangeSwitch active={range} />
        <Link
          href="/live"
          className={clsx(
            "inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-body font-semibold",
            data.live.calls > 0 ? "bg-amber-bg text-amber-text" : "bg-inset text-muted",
          )}
        >
          <span
            aria-hidden="true"
            className={clsx("h-2 w-2 rounded-full", data.live.calls > 0 ? "animate-live bg-amber" : "bg-grey")}
          />
          {t("liveNow", { count: data.live.calls })}
          <span className="font-normal opacity-80">· {t("ofCapacity", { capacity: data.live.capacity })}</span>
        </Link>
      </PageHeader>

      <div className="flex flex-wrap gap-4">
        <StatCard label={t("stats.inbound")} value={formatCount(calls.inbound)} note={share(calls.inbound)} />
        <StatCard
          label={t("stats.outbound")}
          value={formatCount(calls.outbound)}
          note={t("stats.campaignsRunning", { count: data.outbound.campaignsRunning })}
        />
        <StatCard
          label={t("stats.answered")}
          value={formatCount(calls.answeredByAgent)}
          note={share(calls.answeredByAgent)}
        />
        <StatCard label={t("stats.transferred")} value={formatCount(calls.transferred)} note={share(calls.transferred)} />
        <StatCard label={t("stats.missed")} value={formatCount(calls.missed)} note={share(calls.missed)} />
      </div>
      <p className="-mt-2 flex items-center gap-2 text-small text-muted">
        <Icon name="clock" size={13} />
        {t("testCalls", { count: calls.testCalls })}
        <span className="text-faint">
          · {t("generatedAt", { time: formatTime(refreshedAt ?? data.generatedAt) })}
        </span>
      </p>

      <div className="flex flex-wrap items-start gap-4">
        <div className="flex min-w-0 flex-1 basis-[640px] flex-col gap-4">
          <div className="flex flex-wrap gap-4">
            <SpeedCard speed={data.speed} />
            <Funnel outbound={data.outbound} />
          </div>
          <ByHourChart rows={data.byHour} range={range} />
          <div className="flex flex-wrap items-start gap-4">
            <TopQuestions questions={data.topQuestions} outcomes={data.outcomes} />
            <RecentCalls rows={data.recent} />
          </div>
        </div>
        <div className="w-full shrink-0 xl:w-[380px]">
          <AttentionList items={data.attention} />
        </div>
      </div>
    </>
  );
}
