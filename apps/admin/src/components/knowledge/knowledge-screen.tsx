import { AskPanel } from "@/components/knowledge/ask-panel";
import { DocumentsPanel } from "@/components/knowledge/documents-panel";
import { RefreshWhilePending } from "@/components/knowledge/refresh-while-pending";
import type { KbDocumentRow } from "@/lib/contract";
import { can, type Session } from "@/lib/rbac";
import { getKbDocuments } from "@/server/api";

export type KnowledgeDirection = "inbound" | "outbound";

/**
 * The knowledge base as one side of the panel sees it.
 *
 * One corpus, two views. A document is marked for the helpline, for campaign
 * calls, or for both, and each side lists what its calls can actually reach --
 * so an operator setting up a campaign is looking at the material that
 * campaign's calls will quote, not at everything and a mental filter.
 *
 * Uploads default to "both" from either side. The alternative -- defaulting to
 * the side you happen to be standing on -- would quietly make the product
 * catalogue helpline-only because that is where somebody uploaded it, and the
 * failure would show up as an offer call that cannot answer a question about
 * a product.
 */
export async function KnowledgeScreen({
  session,
  direction,
}: {
  session: Session;
  direction: KnowledgeDirection;
}) {
  const all = await getKbDocuments(session);
  const documents = all.filter((doc) => usableOn(doc, direction));
  const elsewhere = all.length - documents.length;
  const indexing = documents.some(
    (doc) => doc.ingestStatus === "pending" || doc.ingestStatus === "indexing",
  );

  return (
    <>
      <div className="flex items-start gap-4">
        <div className="min-w-0 flex-1">
          <DocumentsPanel
            documents={documents}
            canUpload={can(session, "knowledge.upload")}
            renderedAt={new Date().toISOString()}
            direction={direction}
            elsewhere={elsewhere}
          />
        </div>
        {can(session, "knowledge.ask") ? <AskPanel direction={direction} /> : null}
      </div>
      <RefreshWhilePending active={indexing} />
    </>
  );
}

/** Whether a call of this direction may quote the document. */
export function usableOn(doc: KbDocumentRow, direction: KnowledgeDirection): boolean {
  return doc.scope === "both" || doc.scope === direction;
}
