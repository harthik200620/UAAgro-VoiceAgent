import { getTranslations } from "next-intl/server";

import { CampaignList } from "@/components/outbound/campaign-list";
import { OutboundTabs } from "@/components/outbound/outbound-tabs";
import { ButtonLink } from "@/components/ui/button";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { can } from "@/lib/rbac";
import { getCampaigns } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/** Outbound -- every campaign, and the way to start one. */
export default async function OutboundPage() {
  const session = await currentSession();
  if (!session || !can(session, "campaigns.view")) return <Forbidden />;

  const [t, campaigns] = await Promise.all([getTranslations("outbound"), getCampaigns(session)]);

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")}>
        {can(session, "campaigns.create") ? (
          <ButtonLink href="/outbound/new" variant="primary" size="md" icon="plus">
            {t("newCampaign")}
          </ButtonLink>
        ) : null}
      </PageHeader>
      <OutboundTabs session={session} active="campaigns" />
      <CampaignList campaigns={campaigns} />
    </>
  );
}
