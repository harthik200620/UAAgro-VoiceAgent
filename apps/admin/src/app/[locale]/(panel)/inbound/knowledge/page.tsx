import { getTranslations } from "next-intl/server";

import { InboundHeader } from "@/components/inbound/inbound-header";
import { KnowledgeScreen } from "@/components/knowledge/knowledge-screen";
import { Forbidden } from "@/components/ui/forbidden";
import { can } from "@/lib/rbac";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Inbound → Knowledge: what the helpline can answer with.
 *
 * The same corpus the outbound side manages, filtered to the documents a
 * helpline call may quote; the screen itself is shared.
 */
export default async function KnowledgePage() {
  const session = await currentSession();
  if (!session || !can(session, "knowledge.view")) return <Forbidden />;

  const t = await getTranslations("knowledge");
  return (
    <>
      <InboundHeader session={session} active="knowledge" subtitle={t("subtitle")} />
      <KnowledgeScreen session={session} direction="inbound" />
    </>
  );
}
