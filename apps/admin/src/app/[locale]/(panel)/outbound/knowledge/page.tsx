import { getTranslations } from "next-intl/server";

import { KnowledgeScreen } from "@/components/knowledge/knowledge-screen";
import { OutboundTabs } from "@/components/outbound/outbound-tabs";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { can } from "@/lib/rbac";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Outbound → Knowledge: what an offer call can answer with.
 *
 * The same documents as the helpline's, filtered to the ones a campaign call
 * may quote. A farmer who presses 2 asks a real question, and this is where
 * the material that answers it is added.
 */
export default async function OutboundKnowledgePage() {
  const session = await currentSession();
  if (!session || !can(session, "knowledge.view")) return <Forbidden />;

  const t = await getTranslations("knowledge");
  return (
    <>
      <PageHeader title={t("outboundTitle")} subtitle={t("outboundSubtitle")} />
      <OutboundTabs session={session} active="knowledge" />
      <KnowledgeScreen session={session} direction="outbound" />
    </>
  );
}
