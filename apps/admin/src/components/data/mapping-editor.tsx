"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState, useTransition } from "react";

import { loadSourceColumns, saveSourceMapping, testSavedSource } from "@/app/actions/data";
import { Button } from "@/components/ui/button";
import { inputClass } from "@/components/ui/field";
import { Table, Td, Th } from "@/components/ui/table";
import type { DataSource, SourceMapping, SourceTable, TableColumns } from "@/lib/contract";
import { EMPTY_MAPPING, SOURCE_FIELDS, SOURCE_TABLES, validateMapping, withColumn } from "@/lib/data-sources";
import { formatCount } from "@/lib/format";

/**
 * Their tables to our fields. For each of stores, products and stock the
 * operator picks one of their tables, then a column for every one of our
 * fields; the first rows of the table sit underneath so "which column is
 * the price" is answered by looking rather than guessing. A required field
 * left unmapped keeps Save disabled and is named.
 */
export function MappingEditor({
  source,
  onSaved,
  onClose,
}: {
  source: DataSource;
  onSaved: (source: DataSource) => void;
  onClose: () => void;
}) {
  const t = useTranslations("data.mapping");
  const [mapping, setMapping] = useState<SourceMapping>(source.mapping ?? EMPTY_MAPPING);
  const [theirTables, setTheirTables] = useState<{ name: string; rows: number | null }[] | null>(null);
  const [columns, setColumns] = useState<Partial<Record<SourceTable, TableColumns | null>>>({});
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const problems = validateMapping(mapping);
  const complete = SOURCE_TABLES.every((table) => problems[table].length === 0);

  // The list of their tables comes from a connection test; without one the
  // table name can still be typed.
  useEffect(() => {
    let cancelled = false;
    testSavedSource(source.id).then((result) => {
      if (cancelled) return;
      if (result.ok && result.value.ok) setTheirTables(result.value.tables);
      else setTheirTables([]);
    });
    return () => {
      cancelled = true;
    };
  }, [source.id]);

  const fetchColumns = (table: SourceTable, theirTable: string) => {
    setColumns((current) => ({ ...current, [table]: null }));
    loadSourceColumns(source.id, theirTable).then((result) => {
      setColumns((current) => ({ ...current, [table]: result.ok ? result.value : null }));
      if (!result.ok) setError(result.message);
    });
  };

  const enable = (table: SourceTable, on: boolean) =>
    setMapping((current) => ({ ...current, [table]: on ? { table: "", columns: {} } : null }));

  const chooseTable = (table: SourceTable, theirTable: string) => {
    setMapping((current) => ({ ...current, [table]: { table: theirTable, columns: {} } }));
    if (theirTable) fetchColumns(table, theirTable);
  };

  const save = () =>
    startTransition(async () => {
      setError(null);
      const result = await saveSourceMapping(source.id, mapping);
      if (result.ok) onSaved(result.value);
      else setError(result.message);
    });

  return (
    <div className="flex flex-col gap-4 border-t border-inset pt-4">
      <div>
        <div className="font-semibold">{t("title")}</div>
        <p className="text-small text-muted">{t("explainer")}</p>
      </div>

      {SOURCE_TABLES.map((table) => {
        const map = mapping[table];
        const loaded = columns[table];
        return (
          <div key={table} className="flex flex-col gap-3 rounded-panel border border-line bg-paper px-4 py-3.5">
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-2.5 font-semibold">
                <input type="checkbox" checked={map !== null} onChange={(event) => enable(table, event.target.checked)} className="h-4 w-4" />
                {t("syncTable", { table: t(`tables.${table}`) })}
              </label>
              {map ? (
                <label className="flex items-center gap-2 text-small text-muted">
                  {t("table")}
                  {theirTables && theirTables.length > 0 ? (
                    <select value={map.table} onChange={(event) => chooseTable(table, event.target.value)} className={`${inputClass} w-auto font-mono`}>
                      <option value="">—</option>
                      {theirTables.map((theirs) => (
                        <option key={theirs.name} value={theirs.name}>
                          {theirs.rows === null ? theirs.name : `${theirs.name} (${formatCount(theirs.rows)})`}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={map.table}
                      placeholder={t("typeTable")}
                      onChange={(event) =>
                        setMapping((current) => ({ ...current, [table]: { table: event.target.value, columns: map.columns } }))
                      }
                      onBlur={() => map.table && fetchColumns(table, map.table)}
                      className={`${inputClass} w-56 font-mono`}
                    />
                  )}
                </label>
              ) : null}
              {map && problems[table].length > 0 ? (
                <span className="text-small text-amber-text">
                  {t("missing", { fields: problems[table].join(", ") })}
                </span>
              ) : null}
            </div>

            {map && map.table ? (
              <>
                <div className="grid grid-cols-2 gap-x-6 gap-y-2 lg:grid-cols-3">
                  {SOURCE_FIELDS[table].map((field) => (
                    <label key={field.name} className="flex items-center justify-between gap-3 text-small">
                      <span className="font-mono">
                        {field.name}
                        {field.required ? <span className="text-red-text"> *</span> : null}
                      </span>
                      {loaded ? (
                        <select
                          value={map.columns[field.name] ?? ""}
                          onChange={(event) =>
                            setMapping((current) => ({ ...current, [table]: withColumn(map, field.name, event.target.value) }))
                          }
                          className={`${inputClass} w-48 font-mono`}
                        >
                          <option value="">—</option>
                          {loaded.columns.map((column) => (
                            <option key={column.name} value={column.name}>
                              {column.name} · {column.type}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          value={map.columns[field.name] ?? ""}
                          placeholder={loaded === null ? t("loadingColumns") : t("column")}
                          onChange={(event) =>
                            setMapping((current) => ({ ...current, [table]: withColumn(map, field.name, event.target.value) }))
                          }
                          className={`${inputClass} w-48 font-mono`}
                        />
                      )}
                    </label>
                  ))}
                </div>
                {loaded && loaded.sample.length > 0 ? (
                  <div>
                    <div className="mb-1 text-label text-muted">{t("sample")}</div>
                    <Table className="text-small">
                      <thead>
                        <tr>
                          {loaded.columns.map((column) => (
                            <Th key={column.name}>
                              <span className="font-mono">{column.name}</span>
                            </Th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {loaded.sample.map((row, index) => (
                          <tr key={index}>
                            {loaded.columns.map((column) => (
                              <Td key={column.name} className="py-1.5">
                                <span className="font-mono text-label">{cell(row[column.name])}</span>
                              </Td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  </div>
                ) : loaded ? (
                  <p className="text-label text-faint">{t("noSample")}</p>
                ) : null}
              </>
            ) : null}
          </div>
        );
      })}

      {error ? (
        <p role="alert" className="text-ui text-red-text">
          {error}
        </p>
      ) : null}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          {t("close")}
        </Button>
        <Button variant="primary" icon="check" disabled={pending || !complete} onClick={save}>
          {pending ? t("saving") : t("save")}
        </Button>
      </div>
    </div>
  );
}

/** A sample value as text; objects and nulls are shown as what they are, never crash the row. */
function cell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
