import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import type { ConnectionInfo, StorageReport } from "@/lib/contract";
import { formatBytes, formatDate, formatMs } from "@/lib/format";

/**
 * "Also running": Redis, the recordings store and the backups. The health
 * chips need the connection report, which only a super_admin receives; an
 * ops manager still sees where the recordings and backups go.
 */
export async function AlsoRunningCard({
  storage,
  connection,
}: {
  storage: StorageReport;
  connection: ConnectionInfo | null;
}) {
  const t = await getTranslations("data.alsoRunning");
  const ok = (healthy: boolean) => (
    <Chip tone={healthy ? "green" : "red"}>{healthy ? t("ok") : t("down")}</Chip>
  );

  const rows: { label: string; value: ReactNode }[] = [];
  if (connection) {
    rows.push({
      label: t("redis"),
      value: (
        <span className="inline-flex items-center gap-2">
          Redis · <span className="font-mono text-small">{formatMs(connection.redis.latencyMs)}</span>{" "}
          {ok(connection.redis.ok)}
        </span>
      ),
    });
  }
  rows.push({
    label: t("recordings"),
    value: (
      <span className="inline-flex items-center gap-2">
        S3 · {storage.recordings.region}
        {connection ? ok(connection.storage.ok) : null}
      </span>
    ),
  });
  rows.push({
    label: t("backup"),
    value: (
      <span className="inline-flex items-center gap-2">
        {storage.backups.schedule}
        {storage.backups.lastAt
          ? ` · ${t("lastBackup", { date: formatDate(storage.backups.lastAt), size: formatBytes(storage.backups.lastBytes) })}`
          : ` · ${t("noBackupYet")}`}
      </span>
    ),
  });

  return (
    <Card className="flex flex-col gap-1.5 px-5 py-4.5">
      <div className="mb-1.5 font-semibold">{t("title")}</div>
      {rows.map((row) => (
        <div key={row.label} className="flex justify-between gap-3 border-b border-inset py-[7px] text-body">
          <span className="text-muted">{row.label}</span>
          <span className="text-right">{row.value}</span>
        </div>
      ))}
    </Card>
  );
}
