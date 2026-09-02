import { getTranslations } from "next-intl/server";

import { CampaignChip } from "@/components/status/campaign-chip";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Icon } from "@/components/ui/icon";
import { Link } from "@/i18n/routing";
import type { CampaignSummary } from "@/lib/contract";
import { formatCount, formatDate, formatMs, formatTime } from "@/lib/format";

import { ProgressBar } from "./progress-bar";

/** Every campaign, newest first: its name and state, how far it has got, and what the farmers pressed. */
export async function CampaignList({ campaigns }: { campaigns: CampaignSummary[] }) {
  const t = await getTranslations("outbound");

  if (campaigns.length === 0) {
    return (
      <Card>
        <EmptyState>{t("empty")}</EmptyState>
      </Card>
    );
  }

  return (
    <Card className="px-2 py-1.5">
      <ul>
        {campaigns.map((campaign) => (
          <li key={campaign.id} className="border-b border-inset last:border-b-0">
            <Link
              href={`/outbound/${campaign.id}`}
              className="flex items-center gap-6 rounded-panel px-3.5 py-3.5 hover:bg-paper"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2.5">
                  <span className="truncate text-prose font-semibold leading-tight">
                    {campaign.name}
                  </span>
                  <CampaignChip status={campaign.status} />
                </div>
                <div className="mt-1 text-body text-muted">
                  {[
                    campaign.flowName
                      ? `${campaign.flowName}${campaign.flowVersion === null ? "" : ` · v${campaign.flowVersion}`}`
                      : null,
                    campaign.startedAt
                      ? t("startedAt", { time: formatTime(campaign.startedAt) })
                      : t("createdOn", { date: formatDate(campaign.createdAt) }),
                    t("callingHours", { from: campaign.windowStart, to: campaign.windowEnd }),
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
              </div>
              <div className="w-56 shrink-0">
                <ProgressBar counts={campaign.counts} label={t("progressLabel", { done: campaign.counts.done, total: campaign.counts.total })} />
                <div className="mt-1.5 text-label text-muted">
                  {t("doneOf", { done: formatCount(campaign.counts.done), total: formatCount(campaign.counts.total) })}
                </div>
              </div>
              <div className="w-64 shrink-0 text-label text-muted">
                {t("listCounts", {
                  pressed1: campaign.counts.pressed1,
                  pressed2: campaign.counts.pressed2,
                  noAnswer: campaign.counts.noAnswer,
                })}
                <div className="mt-0.5">
                  {t("firstReplyMedian")}{" "}
                  <span className="font-mono text-ink">{formatMs(campaign.firstReplyP50Ms)}</span>
                </div>
              </div>
              <Icon name="chevronRight" size={14} className="text-faint" />
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}
