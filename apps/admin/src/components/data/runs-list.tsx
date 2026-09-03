"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { loadSourceRuns } from "@/app/actions/data";
import { Chip } from "@/components/ui/chip";
import { Table, Td, Th } from "@/components/ui/table";
import type { SyncRun } from "@/lib/contract";
import { formatCount, formatDateTime, formatTime } from "@/lib/format";
import { syncTone } from "@/lib/tones";

const POLL_MS = 10_000;

/** The last syncs of one source, newest first, re-read every ten seconds while one is still running. */
export function RunsList({ sourceId }: { sourceId: string }) {
  const t = useTranslations("data.runs");
  const [runs, setRuns] = useState<SyncRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = runs?.some((run) => run.status === "running") ?? true;

  useEffect(() => {
    let cancelled = false;
    const read = () =>
      loadSourceRuns(sourceId).then((result) => {
        if (cancelled) return;
        if (result.ok) setRuns(result.value);
        else setError(result.message);
      });
    void read();
    const timer = running ? window.setInterval(read, POLL_MS) : undefined;
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [sourceId, running]);

  if (error) {
    return (
      <p role="alert" className="border-t border-inset pt-3 text-ui text-red-text">
        {error}
      </p>
    );
  }
  if (runs === null) return <p className="border-t border-inset pt-3 text-small text-muted">{t("loading")}</p>;
  if (runs.length === 0) return <p className="border-t border-inset pt-3 text-small text-muted">{t("empty")}</p>;

  return (
    <div className="border-t border-inset pt-3">
      <Table>
        <thead>
          <tr>
            <Th>{t("started")}</Th>
            <Th>{t("finished")}</Th>
            <Th>{t("status")}</Th>
            <Th align="right">{t("stores")}</Th>
            <Th align="right">{t("products")}</Th>
            <Th align="right">{t("stock")}</Th>
            <Th>{t("error")}</Th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id}>
              <Td>
                <span className="font-mono text-small">{formatDateTime(run.startedAt)}</span>
              </Td>
              <Td>
                <span className="font-mono text-small">{run.finishedAt ? formatTime(run.finishedAt) : "—"}</span>
              </Td>
              <Td>
                <Chip tone={syncTone(run.status)} pulse={run.status === "running"}>
                  {t(`states.${run.status}`)}
                </Chip>
              </Td>
              <Td align="right">
                <span className="font-mono text-small">{formatCount(run.stores)}</span>
              </Td>
              <Td align="right">
                <span className="font-mono text-small">{formatCount(run.products)}</span>
              </Td>
              <Td align="right">
                <span className="font-mono text-small">{formatCount(run.stock)}</span>
              </Td>
              <Td>
                <span className="text-small text-red-text">{run.error ?? ""}</span>
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}
