"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { removeSource, syncSourceNow, testSavedSource } from "@/app/actions/data";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import type { ConnectionReport, DataSource, SourceTable } from "@/lib/contract";
import { SOURCE_TABLES, validateMapping } from "@/lib/data-sources";
import { formatCount, formatDateTime, formatMs } from "@/lib/format";
import { syncTone } from "@/lib/tones";

import { MappingEditor } from "./mapping-editor";
import { RunsList } from "./runs-list";
import { SourceForm } from "./source-form";

/**
 * The client's MySQL databases as the panel lists them: where each one is,
 * what is mapped, how the last sync went. Writing them -- adding, editing,
 * mapping, deleting -- is the super_admin's; an ops manager sees the list
 * and can run a sync.
 */
export function SourcesPanel({
  sources: initial,
  canWrite,
}: {
  sources: DataSource[];
  canWrite: boolean;
}) {
  const t = useTranslations("data.sources");
  const [sources, setSources] = useState(initial);
  const [editing, setEditing] = useState<"new" | string | null>(null);
  const [mappingFor, setMappingFor] = useState<string | null>(null);
  const [runsFor, setRunsFor] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, ConnectionReport>>({});
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const upsert = (source: DataSource) =>
    setSources((current) =>
      current.some((row) => row.id === source.id)
        ? current.map((row) => (row.id === source.id ? source : row))
        : [source, ...current],
    );

  const sync = (source: DataSource) =>
    startTransition(async () => {
      setError(null);
      const result = await syncSourceNow(source.id);
      if (result.ok) {
        upsert({ ...source, lastRun: result.value });
        setRunsFor(source.id);
      } else setError(result.message);
    });

  const test = (source: DataSource) =>
    startTransition(async () => {
      setError(null);
      const result = await testSavedSource(source.id);
      if (result.ok) setReports((current) => ({ ...current, [source.id]: result.value }));
      else setError(result.message);
    });

  const remove = (id: string) => {
    setDeletingId(null);
    startTransition(async () => {
      setError(null);
      const result = await removeSource(id);
      if (result.ok) setSources((current) => current.filter((row) => row.id !== id));
      else setError(result.message);
    });
  };

  return (
    <div className="flex flex-col gap-4">
      {error ? (
        <p role="alert" className="text-ui text-red-text">
          {error}
        </p>
      ) : null}

      {editing === "new" ? (
        <Card className="px-6 pb-5 pt-4.5">
          <SourceForm
            source={null}
            onSaved={(source) => {
              upsert(source);
              setEditing(null);
            }}
            onCancel={() => setEditing(null)}
          />
        </Card>
      ) : null}

      {sources.length === 0 && editing !== "new" ? (
        <Card>
          <EmptyState>{t("empty")}</EmptyState>
        </Card>
      ) : null}

      {sources.map((source) => {
        const problems = validateMapping(source.mapping);
        const report = reports[source.id];
        return (
          <Card key={source.id} className="flex flex-col gap-4 px-6 pb-5 pt-4.5">
            {editing === source.id ? (
              <SourceForm
                source={source}
                onSaved={(saved) => {
                  upsert(saved);
                  setEditing(null);
                }}
                onCancel={() => setEditing(null)}
              />
            ) : (
              <>
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2.5">
                      <span className="text-md font-semibold">{source.name}</span>
                      <Chip tone="grey">{t("kind")}</Chip>
                      <Chip tone={source.tls ? "green" : "grey"}>{source.tls ? t("tls") : t("noTls")}</Chip>
                      <Chip tone={source.hasPassword ? "green" : "red"}>
                        {source.hasPassword ? t("passwordSet") : t("noPassword")}
                      </Chip>
                    </div>
                    <div className="mt-1 font-mono text-small text-muted">
                      {source.user}@{source.host}:{source.port}/{source.database}
                    </div>
                    <div className="mt-1 text-small text-muted">
                      {t("scheduleLabel")} {t(`schedule.${source.schedule}`)}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button icon="refresh" disabled={pending} onClick={() => sync(source)}>
                      {t("sync")}
                    </Button>
                    <Button icon="clock" onClick={() => setRunsFor(runsFor === source.id ? null : source.id)}>
                      {t("runs")}
                    </Button>
                    {canWrite ? (
                      <>
                        <Button icon="check" disabled={pending} onClick={() => test(source)}>
                          {t("test")}
                        </Button>
                        <Button
                          icon="table"
                          onClick={() => setMappingFor(mappingFor === source.id ? null : source.id)}
                        >
                          {t("mapping")}
                        </Button>
                        <Button onClick={() => setEditing(source.id)}>{t("edit")}</Button>
                        <Button variant="danger" icon="trash" disabled={pending} onClick={() => setDeletingId(source.id)}>
                          {t("delete")}
                        </Button>
                      </>
                    ) : null}
                  </div>
                </div>

                <div className="flex flex-wrap gap-x-6 gap-y-2 border-t border-inset pt-3 text-small">
                  {SOURCE_TABLES.map((table) => (
                    <MappingState
                      key={table}
                      table={table}
                      configured={source.mapping[table] !== null}
                      missing={problems[table]}
                    />
                  ))}
                  <span className="inline-flex items-center gap-2">
                    <span className="text-muted">{t("lastRun")}</span>
                    {source.lastRun ? (
                      <>
                        <Chip tone={syncTone(source.lastRun.status)} pulse={source.lastRun.status === "running"}>
                          {t(`runStatus.${source.lastRun.status}`)}
                        </Chip>
                        <span className="text-muted">{formatDateTime(source.lastRun.startedAt)}</span>
                        <span className="font-mono text-label">
                          {t("runCounts", {
                            stores: formatCount(source.lastRun.stores),
                            products: formatCount(source.lastRun.products),
                            stock: formatCount(source.lastRun.stock),
                          })}
                        </span>
                        {source.lastRun.error ? <span className="text-red-text">{source.lastRun.error}</span> : null}
                      </>
                    ) : (
                      <span className="text-muted">{t("noRuns")}</span>
                    )}
                  </span>
                </div>

                {report ? <ReportLine report={report} /> : null}
              </>
            )}

            {mappingFor === source.id ? (
              <MappingEditor
                source={source}
                onSaved={(saved) => {
                  upsert(saved);
                  setMappingFor(null);
                }}
                onClose={() => setMappingFor(null)}
              />
            ) : null}

            {runsFor === source.id ? <RunsList sourceId={source.id} /> : null}
          </Card>
        );
      })}

      {canWrite && editing !== "new" ? (
        <div>
          <Button variant="primary" icon="plus" onClick={() => setEditing("new")}>
            {t("add")}
          </Button>
        </div>
      ) : null}

      <Dialog open={deletingId !== null} onClose={() => setDeletingId(null)} title={t("deleteTitle")}>
        <p className="text-ui text-muted">{t("deleteQuestion")}</p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setDeletingId(null)}>
            {t("cancel")}
          </Button>
          <Button variant="danger" icon="trash" disabled={pending} onClick={() => deletingId && remove(deletingId)}>
            {t("confirmDelete")}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}

/** One table's mapping in a word: not synced, complete, or which required fields are still unmapped. */
function MappingState({
  table,
  configured,
  missing,
}: {
  table: SourceTable;
  configured: boolean;
  missing: string[];
}) {
  const t = useTranslations("data.sources");
  const tables = useTranslations("data.mapping.tables");
  return (
    <span className="inline-flex items-center gap-2">
      <span className="text-muted">{tables(table)}</span>
      {!configured ? (
        <Chip tone="grey">{t("notSynced")}</Chip>
      ) : missing.length > 0 ? (
        <Chip tone="amber">{t("incomplete", { count: missing.length })}</Chip>
      ) : (
        <Chip tone="green">{t("mapped")}</Chip>
      )}
    </span>
  );
}

/** What a test found: reachable and how fast, with the tables it could see, or the error. */
export function ReportLine({ report }: { report: ConnectionReport }) {
  const t = useTranslations("data.sources.form");
  return (
    <div role="status" className="flex flex-wrap items-center gap-2 border-t border-inset pt-3 text-small">
      <Chip tone={report.ok ? "green" : "red"}>{report.ok ? t("reachable") : t("unreachable")}</Chip>
      {report.ok ? (
        <>
          <span className="font-mono text-muted">
            {[formatMs(report.latencyMs), report.serverVersion].filter(Boolean).join(" · ")}
          </span>
          <span className="text-muted">
            {t("tables", { count: report.tables.length })}
            {report.tables.length > 0
              ? `: ${report.tables
                  .slice(0, 12)
                  .map((table) => (table.rows === null ? table.name : `${table.name} (${formatCount(table.rows)})`))
                  .join(", ")}${report.tables.length > 12 ? " …" : ""}`
              : ""}
          </span>
        </>
      ) : (
        <span className="text-red-text">{report.error ?? t("unreachable")}</span>
      )}
    </div>
  );
}
