"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { createScriptFrom } from "@/app/actions/flows";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, inputClass } from "@/components/ui/field";
import { useRouter } from "@/i18n/routing";

/**
 * "New": a fresh script under a new name, started from an existing one so
 * the legal opening and the opt-out line come along. With nothing to copy
 * from yet the button explains itself instead of failing.
 */
export function NewScriptButton({ sourceId }: { sourceId: string | null }) {
  const t = useTranslations("flows.newScript");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  if (!sourceId) {
    return (
      <span title={t("nothingToCopy")}>
        <Button icon="plus" disabled>
          {t("button")}
        </Button>
      </span>
    );
  }

  return (
    <>
      <Button icon="plus" onClick={() => setOpen(true)}>
        {t("button")}
      </Button>
      <Dialog open={open} onClose={() => setOpen(false)} title={t("title")}>
        <p className="text-ui text-muted">{t("explainer")}</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            startTransition(async () => {
              setError(null);
              const result = await createScriptFrom(sourceId, name);
              if (result.ok) {
                setOpen(false);
                router.push(`/flows/${result.value.id}`);
                router.refresh();
              } else setError(result.message);
            });
          }}
          className="flex flex-col gap-3.5"
        >
          <Field label={t("name")}>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              maxLength={120}
              autoFocus
              className={inputClass}
              placeholder={t("namePlaceholder")}
            />
          </Field>
          {error ? (
            <p role="alert" className="text-ui text-red-text">
              {error}
            </p>
          ) : null}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              {t("cancel")}
            </Button>
            <Button type="submit" variant="primary" icon="plus" disabled={pending || !name.trim()}>
              {t("create")}
            </Button>
          </div>
        </form>
      </Dialog>
    </>
  );
}
