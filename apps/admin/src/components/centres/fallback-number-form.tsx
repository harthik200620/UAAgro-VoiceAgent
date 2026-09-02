"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { saveFallbackNumber, type FallbackState } from "@/app/actions/centres";
import { Button } from "@/components/ui/button";
import { inputClass } from "@/components/ui/field";

/** The number a hand-over rings when no centre manager answers. Shown in mono; "Change" opens the field. */
export function FallbackNumberForm({ current, canEdit }: { current: string | null; canEdit: boolean }) {
  const t = useTranslations("centres.handover");
  const [editing, setEditing] = useState(false);
  const [state, action, pending] = useActionState<FallbackState, FormData>(
    async (previous, formData) => {
      const next = await saveFallbackNumber(previous, formData);
      if (next.status === "saved") setEditing(false);
      return next;
    },
    { status: "idle" },
  );
  const number = state.status === "saved" ? state.rules.fallbackNumber : current;

  if (!editing) {
    return (
      <span className="inline-flex items-center gap-2 whitespace-nowrap">
        {t("fallback")}{" "}
        <span className="font-mono text-small text-ink">{number ?? "—"}</span>
        {canEdit ? (
          <Button variant="ghost" onClick={() => setEditing(true)}>
            {t("change")}
          </Button>
        ) : null}
        {state.status === "error" ? (
          <span role="alert" className="text-red-text">
            {state.message}
          </span>
        ) : null}
      </span>
    );
  }

  return (
    <form action={action} className="inline-flex items-center gap-2">
      <label className="inline-flex items-center gap-2">
        <span className="whitespace-nowrap">{t("fallback")}</span>
        <input
          name="fallbackNumber"
          type="tel"
          required
          defaultValue={number ?? ""}
          autoFocus
          className={`${inputClass} w-48 font-mono`}
        />
      </label>
      <Button type="submit" variant="primary" disabled={pending}>
        {t("save")}
      </Button>
      <Button variant="ghost" onClick={() => setEditing(false)}>
        {t("cancel")}
      </Button>
    </form>
  );
}
