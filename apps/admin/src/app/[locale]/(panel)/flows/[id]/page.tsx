import { getTranslations } from "next-intl/server";

import { FlowList } from "@/components/flows/flow-list";
import { FlowTypeTabs } from "@/components/flows/flow-type-tabs";
import { ScriptWorkbench } from "@/components/flows/script-workbench";
import { SystemPrompt } from "@/components/flows/system-prompt";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { groupScripts, nextVersion } from "@/lib/flows";
import { can } from "@/lib/rbac";
import { getFlow, getFlows } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * One script open in the editor. The version list on the left and the
 * versions card on the right come from the same fetch; the script body and
 * the prompt come from the version itself. For the helpline the prompt is
 * editable; an outbound flow shows whatever persona it carries read-only.
 */
export default async function FlowPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) return <Forbidden />;

  const [t, flow] = await Promise.all([getTranslations("flows"), getFlow(session, id)]);
  const groups = groupScripts(await getFlows(session, flow.flowType));
  const versions = groups.find((group) => group.name === flow.name)?.versions ?? [flow];
  const inbound = flow.flowType === "inbound";
  // `defaults` arrived with the 3 September contract; without it there is simply nothing to restore.
  const defaultPrompt = flow.defaults?.systemPrompt ?? null;

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <FlowTypeTabs active={flow.flowType} />
      <div className="flex flex-wrap items-start gap-4">
        <FlowList groups={groups} currentId={flow.id} sourceId={flow.id} />
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
          prompt={inbound ? { text: flow.systemPrompt ?? "", defaultText: defaultPrompt } : null}
          versions={versions.map((version) => ({
            id: version.id,
            version: version.version,
            isPublished: version.isPublished,
            publishedAt: version.publishedAt,
            publishedByName: version.publishedByName,
            changelog: version.changelog,
          }))}
          nextVersion={nextVersion(versions)}
          canPublish={can(session, "flows.publish")}
        >
          {!inbound && flow.systemPrompt ? <SystemPrompt prompt={flow.systemPrompt} /> : null}
        </ScriptWorkbench>
      </div>
    </>
  );
}
