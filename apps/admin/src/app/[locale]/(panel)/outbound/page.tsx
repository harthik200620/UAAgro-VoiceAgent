import { getTranslations } from "next-intl/server";

import { CampaignList } from "@/components/outbound/campaign-list";
import { QuickDial } from "@/components/outbound/quick-dial-form";
import { ButtonLink } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { can } from "@/lib/rbac";
import { getCampaigns, getFlows } from "@/server/api";
import { optional } from "@/server/load";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Outbound -- numbers to call right now at the top, every campaign under it.
 * Only published outbound scripts are offered to "Call now": a draft may
 * still change before anyone hears it.
 */
export default async function OutboundPage() {
  const session = await currentSession();
  if (!session || !can(session, "campaigns.view")) return <Forbidden />;

  const canDial = can(session, "campaigns.dial");
  const [t, campaigns, flows] = await Promise.all([
    getTranslations("outbound"),
    getCampaigns(session),
    canDial ? optional(getFlows(session, "outbound")) : Promise.resolve(null),
  ]);
  const published = (flows ?? [])
    .filter((flow) => flow.isPublished)
    .map((flow) => ({ id: flow.id, name: flow.name, version: flow.version }));

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")}>
        {can(session, "campaigns.create") ? (
          <ButtonLink href="/outbound/new" size="md" icon="plus">
            {t("newCampaign")}
          </ButtonLink>
        ) : null}
      </PageHeader>
      {canDial ? (
        <Card className="px-6 pb-5 pt-5">
          <QuickDial flows={published} />
        </Card>
      ) : null}
      <CampaignList campaigns={campaigns} />
    </>
  );
}
