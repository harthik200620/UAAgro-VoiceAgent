import { useTranslations } from "next-intl";

import { Card } from "@/components/ui/card";
import type { Overview } from "@/lib/contract";
import { formatCount, formatMs } from "@/lib/format";

/** "Reply speed": the first reply as a numeral, the tail, every reply, and how many were inside the budget. */
export function SpeedCard({ speed }: { speed: Overview["speed"] }) {
  const t = useTranslations("overview.speed");
  const within = speed.withinBudgetPct === null ? null : Math.round(speed.withinBudgetPct);

  return (
    <Card className="flex flex-1 flex-col gap-3 px-5.5 pb-4.5 pt-4.5">
      <div className="font-semibold">{t("title")}</div>
      <div className="flex items-baseline gap-2">
        <span className="font-serif text-num-lg">
          {speed.firstReplyP50Ms === null ? "—" : formatCount(Math.round(speed.firstReplyP50Ms))}
        </span>
        <span className="text-xl text-muted">ms</span>
        <span className="ml-auto text-small text-muted">{t("firstReply")}</span>
      </div>
      <dl className="flex flex-col gap-1.5 text-small">
        <Row label={t("p95")} value={formatMs(speed.firstReplyP95Ms)} />
        <Row label={t("everyReply")} value={formatMs(speed.replyP50Ms)} />
        <Row label={t("withinBudget")} value={within === null ? "—" : `${within}%`} />
      </dl>
      <div
        role="img"
        aria-label={t("budgetBar", { percent: within ?? 0 })}
        className="h-2 w-full overflow-hidden rounded-full bg-inset"
      >
        <div className="h-full rounded-full bg-ink" style={{ width: `${within ?? 0}%` }} />
      </div>
      <p className="text-label text-faint">{t("budgetNote")}</p>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted">{label}</dt>
      <dd className="font-mono text-label">{value}</dd>
    </div>
  );
}
