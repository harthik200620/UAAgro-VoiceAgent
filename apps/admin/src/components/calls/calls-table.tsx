import { getTranslations } from "next-intl/server";

import { DirectionLabel } from "@/components/status/direction-label";
import { Last4 } from "@/components/status/last4";
import { OutcomeChip } from "@/components/status/outcome-chip";
import { Chip } from "@/components/ui/chip";
import { EmptyState } from "@/components/ui/empty-state";
import { Icon } from "@/components/ui/icon";
import { Table, Td, Th } from "@/components/ui/table";
import { Link } from "@/i18n/routing";
import type { CallRow } from "@/lib/contract";
import { formatDate, formatDuration, formatMs, formatTime } from "@/lib/format";

/** Every call on the page: who, which way, how it ended, what it was about in a line, and whether there is a recording to hear. */
export async function CallsTable({ rows }: { rows: CallRow[] }) {
  const t = await getTranslations("calls");
  const columns = await getTranslations("calls.columns");

  if (rows.length === 0) return <EmptyState>{t("empty")}</EmptyState>;

  return (
    <Table>
      <thead>
        <tr>
          <Th>{columns("time")}</Th>
          <Th>{columns("direction")}</Th>
          <Th>{columns("farmer")}</Th>
          <Th>{columns("centre")}</Th>
          <Th>{columns("outcome")}</Th>
          <Th align="right">{columns("length")}</Th>
          <Th align="right">{columns("firstReply")}</Th>
          <Th>{columns("summary")}</Th>
          <Th />
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.id} className="hover:bg-paper">
            <Td>
              <span className="whitespace-nowrap font-mono text-small text-muted">
                {formatDate(row.startedAt)} {formatTime(row.startedAt)}
              </span>
            </Td>
            <Td>
              <DirectionLabel direction={row.direction} />
            </Td>
            <Td>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <Link href={`/calls/${row.id}`} className="font-medium hover:underline">
                  {row.farmerName ?? t("unknownFarmer")}
                </Link>
                <Last4 value={row.callerLast4} className="text-small" />
                {row.isTest ? <Chip tone="grey">{t("test")}</Chip> : null}
              </div>
            </Td>
            <Td>{row.centreCode ?? "—"}</Td>
            <Td>
              <span className="inline-flex items-center gap-1.5">
                <OutcomeChip outcome={row.outcome} dtmf={row.dtmf} />
                {row.transferred ? (
                  <span className="text-label text-muted">{t("transferred")}</span>
                ) : null}
              </span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatDuration(row.durationSeconds)}</span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatMs(row.firstReplyMs)}</span>
            </Td>
            <Td className="max-w-[360px]">
              <span lang="hi" className="line-clamp-2 text-body text-muted" title={row.summaryHi ?? undefined}>
                {row.summaryHi ?? "—"}
              </span>
            </Td>
            <Td align="right">
              <span className="inline-flex items-center gap-1">
                {row.recordingAvailable ? (
                  <Icon name="mic" size={14} className="text-muted" aria-label={t("hasRecording")} />
                ) : null}
                <Link href={`/calls/${row.id}`} aria-label={t("openCall")} className="inline-flex p-1">
                  <Icon name="chevronRight" size={14} className="text-faint" />
                </Link>
              </span>
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
