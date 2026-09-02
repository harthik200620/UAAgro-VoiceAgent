import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { languageName } from "@/components/status/language-name";
import { OutcomeChip } from "@/components/status/outcome-chip";
import { Card } from "@/components/ui/card";
import { Link } from "@/i18n/routing";
import type { CallDetail } from "@/lib/contract";
import { formatBytes, formatDate, formatDuration } from "@/lib/format";

/** The facts of a call in one column: outcome, campaign, flow, centre, language, follow-ups, recording. */
export async function DetailsCard({ call }: { call: CallDetail }) {
  const t = await getTranslations("call.details");
  const languages = await getTranslations("languages");

  const rows: { label: string; value: ReactNode }[] = [
    { label: t("outcome"), value: <OutcomeChip outcome={call.outcome} /> },
    {
      label: t("campaign"),
      value: call.campaignId ? (
        <Link href={`/outbound/${call.campaignId}`} lang="hi" className="hover:underline">
          {call.campaignName ?? call.campaignId}
        </Link>
      ) : (
        "—"
      ),
    },
    {
      label: t("flow"),
      value: call.flowName ? `${call.flowName}${call.flowVersion === null ? "" : ` · v${call.flowVersion}`}` : "—",
    },
    {
      label: t("centre"),
      value: call.centreName
        ? `${call.centreName}${call.centreCode ? ` (${call.centreCode})` : ""}`
        : (call.centreCode ?? "—"),
    },
    { label: t("language"), value: languageName(call.language, languages) },
    { label: t("followUps"), value: call.followUps.length > 0 ? call.followUps.join(" · ") : "—" },
    {
      label: t("recording"),
      value: call.recording.available ? (
        <span className="font-mono text-label">
          {[
            formatDuration(call.recording.durationSeconds),
            formatBytes(call.recording.bytes),
            call.recording.retainedUntil ? t("keptUntil", { date: formatDate(call.recording.retainedUntil) }) : null,
          ]
            .filter(Boolean)
            .join(" · ")}
        </span>
      ) : (
        t("noRecording")
      ),
    },
  ];

  return (
    <Card className="flex flex-col gap-2.5 px-5.5 py-4">
      {rows.map((row) => (
        <div key={row.label} className="flex justify-between gap-3 text-body">
          <span className="text-muted">{row.label}</span>
          <span className="text-right">{row.value}</span>
        </div>
      ))}
    </Card>
  );
}
