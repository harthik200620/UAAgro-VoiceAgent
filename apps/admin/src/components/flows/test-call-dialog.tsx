"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { testCall } from "@/app/actions/flows";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, inputClass } from "@/components/ui/field";

/**
 * "Test on my phone": a real call with this draft to a number the operator
 * types. When telephony is not configured the API says so with a remedy,
 * and that text is shown as it came.
 */
export function TestCallDialog({
  flowId,
  open,
  onClose,
}: {
  flowId: string;
  open: boolean;
  onClose: () => void;
}) {
  const t = useTranslations("flows.testCall");
  const [phone, setPhone] = useState("");
  const [outcome, setOutcome] = useState<{ ok: boolean; message: string } | null>(null);
  const [pending, startTransition] = useTransition();

  return (
    <Dialog open={open} onClose={onClose} title={t("title")}>
      <p className="text-ui text-muted">{t("explainer")}</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          startTransition(async () => {
            const result = await testCall(flowId, phone);
            setOutcome(
              result.ok ? { ok: true, message: t("calling", { sid: result.value.callSid }) } : { ok: false, message: result.message },
            );
          });
        }}
        className="flex flex-col gap-3.5"
      >
        <Field label={t("phone")}>
          <input
            type="tel"
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
            placeholder="+91"
            autoComplete="tel"
            required
            className={`${inputClass} font-mono`}
          />
        </Field>
        {outcome ? (
          <p role={outcome.ok ? "status" : "alert"} className={outcome.ok ? "text-ui text-green-text" : "text-ui text-red-text"}>
            {outcome.message}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            {t("close")}
          </Button>
          <Button type="submit" variant="primary" icon="phone" disabled={pending || !phone.trim()}>
            {pending ? t("placing") : t("call")}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
