"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { dialNowAction, type DialState } from "@/app/actions/campaigns";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/field";
import { Icon } from "@/components/ui/icon";
import { Link } from "@/i18n/routing";
import { countContacts } from "@/lib/contacts";

import { GateResult } from "./gate-result";

type PublishedFlow = { id: string; name: string; version: number };

/**
 * "Call now": paste the numbers, pick the script, attest consent, and the
 * phones ring. When the calls started the operator is taken to the board
 * to watch them; when the gate or the four-eyes rule held them back, the
 * reasons appear here in place of the form, with a way to start over.
 */
export function QuickDial({ flows }: { flows: PublishedFlow[] }) {
  // The form's action state cannot be reset in place; a new key mounts a fresh one.
  const [attempt, setAttempt] = useState(0);
  return <QuickDialForm key={attempt} flows={flows} onReset={() => setAttempt((value) => value + 1)} />;
}

function QuickDialForm({ flows, onReset }: { flows: PublishedFlow[]; onReset: () => void }) {
  const t = useTranslations("quickDial");
  const [state, action, pending] = useActionState<DialState, FormData>(dialNowAction, { status: "idle" });
  const [numbers, setNumbers] = useState("");
  const count = countContacts(numbers);

  if (state.status === "waiting") {
    const { result } = state;
    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 font-semibold">
            <Icon name="phone" />
            {t("notStarted")}
          </div>
          <Button variant="ghost" icon="plus" onClick={onReset}>
            {t("another")}
          </Button>
        </div>
        <GateResult
          result={result}
          blockedBy={result.blockedBy.length > 0 ? result.blockedBy : result.campaign.blockedBy}
        />
      </div>
    );
  }

  return (
    <form action={action} className="flex flex-col gap-4">
      <div>
        <div className="flex items-center gap-2 font-semibold">
          <Icon name="phone" />
          {t("title")}
        </div>
        <p className="mt-1 text-body text-muted">{t("explainer")}</p>
      </div>

      <div className="flex flex-wrap items-start gap-5">
        <Field label={t("numbers")} hint={t("numbersHint", { count })} className="min-w-[320px] flex-1">
          <textarea
            name="numbers"
            required
            rows={6}
            value={numbers}
            onChange={(event) => setNumbers(event.target.value)}
            placeholder={t("numbersPlaceholder")}
            className={`${inputClass} font-mono text-body leading-relaxed`}
          />
        </Field>

        <div className="flex w-full flex-col gap-3.5 lg:w-[340px]">
          {flows.length > 0 ? (
            <Field label={t("flow")}>
              <select name="flowId" required className={inputClass} defaultValue={flows[0]?.id ?? ""}>
                {flows.map((flow) => (
                  <option key={flow.id} value={flow.id}>
                    {flow.name} · v{flow.version}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <p className="rounded-panel border border-line bg-paper px-3.5 py-3 text-body text-muted">
              {t("noFlows")}{" "}
              <Link href="/flows" className="underline underline-offset-2">
                {t("goToFlows")}
              </Link>
            </p>
          )}
          <Field label={t("name")} hint={t("nameHint")}>
            <input name="name" maxLength={120} className={inputClass} placeholder={t("namePlaceholder")} />
          </Field>
          <label className="flex items-start gap-2.5 rounded-panel border border-line bg-paper px-3.5 py-3 text-ui">
            <input type="checkbox" name="consentAttested" required className="mt-1 h-4 w-4 shrink-0" />
            <span>{t("consent")}</span>
          </label>
        </div>
      </div>

      {state.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {state.message}
        </p>
      ) : null}

      <div className="flex items-center justify-end gap-3">
        <span className="text-small text-muted">{t("thenLive")}</span>
        <Button
          type="submit"
          variant="primary"
          size="md"
          icon="phone"
          disabled={pending || flows.length === 0 || count === 0}
        >
          {pending ? t("calling") : t("callNow")}
        </Button>
      </div>
    </form>
  );
}
