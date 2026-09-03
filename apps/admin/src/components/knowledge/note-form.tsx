"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { addNote, type UploadState } from "@/app/actions/knowledge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, inputClass } from "@/components/ui/field";
import { Icon } from "@/components/ui/icon";

/**
 * "Add a note": a title and a paragraph typed straight in, for the answer a
 * farmer asked for this morning and no document has yet. It is indexed like
 * a file and shows in the list as a text document within a minute.
 */
export function NoteForm() {
  const t = useTranslations("knowledge.note");
  // A queued note clears the form: the fields are uncontrolled and reset with the key.
  const [formKey, setFormKey] = useState(0);
  const [state, action, pending] = useActionState<UploadState, FormData>(
    async (previous, formData) => {
      const next = await addNote(previous, formData);
      if (next.status === "queued") setFormKey((key) => key + 1);
      return next;
    },
    { status: "idle" },
  );

  return (
    <Card className="flex flex-col gap-3 px-5 pb-5 pt-4.5">
      <div className="flex items-center gap-2 font-semibold">
        <Icon name="file" />
        {t("title")}
      </div>
      <p className="text-small text-muted">{t("explainer")}</p>
      <form key={formKey} action={action} className="flex flex-col gap-3">
        <Field label={t("noteTitle")}>
          <input name="title" required maxLength={200} className={inputClass} placeholder={t("titlePlaceholder")} />
        </Field>
        <Field label={t("text")}>
          <textarea
            lang="hi"
            name="text"
            required
            rows={5}
            maxLength={20_000}
            className={`${inputClass} leading-relaxed`}
            placeholder={t("textPlaceholder")}
          />
        </Field>
        <label className="flex items-center gap-2.5 text-body text-muted">
          {t("usedOn")}
          <select
            name="scope"
            defaultValue="both"
            className="rounded-btn border border-line bg-surface px-2.5 py-1.5 text-body text-ink"
          >
            <option value="both">{t("scopeBoth")}</option>
            <option value="inbound">{t("scopeInbound")}</option>
            <option value="outbound">{t("scopeOutbound")}</option>
          </select>
        </label>
        <div className="flex justify-end">
          <Button type="submit" variant="primary" icon="plus" disabled={pending}>
            {pending ? t("adding") : t("add")}
          </Button>
        </div>
      </form>
      {state.status === "queued" ? (
        <p role="status" className="text-ui text-green-text">
          {t("queued", { title: state.title })}
        </p>
      ) : null}
      {state.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {state.message}
        </p>
      ) : null}
    </Card>
  );
}
