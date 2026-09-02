import { getTranslations } from "next-intl/server";

import { CampaignForm } from "@/components/outbound/campaign-form";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { Icon } from "@/components/ui/icon";
import { PageHeader } from "@/components/ui/page-header";
import { Link } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getFlows } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * A new campaign. Only published outbound scripts are offered: a draft may
 * still change before anyone hears it, and a campaign is a promise to say
 * one particular thing.
 */
export default async function NewCampaignPage() {
  const session = await currentSession();
  if (!session || !can(session, "campaigns.create")) return <Forbidden />;

  const [t, flows] = await Promise.all([getTranslations("newCampaign"), getFlows(session, "outbound")]);
  const published = flows
    .filter((flow) => flow.isPublished)
    .map((flow) => ({ id: flow.id, name: flow.name, version: flow.version }));

  return (
    <>
      <div className="flex flex-col gap-3.5">
        <Link href="/outbound" className="inline-flex items-center gap-1.5 text-body text-muted hover:text-ink">
          <Icon name="chevronLeft" size={14} />
          {t("back")}
        </Link>
        <PageHeader title={t("title")} subtitle={t("subtitle")} />
      </div>

      <div className="flex items-start gap-4">
        <Card className="w-[720px] max-w-full px-6 py-5">
          {published.length === 0 ? (
            <p className="text-ui text-muted">
              {t("noFlows")}{" "}
              <Link href="/flows" className="underline underline-offset-2">
                {t("goToFlows")}
              </Link>
            </p>
          ) : (
            <CampaignForm flows={published} />
          )}
        </Card>
        <Card className="flex w-[380px] shrink-0 flex-col gap-3 px-5.5 py-4.5 text-body text-muted">
          <div className="font-semibold text-ink">{t("whatHappens")}</div>
          <p>{t("explainChecks")}</p>
          <p>{t("explainApproval")}</p>
          <p>{t("explainOptOut")}</p>
        </Card>
      </div>
    </>
  );
}
