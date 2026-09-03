import { useTranslations } from "next-intl";

import { DirectionLabel } from "@/components/status/direction-label";
import { Last4 } from "@/components/status/last4";
import { OutcomeChip } from "@/components/status/outcome-chip";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Icon } from "@/components/ui/icon";
import { Table, Td, Th } from "@/components/ui/table";
import { Link } from "@/i18n/routing";
import type { OverviewRecentCall } from "@/lib/contract";
import { formatDuration, formatTime } from "@/lib/format";

/** The last calls, newest first, each with its one-line summary and a link to the transcript. */
export function RecentCalls({ rows }: { rows: OverviewRecentCall[] }) {
  const t = useTranslations("overview.recent");
  const columns = useTranslations("calls.columns");

  return (
    <Card className="min-w-0 flex-1 px-2 pb-1.5 pt-4">
      <div className="flex items-baseline justify-between px-3.5 pb-2">
        <span className="font-semibold">{t("title")}</span>
        <Link href="/calls" className="text-small text-muted hover:text-ink">
          {t("all")} →
        </Link>
      </div>
      {rows.length === 0 ? (
        <p className="px-3.5 pb-4 pt-2 text-ui text-muted">{t("empty")}</p>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{columns("time")}</Th>
              <Th>{columns("farmer")}</Th>
              <Th>{columns("direction")}</Th>
              <Th>{columns("outcome")}</Th>
              <Th>{columns("summary")}</Th>
              <Th align="right">{columns("length")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-paper">
                <Td>
                  <span className="font-mono text-small text-muted">{formatTime(row.startedAt)}</span>
                </Td>
                <Td>
                  <Link href={`/calls/${row.id}`} className="font-medium hover:underline">
                    {row.farmerName ?? t("unknownFarmer")}
                  </Link>{" "}
                  <Last4 value={row.callerLast4} className="text-label" />
                  {row.isTest ? (
                    <Chip tone="grey" className="ml-1.5">
                      {t("test")}
                    </Chip>
                  ) : null}
                </Td>
                <Td>
                  <DirectionLabel direction={row.direction} />
                </Td>
                <Td>
                  <OutcomeChip outcome={row.outcome} dtmf={row.dtmf} />
                </Td>
                <Td className="max-w-[320px]">
                  <span lang="hi" className="line-clamp-1 text-body text-muted">
                    {row.summaryHi ?? "—"}
                  </span>
                </Td>
                <Td align="right">
                  <span className="font-mono text-small">{formatDuration(row.durationSeconds)}</span>
                </Td>
                <Td align="right">
                  <Link href={`/calls/${row.id}`} aria-label={t("open")} className="inline-flex p-1">
                    <Icon name="chevronRight" size={14} className="text-faint" />
                  </Link>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
