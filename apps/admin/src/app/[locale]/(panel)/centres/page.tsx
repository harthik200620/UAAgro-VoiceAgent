import { getTranslations } from "next-intl/server";

import { AddCentreForm } from "@/components/centres/add-centre-form";
import { CentresScreen } from "@/components/centres/centres-screen";
import { HandoverCard } from "@/components/centres/handover-card";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { can } from "@/lib/rbac";
import { getCentres, getTransferRules } from "@/server/api";
import { optional } from "@/server/load";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Centres -- the stores on a map, and everything the agent says about each:
 * where it is, when it opens, who runs it, what it sells and what has run
 * out. The hand-over rules load only for a session that may edit them,
 * because that is who the API serves them to.
 */
export default async function CentresPage() {
  const session = await currentSession();
  if (!session || !can(session, "centres.view")) return <Forbidden />;

  const canEdit = can(session, "centres.edit");
  const [t, centres, rules] = await Promise.all([
    getTranslations("centres"),
    getCentres(session),
    canEdit ? optional(getTransferRules(session)) : Promise.resolve(null),
  ]);

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <CentresScreen centres={centres} canEdit={canEdit} canEditStock={can(session, "inventory.edit")} />
      <div className="flex flex-wrap items-start gap-4">
        {rules ? (
          <div className="min-w-0 flex-1 basis-[520px]">
            <HandoverCard rules={rules} canEdit={canEdit} />
          </div>
        ) : null}
        {canEdit ? (
          <Card className="w-full shrink-0 px-5 pb-5 pt-4.5 xl:w-[380px]">
            <AddCentreForm />
          </Card>
        ) : null}
      </div>
    </>
  );
}
