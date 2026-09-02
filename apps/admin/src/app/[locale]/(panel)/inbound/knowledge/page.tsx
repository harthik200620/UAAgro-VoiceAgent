import { getTranslations } from "next-intl/server";

import { InboundHeader } from "@/components/inbound/inbound-header";
import { AskPanel } from "@/components/knowledge/ask-panel";
import { DocumentsPanel } from "@/components/knowledge/documents-panel";
import { RefreshWhilePending } from "@/components/knowledge/refresh-while-pending";
import { Forbidden } from "@/components/ui/forbidden";
import { can } from "@/lib/rbac";
import { getKbDocuments } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/** Inbound: the knowledge base -- the documents, and a way to try a question against them. */
export default async function KnowledgePage() {
  const session = await currentSession();
  if (!session || !can(session, "knowledge.view")) return <Forbidden />;

  const [t, documents] = await Promise.all([getTranslations("knowledge"), getKbDocuments(session)]);
  const indexing = documents.some(
    (doc) => doc.ingestStatus === "pending" || doc.ingestStatus === "indexing",
  );

  return (
    <>
      <InboundHeader session={session} active="knowledge" subtitle={t("subtitle")} />
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <DocumentsPanel
            documents={documents}
            canUpload={can(session, "knowledge.upload")}
            renderedAt={new Date().toISOString()}
          />
        </div>
        {can(session, "knowledge.ask") ? <AskPanel /> : null}
      </div>
      <RefreshWhilePending active={indexing} />
    </>
  );
}
