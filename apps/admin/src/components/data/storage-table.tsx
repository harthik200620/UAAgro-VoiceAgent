import { getTranslations } from "next-intl/server";

import { Table, Td, Th } from "@/components/ui/table";
import type { StorageReport } from "@/lib/contract";
import { formatBytes, formatCount } from "@/lib/format";

/** "What is stored, and where": every table with its size and its index, plus the recordings bucket. */
export async function StorageTable({ storage }: { storage: StorageReport }) {
  const t = await getTranslations("data.storage");

  return (
    <Table>
      <thead>
        <tr>
          <Th />
          <Th>{t("columns.table")}</Th>
          <Th align="right">{t("columns.rows")}</Th>
          <Th align="right">{t("columns.size")}</Th>
          <Th>{t("columns.indexedBy")}</Th>
        </tr>
      </thead>
      <tbody>
        {storage.tables.map((table) => (
          <tr key={table.table}>
            <Td>
              <div className="font-medium">{table.label}</div>
              {table.note ? <div className="text-label text-muted">{table.note}</div> : null}
            </Td>
            <Td>
              <span className="font-mono text-small">{table.table}</span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatCount(table.rows)}</span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small text-muted">{formatBytes(table.bytes)}</span>
            </Td>
            <Td>
              <span className="font-mono text-label text-muted">{table.indexedBy}</span>
            </Td>
          </tr>
        ))}
        <tr>
          <Td>
            <div className="font-medium">{t("recordings")}</div>
            <div className="text-label text-muted">
              {storage.recordings.region} · {t("keptDays", { days: storage.recordings.retentionDays })}
            </div>
          </Td>
          <Td>
            <span className="font-mono text-small">S3 · {storage.recordings.bucket}</span>
          </Td>
          <Td align="right">
            <span className="font-mono text-small text-muted">—</span>
          </Td>
          <Td align="right">
            <span className="font-mono text-small text-muted">—</span>
          </Td>
          <Td>
            <span className="font-mono text-label text-muted">{t("byCallRef")}</span>
          </Td>
        </tr>
      </tbody>
    </Table>
  );
}
