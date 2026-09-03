import { useTranslations } from "next-intl";

import { Card } from "@/components/ui/card";
import type { Overview } from "@/lib/contract";
import { formatCount } from "@/lib/format";
import { funnelSteps } from "@/lib/overview";

/**
 * The outbound funnel: numbers dialled, farmers reached, and what they
 * pressed, each bar a share of the first. One colour -- the outbound
 * series' -- because the stages are one story narrowing, not five things.
 */
export function Funnel({ outbound }: { outbound: Overview["outbound"] }) {
  const t = useTranslations("overview.funnel");
  const steps = funnelSteps(outbound);

  return (
    <Card className="flex flex-1 flex-col gap-3 px-5.5 pb-4.5 pt-4.5">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-semibold">{t("title")}</span>
        <span className="text-small text-muted">{t("running", { count: outbound.campaignsRunning })}</span>
      </div>
      {outbound.contactsDialled === 0 ? (
        <p className="py-4 text-ui text-muted">{t("empty")}</p>
      ) : (
        <ol className="flex flex-col gap-2">
          {steps.map((step) => (
            <li key={step.key} className="grid grid-cols-[110px_1fr_56px] items-center gap-3 text-small">
              <span className="text-muted">{t(`steps.${step.key}`)}</span>
              <span
                role="img"
                aria-label={t("share", { value: formatCount(step.value), percent: Math.round(step.share * 100) })}
                className="h-3.5 w-full overflow-hidden rounded-r bg-inset"
              >
                <span
                  className="block h-full rounded-r-[4px] bg-series-outbound"
                  style={{ width: `${Math.max(step.value > 0 ? 1 : 0, step.share * 100)}%` }}
                />
              </span>
              <span className="text-right font-mono text-label">{formatCount(step.value)}</span>
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}
