"use client";

import { useTranslations } from "next-intl";
import { useActionState } from "react";

import {
  saveSource,
  testSourceForm,
  type SourceFormState,
  type SourceTestState,
} from "@/app/actions/data";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/field";
import type { DataSource } from "@/lib/contract";

import { ReportLine } from "./sources-panel";

/**
 * A MySQL source, new or being changed. One form, two submit buttons: "Test
 * connection" reaches the database with exactly what the fields hold and
 * stores nothing; "Save" keeps it. The password field is write-only -- on
 * an existing source it is blank and blank means unchanged; the panel never
 * sees the stored one.
 */
export function SourceForm({
  source,
  onSaved,
  onCancel,
}: {
  source: DataSource | null;
  onSaved: (source: DataSource) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("data.sources.form");
  const schedules = useTranslations("data.sources.schedule");
  const [saveState, saveAction, saving] = useActionState<SourceFormState, FormData>(
    async (previous, formData) => {
      const next = await saveSource(previous, formData);
      if (next.status === "saved") onSaved(next.source);
      return next;
    },
    { status: "idle" },
  );
  const [testState, testAction, testing] = useActionState<SourceTestState, FormData>(testSourceForm, {
    status: "idle",
  });
  const busy = saving || testing;

  return (
    <form action={saveAction} className="flex flex-col gap-4">
      <div className="font-semibold">{source ? t("editTitle", { name: source.name }) : t("title")}</div>
      {source ? <input type="hidden" name="id" value={source.id} /> : null}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Field label={t("name")} className="col-span-2">
          <input name="name" required maxLength={120} defaultValue={source?.name ?? ""} className={inputClass} />
        </Field>
        <Field label={t("schedule")}>
          <select name="schedule" defaultValue={source?.schedule ?? "manual"} className={inputClass}>
            {(["manual", "hourly", "daily"] as const).map((schedule) => (
              <option key={schedule} value={schedule}>
                {schedules(schedule)}
              </option>
            ))}
          </select>
        </Field>
        <label className="flex items-center gap-2.5 self-end pb-2.5 text-body">
          <input type="checkbox" name="tls" defaultChecked={source?.tls ?? true} className="h-4 w-4" />
          {t("tls")}
        </label>

        <Field label={t("host")} className="col-span-2">
          <input name="host" required defaultValue={source?.host ?? ""} className={`${inputClass} font-mono`} />
        </Field>
        <Field label={t("port")}>
          <input
            name="port"
            type="number"
            min={1}
            max={65535}
            defaultValue={source?.port ?? 3306}
            className={`${inputClass} font-mono`}
          />
        </Field>
        <Field label={t("database")}>
          <input name="database" required defaultValue={source?.database ?? ""} className={`${inputClass} font-mono`} />
        </Field>

        <Field label={t("user")} className="col-span-2">
          <input name="user" required defaultValue={source?.user ?? ""} className={`${inputClass} font-mono`} />
        </Field>
        <Field label={t("password")} hint={source ? t("passwordKeep") : t("passwordHint")} className="col-span-2">
          <input
            name="password"
            type="password"
            autoComplete="new-password"
            required={source === null}
            placeholder={source?.hasPassword ? "••••••••" : ""}
            className={`${inputClass} font-mono`}
          />
        </Field>
      </div>

      {testState.status === "done" ? <ReportLine report={testState.report} /> : null}
      {testState.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {testState.message}
        </p>
      ) : null}
      {saveState.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {saveState.message}
        </p>
      ) : null}

      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>
          {t("cancel")}
        </Button>
        <Button type="submit" formAction={testAction} icon="check" disabled={busy}>
          {testing ? t("testing") : t("test")}
        </Button>
        <Button type="submit" variant="primary" icon="check" disabled={busy}>
          {saving ? t("saving") : t("save")}
        </Button>
      </div>
    </form>
  );
}
