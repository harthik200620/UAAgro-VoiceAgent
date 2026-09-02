import { getTranslations } from "next-intl/server";

import { FlowList } from "@/components/flows/flow-list";
import { FlowTypeTabs } from "@/components/flows/flow-type-tabs";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { groupScripts } from "@/lib/flows";
import { can } from "@/lib/rbac";
import { getFlows } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/** Flows -- the scripts of one direction, with nothing open yet. */
export default async function FlowsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const session = await currentSession();
  if (!session || !can(session, "flows.edit")) return <Forbidden />;

  const type = (await searchParams).type === "inbound" ? "inbound" : "outbound";
  const [t, flows] = await Promise.all([getTranslations("flows"), getFlows(session, type)]);
  const groups = groupScripts(flows);

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <FlowTypeTabs active={type} />
      <div className="flex flex-wrap items-start gap-4">
        <FlowList groups={groups} currentId={null} sourceId={groups[0]?.newest.id ?? null} />
        <Card className="flex-1 px-6 py-10 text-center text-ui text-muted">
          {groups.length === 0 ? t("nothingYet") : t("chooseScript")}
        </Card>
      </div>
    </>
  );
}
