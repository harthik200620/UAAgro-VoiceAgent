import { getTranslations } from "next-intl/server";

import { ScriptWorkbench } from "@/components/flows/script-workbench";
import { SystemPrompt } from "@/components/flows/system-prompt";
import { InboundHeader } from "@/components/inbound/inbound-header";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { groupScripts, nextVersion } from "@/lib/flows";
import { can } from "@/lib/rbac";
import { getFlow, getFlows } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Inbound: the greeting. The inbound script's newest version opens in the
 * same editor Flows uses -- a draft newer than the live one is what the
 * operator is working on, so that is what opens; otherwise the live words.
 */
export default async function GreetingPage() {
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) return <Forbidden />;

  const [t, flows] = await Promise.all([getTranslations("greeting"), getFlows(session, "inbound")]);
  const group = groupScripts(flows)[0];

  if (!group) {
    return (
      <>
        <InboundHeader session={session} active="greeting" subtitle={t("subtitle")} />
        <Card className="px-6 py-10 text-center text-ui text-muted">{t("empty")}</Card>
      </>
    );
  }

  const flow = await getFlow(session, group.newest.id);

  return (
    <>
      <InboundHeader session={session} active="greeting" subtitle={t("subtitle")} />
      <div className="flex flex-wrap items-start gap-4">
        <ScriptWorkbench
          flow={{
            id: flow.id,
            name: flow.name,
            flowType: flow.flowType,
            version: flow.version,
            isPublished: flow.isPublished,
            publishedAt: flow.publishedAt,
          }}
          script={flow.script}
          versions={group.versions.map((version) => ({
            id: version.id,
            version: version.version,
            isPublished: version.isPublished,
            publishedAt: version.publishedAt,
            publishedByName: version.publishedByName,
            changelog: version.changelog,
          }))}
          nextVersion={nextVersion(group.versions)}
          canPublish={can(session, "flows.publish")}
          destination="greeting"
        >
          {flow.systemPrompt ? <SystemPrompt prompt={flow.systemPrompt} /> : null}
        </ScriptWorkbench>
      </div>
    </>
  );
}
