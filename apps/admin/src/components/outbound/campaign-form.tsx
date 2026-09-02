"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { createCampaignAction, type CreateCampaignState } from "@/app/actions/campaigns";
import { Button, buttonClass } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/field";
import { Icon } from "@/components/ui/icon";
import { Segmented } from "@/components/ui/segmented";
import { countContacts, csvToContactLines } from "@/lib/contacts";

import { GateResult } from "./gate-result";

const CONCURRENCY = [1, 5, 10, 20, 30].map((value) => ({ value, label: String(value) }));

/**
 * A campaign from a pasted list. A CSV is read in the browser and turned
 * into the same `name, number` lines, appended to the textarea, so the
 * operator sees exactly what will be sent before it goes. The consent box is
 * a legal attestation, so it is a real required checkbox and not a default.
 */
export function CampaignForm({ flows }: { flows: { id: string; name: string; version: number }[] }) {
  const t = useTranslations("newCampaign");
  const [state, action, pending] = useActionState<CreateCampaignState, FormData>(
    createCampaignAction,
    { status: "idle" },
  );
  const [numbers, setNumbers] = useState("");
  const [concurrency, setConcurrency] = useState(10);
  const count = countContacts(numbers);

  if (state.status === "created") {
    return <GateResult result={state.result} />;
  }

  const appendCsv = async (file: File) => {
    const lines = csvToContactLines(await file.text());
    setNumbers((current) => [current.trimEnd(), ...lines].filter(Boolean).join("\n"));
  };

  return (
    <form action={action} className="flex flex-col gap-4.5">
      <Field label={t("name")}>
        <input name="name" required maxLength={120} lang="hi" className={inputClass} placeholder={t("namePlaceholder")} />
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
      <div>
        <label className={buttonClass("secondary", "sm", "cursor-pointer")}>
          <Icon name="upload" />
          <span>{t("uploadCsv")}</span>
          <input
            type="file"
            accept=".csv,text/csv,text/plain"
            className="sr-only"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void appendCsv(file);
              event.target.value = "";
            }}
          />
        </label>
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
