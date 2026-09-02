"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState, useTransition } from "react";

import {
  createCampaignAction,
  readContacts,
  type CreateCampaignState,
} from "@/app/actions/campaigns";
import { Button, buttonClass } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/field";
import { Icon } from "@/components/ui/icon";
import { Segmented } from "@/components/ui/segmented";
import { countContacts } from "@/lib/contacts";

import { GateResult } from "./gate-result";

const CONCURRENCY = [1, 5, 10, 20, 30].map((value) => ({ value, label: String(value) }));

/**
 * A campaign from a pasted list.
 *
 * A spreadsheet or CSV is read by the control plane and comes back as the
 * same `name, number` lines, appended to the textarea -- so whichever way the
 * numbers arrived, the operator sees exactly the list that will be sent, and
 * there is one parser rather than one per file format. The consent box is a
 * legal attestation, so it is a real required checkbox and not a default.
 */
export function CampaignForm({ flows }: { flows: { id: string; name: string; version: number }[] }) {
  const t = useTranslations("newCampaign");
  const [state, action, pending] = useActionState<CreateCampaignState, FormData>(
    createCampaignAction,
    { status: "idle" },
  );
  const [numbers, setNumbers] = useState("");
  const [concurrency, setConcurrency] = useState(10);
  const [reading, startReading] = useTransition();
  const [fileNote, setFileNote] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const count = countContacts(numbers);

  if (state.status === "created") {
    return <GateResult result={state.result} />;
  }

  const appendFile = (file: File) =>
    startReading(async () => {
      setFileNote(null);
      const body = new FormData();
      body.set("file", file, file.name);
      const outcome = await readContacts(body);
      if (!outcome.ok) {
        setFileNote({ tone: "bad", text: outcome.message });
        return;
      }
      const { lines, found, skipped } = outcome.value;
      if (found === 0) {
        setFileNote({ tone: "bad", text: t("readNothing") });
        return;
      }
      setNumbers((current) => [current.trimEnd(), ...lines].filter(Boolean).join("\n"));
      setFileNote({
        tone: "ok",
        text: skipped > 0
          ? t("readFileSkipped", { found, skipped })
          : t("readFile", { found, name: file.name }),
      });
    });

  return (
    <form action={action} className="flex flex-col gap-4.5">
      <Field label={t("name")}>
        <input name="name" required maxLength={120} className={inputClass} placeholder={t("namePlaceholder")} />
      </Field>

      <Field label={t("flow")} hint={t("flowHint")}>
        <select name="flowId" required className={inputClass} defaultValue={flows[0]?.id ?? ""}>
          {flows.map((flow) => (
            <option key={flow.id} value={flow.id}>
              {flow.name} · v{flow.version}
            </option>
          ))}
        </select>
      </Field>

      <Field
        label={t("numbers")}
        hint={t("numbersHint", { count })}
      >
        <textarea
          name="numbers"
          required
          rows={10}
          value={numbers}
          onChange={(event) => setNumbers(event.target.value)}
          placeholder={t("numbersPlaceholder")}
          className={`${inputClass} font-mono text-body leading-relaxed`}
        />
      </Field>
      <div className="flex items-center gap-3">
        <label className={buttonClass("secondary", "sm", "cursor-pointer")}>
          <Icon name="upload" />
          <span>{reading ? t("reading") : t("uploadFile")}</span>
          <input
            type="file"
            accept=".xlsx,.csv,.tsv,.txt,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv,text/plain"
            className="sr-only"
            disabled={reading}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) appendFile(file);
              // Cleared so choosing the same file twice fires again.
              event.target.value = "";
            }}
          />
        </label>
        {fileNote ? (
          <span
            role={fileNote.tone === "bad" ? "alert" : "status"}
            className={fileNote.tone === "bad" ? "text-ui text-red-text" : "text-ui text-muted"}
          >
            {fileNote.text}
          </span>
        ) : null}
      </div>

      <div className="flex items-center gap-3">
        <span className="text-label text-muted">{t("callsAtATime")}</span>
        <Segmented label={t("callsAtATime")} options={CONCURRENCY} value={concurrency} onChange={setConcurrency} />
        <input type="hidden" name="maxConcurrent" value={concurrency} />
      </div>

      <label className="flex items-start gap-2.5 rounded-panel border border-line bg-paper px-3.5 py-3 text-ui">
        <input type="checkbox" name="consentAttested" required className="mt-1 h-4 w-4 shrink-0" />
        <span>{t("consent")}</span>
      </label>

      <Field label={t("note")} hint={t("noteHint")}>
        <textarea name="consentNote" rows={2} maxLength={500} className={inputClass} />
      </Field>

      {state.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {state.message}
        </p>
      ) : null}

      <div className="flex justify-end">
        <Button type="submit" variant="primary" size="md" icon="check" disabled={pending}>
          {pending ? t("checking") : t("submit")}
        </Button>
      </div>
    </form>
  );
}
