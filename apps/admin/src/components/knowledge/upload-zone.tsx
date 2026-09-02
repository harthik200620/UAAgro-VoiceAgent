"use client";

import { useTranslations } from "next-intl";
import { useActionState, type RefObject } from "react";

import { addDocument, type UploadState } from "@/app/actions/knowledge";
import { Button } from "@/components/ui/button";
import { Icon } from "@/components/ui/icon";
import { inputClass } from "@/components/ui/field";

/**
 * The dashed area under the documents table. Choosing a file submits at
 * once; a website address goes with the small form beside it. Both land in
 * the same Server Action, which decides between multipart and JSON.
 *
 * "Used on" defaults to both kinds of call, from either side of the panel.
 * Defaulting to the side the operator happens to be standing on would make
 * the product catalogue helpline-only because that is where it was uploaded,
 * and the failure would surface as an offer call that cannot answer a
 * question about a product.
 */
const selectClass =
  "rounded-btn border border-line bg-surface px-2.5 py-1.5 text-body text-ink";

export function UploadZone({ fileInput }: { fileInput: RefObject<HTMLInputElement | null> }) {
  const t = useTranslations("knowledge.upload");
  const [state, action, pending] = useActionState<UploadState, FormData>(addDocument, {
    status: "idle",
  });

  return (
    <form action={action} className="mx-3.5 mb-2.5 mt-3.5 flex flex-col gap-3">
      <label className="flex cursor-pointer flex-col items-center gap-1.5 rounded-panel border-[1.5px] border-dashed border-line px-5 py-5.5 text-center text-body text-muted hover:border-ink">
        <Icon name="upload" size={20} />
        <span>{pending ? t("sending") : t("drop")}</span>
        <span className="text-label text-faint">{t("dropHint")}</span>
        <input
          ref={fileInput}
          type="file"
          name="file"
          accept=".pdf,.docx,.csv,.txt,.md,application/pdf,text/csv,text/plain,text/markdown"
          className="sr-only"
          disabled={pending}
          onChange={(event) => {
            if (event.target.files?.length) event.target.form?.requestSubmit();
          }}
        />
      </label>

      <label className="flex items-center gap-2.5 text-body text-muted">
        {t("usedOn")}
        <select name="scope" defaultValue="both" disabled={pending} className={selectClass}>
          <option value="both">{t("scopeBoth")}</option>
          <option value="inbound">{t("scopeInbound")}</option>
          <option value="outbound">{t("scopeOutbound")}</option>
        </select>
      </label>

      <div className="flex items-end gap-2.5">
        <label className="flex flex-1 flex-col gap-[5px]">
          <span className="text-label text-muted">{t("url")}</span>
          <input type="url" name="url" placeholder="https://" className={inputClass} disabled={pending} />
        </label>
        <label className="flex w-56 flex-col gap-[5px]">
          <span className="text-label text-muted">{t("title")}</span>
          <input type="text" name="title" maxLength={200} className={inputClass} disabled={pending} />
        </label>
        <Button type="submit" variant="primary" icon="plus" disabled={pending} className="mb-px py-[9px]">
          {t("addUrl")}
        </Button>
      </div>

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
    </form>
  );
}
