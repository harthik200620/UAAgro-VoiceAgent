"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";
import { useRef, useState, useTransition } from "react";

import { removeDocument, setDocumentPublished } from "@/app/actions/knowledge";
import { languageName } from "@/components/status/language-name";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Icon } from "@/components/ui/icon";
import { Table, Td, Th } from "@/components/ui/table";
import type { KbDocumentRow } from "@/lib/contract";
import { dayOf, formatCount, formatDate, formatTime } from "@/lib/format";
import { humanize, ingestTone, messageKey } from "@/lib/tones";

import { UploadZone } from "./upload-zone";

/**
 * The documents the agent answers from. A row opens to show where it came
 * from and how far indexing got; switching off and deleting live there so a
 * misclick on the list cannot silence a document.
 */
export function DocumentsPanel({
  documents,
  canUpload,
  renderedAt,
}: {
  documents: KbDocumentRow[];
  canUpload: boolean;
  renderedAt: string;
}) {
  const t = useTranslations("knowledge");
  const languages = useTranslations("languages");
  const fileInput = useRef<HTMLInputElement>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const now = new Date(renderedAt);

  const live = documents.filter((doc) => doc.isPublished && doc.ingestStatus === "indexed").length;
  const pieces = documents.reduce((sum, doc) => sum + (doc.isPublished ? doc.chunks : 0), 0);

  const toggle = (doc: KbDocumentRow) =>
    startTransition(async () => {
      setError(null);
      const result = await setDocumentPublished(doc.id, !doc.isPublished);
      if (!result.ok) setError(result.message);
    });

  const remove = (id: string) =>
    startTransition(async () => {
      setError(null);
      setDeletingId(null);
      const result = await removeDocument(id);
      if (!result.ok) setError(result.message);
      else setOpenId(null);
    });

  const updated = (iso: string) => {
    switch (dayOf(iso, now)) {
      case "today":
        return t("todayAt", { time: formatTime(iso) });
      case "yesterday":
        return t("yesterday");
      case "earlier":
        return formatDate(iso);
    }
  };

  return (
    <Card className="px-2 pb-1.5 pt-4">
      <div className="flex items-center justify-between px-3.5 pb-2.5">
        <div>
          <span className="font-semibold">{t("documentsTitle")}</span>{" "}
          <span className="text-body text-muted">
            · {t("liveCount", { count: live })} · {t("pieces", { count: formatCount(pieces) })}
          </span>
        </div>
        {canUpload ? (
          <Button variant="primary" icon="upload" onClick={() => fileInput.current?.click()}>
            {t("addDocument")}
          </Button>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="px-3.5 pb-2 text-ui text-red-text">
          {error}
        </p>
      ) : null}

      {documents.length === 0 ? (
        <EmptyState>{t("empty")}</EmptyState>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>{t("columns.document")}</Th>
              <Th>{t("columns.type")}</Th>
              <Th>{t("columns.size")}</Th>
              <Th align="right">{t("columns.pieces")}</Th>
              <Th>{t("columns.status")}</Th>
              <Th>{t("columns.updated")}</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {documents.map((doc) => {
              const open = openId === doc.id;
              const statusKey = !doc.isPublished && doc.ingestStatus === "indexed" ? "off" : doc.ingestStatus;
              return (
                <DocumentRows
                  key={doc.id}
                  doc={doc}
                  open={open}
                  onToggleOpen={() => setOpenId(open ? null : doc.id)}
                  statusLabel={t(`status.${statusKey}`)}
                  typeLabel={[
                    t.has(`docTypes.${messageKey(doc.docType)}`) ? t(`docTypes.${messageKey(doc.docType)}`) : humanize(doc.docType),
                    languageName(doc.language, languages),
                  ]
                    .filter((part) => part !== "—")
                    .join(" · ")}
                  updatedLabel={updated(doc.updatedAt)}
                  canUpload={canUpload}
                  pending={pending}
                  onSwitch={() => toggle(doc)}
                  onDelete={() => setDeletingId(doc.id)}
                />
              );
            })}
          </tbody>
        </Table>
      )}

      {canUpload ? <UploadZone fileInput={fileInput} /> : null}

      <Dialog open={deletingId !== null} onClose={() => setDeletingId(null)} title={t("deleteTitle")}>
        <p className="text-ui text-muted">{t("deleteQuestion")}</p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setDeletingId(null)}>
            {t("cancel")}
          </Button>
          <Button variant="danger" icon="trash" disabled={pending} onClick={() => deletingId && remove(deletingId)}>
            {t("delete")}
          </Button>
        </div>
      </Dialog>
    </Card>
  );
}

function DocumentRows({
  doc,
  open,
  onToggleOpen,
  statusLabel,
  typeLabel,
  updatedLabel,
  canUpload,
  pending,
  onSwitch,
  onDelete,
}: {
  doc: KbDocumentRow;
  open: boolean;
  onToggleOpen: () => void;
  statusLabel: string;
  typeLabel: string;
  updatedLabel: string;
  canUpload: boolean;
  pending: boolean;
  onSwitch: () => void;
  onDelete: () => void;
}) {
  const t = useTranslations("knowledge");
  const size = doc.pageCount === null ? "—" : t("pages", { count: doc.pageCount });

  return (
    <>
      <tr
        className={clsx("cursor-pointer hover:bg-paper", open && "bg-paper")}
        onClick={onToggleOpen}
        aria-expanded={open}
      >
        <Td>
          <span className="inline-flex items-center gap-2.5">
            <Icon name="file" className="text-muted" />
            <span lang="hi" className="font-medium">
              {doc.title}
            </span>
          </span>
        </Td>
        <Td>
          <span className="text-muted">{typeLabel}</span>
        </Td>
        <Td>{size}</Td>
        <Td align="right">
          <span className="font-mono text-small">{doc.chunks > 0 ? formatCount(doc.chunks) : "—"}</span>
        </Td>
        <Td>
          <Chip tone={ingestTone(doc.ingestStatus, doc.isPublished)} pulse={doc.ingestStatus === "indexing"}>
            {statusLabel}
          </Chip>
        </Td>
        <Td>
          <span className="text-muted">{updatedLabel}</span>
        </Td>
        <Td align="right">
          <button
            type="button"
            aria-label={open ? t("collapse") : t("expand")}
            className="inline-flex p-1"
            onClick={(event) => {
              event.stopPropagation();
              onToggleOpen();
            }}
          >
            <Icon
              name="chevronRight"
              size={14}
              className={clsx("text-faint transition-transform", open && "rotate-90")}
            />
          </button>
        </Td>
      </tr>
      {open ? (
        <tr className="bg-paper">
          <Td colSpan={7} className="py-4">
            <div className="flex items-start justify-between gap-6 px-1">
              <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-body">
                <dt className="text-muted">{t("detail.source")}</dt>
                <dd className="break-all font-mono text-small">{doc.source ?? "—"}</dd>
                <dt className="text-muted">{t("detail.version")}</dt>
                <dd className="font-mono text-small">v{doc.version}</dd>
                <dt className="text-muted">{t("detail.embedded")}</dt>
                <dd className="font-mono text-small">
                  {formatCount(doc.embedded)} / {formatCount(doc.chunks)}
                </dd>
                {doc.ingestError ? (
                  <>
                    <dt className="text-muted">{t("detail.error")}</dt>
                    <dd className="text-red-text">{doc.ingestError}</dd>
                  </>
                ) : null}
              </dl>
              {canUpload ? (
                <div className="flex shrink-0 gap-2">
                  <Button icon="power" disabled={pending} onClick={onSwitch}>
                    {doc.isPublished ? t("switchOff") : t("switchOn")}
                  </Button>
                  <Button variant="danger" icon="trash" disabled={pending} onClick={onDelete}>
                    {t("delete")}
                  </Button>
                </div>
              ) : null}
            </div>
          </Td>
        </tr>
      ) : null}
    </>
  );
}
