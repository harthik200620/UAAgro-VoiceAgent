import { getTranslations } from "next-intl/server";

import { AskPanel } from "@/components/knowledge/ask-panel";
import { DocumentsPanel } from "@/components/knowledge/documents-panel";
import { NoteForm } from "@/components/knowledge/note-form";
import { RefreshWhilePending } from "@/components/knowledge/refresh-while-pending";
import { StatusStrip } from "@/components/knowledge/status-strip";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { can } from "@/lib/rbac";
import { getKbDocuments, getKnowledgeStatus } from "@/server/api";
import { load } from "@/server/load";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Knowledge -- what the agent is allowed to answer from, on either kind of
 * call. One corpus: every document carries the scope that says whether the
 * helpline, campaign calls or both may quote it, and the ask panel tries a
 * question as one kind of call or the other.
 */
export default async function KnowledgePage() {
  const session = await currentSession();
  if (!session || !can(session, "knowledge.view")) return <Forbidden />;

  const [t, status, documents] = await Promise.all([
    getTranslations("knowledge"),
    load(getKnowledgeStatus(session)),
    getKbDocuments(session),
  ]);
  const indexing = documents.some(
    (doc) => doc.ingestStatus === "pending" || doc.ingestStatus === "indexing",
  );
  const canUpload = can(session, "knowledge.upload");

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <StatusStrip status={status} />
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-0 flex-1 basis-[560px]">
          <DocumentsPanel
            documents={documents}
            canUpload={canUpload}
            renderedAt={new Date().toISOString()}
          />
        </div>
        <div className="flex w-full shrink-0 flex-col gap-4 xl:w-[380px]">
          {can(session, "knowledge.ask") ? <AskPanel /> : null}
          {canUpload ? <NoteForm /> : null}
        </div>
      </div>
      <RefreshWhilePending active={indexing} />
    </>
  );
}
