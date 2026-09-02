import { useTranslations } from "next-intl";

import { DirectionLabel } from "@/components/status/direction-label";
import { Last4 } from "@/components/status/last4";
import { OutcomeChip } from "@/components/status/outcome-chip";
import { Icon } from "@/components/ui/icon";
import { Table, Td, Th } from "@/components/ui/table";
import { Link } from "@/i18n/routing";
import type { RecentCall } from "@/lib/contract";
import { formatDuration, formatMs, formatTime } from "@/lib/format";

/** "Finished today": the last calls of the day, newest first, each a link to its transcript. */
export function FinishedTable({ rows }: { rows: RecentCall[] }) {
  const t = useTranslations("live");
  const columns = useTranslations("calls.columns");

  if (rows.length === 0) {
    return <p className="px-3.5 pb-4 pt-2 text-ui text-muted">{t("noFinished")}</p>;
  }

  return (
    <Table>
      <thead>
        <tr>
          <Th>{columns("time")}</Th>
          <Th>{columns("farmer")}</Th>
          <Th>{columns("number")}</Th>
          <Th>{columns("direction")}</Th>
          <Th>{columns("centre")}</Th>
          <Th>{columns("outcome")}</Th>
          <Th align="right">{columns("length")}</Th>
          <Th align="right">{columns("firstReply")}</Th>
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
              </Link>
            </Td>
            <Td>
              <Last4 value={row.callerLast4} className="text-small" />
            </Td>
            <Td>
              <DirectionLabel direction={row.direction} />
            </Td>
            <Td>{row.centreCode ?? "—"}</Td>
            <Td>
              <OutcomeChip outcome={row.outcome} dtmf={row.dtmf} />
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatDuration(row.durationSeconds)}</span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatMs(row.firstReplyMs)}</span>
            </Td>
            <Td align="right">
              <Link href={`/calls/${row.id}`} aria-label={t("openCall")} className="inline-flex p-1">
                <Icon name="chevronRight" size={14} className="text-faint" />
              </Link>
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
