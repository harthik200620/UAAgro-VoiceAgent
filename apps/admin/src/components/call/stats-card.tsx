import { getTranslations } from "next-intl/server";

import { Card } from "@/components/ui/card";
import type { CallStats } from "@/lib/contract";
import { formatCount } from "@/lib/format";

/** The call in numbers: turns each way, replies served from cache, tools called, and the model that answered. */
export async function StatsCard({ stats }: { stats: CallStats }) {
  const t = await getTranslations("call.stats");
  const rows: { label: string; value: string }[] = [
    { label: t("turns"), value: t("turnsValue", { farmer: stats.farmerTurns, agent: stats.agentTurns }) },
    { label: t("cachedReplies"), value: formatCount(stats.cachedReplies) },
    { label: t("toolCalls"), value: formatCount(stats.toolCalls) },
    { label: t("model"), value: stats.llmModel ?? "—" },
  ];

  return (
    <Card className="flex flex-col gap-2.5 px-5.5 py-4">
      <div className="text-small font-medium text-muted">{t("title", { count: stats.turns })}</div>
      {rows.map((row) => (
        <div key={row.label} className="flex justify-between gap-3 text-body">
          <span className="text-muted">{row.label}</span>
          <span className="text-right font-mono text-small">{row.value}</span>
        </div>
      ))}
    </Card>
  );
}
