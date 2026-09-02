import { getTranslations } from "next-intl/server";

import { AddCentreForm } from "@/components/centres/add-centre-form";
import { CentresTable } from "@/components/centres/centres-table";
import { HandoverCard } from "@/components/centres/handover-card";
import { InboundHeader } from "@/components/inbound/inbound-header";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { can } from "@/lib/rbac";
import { getCentres, getTransferRules } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Inbound: centres and managers. The hand-over rules load only for a
 * session that may edit them, because that is who the API serves them to;
 * a centre manager sees their own centres and nothing about routing.
 */
export default async function CentresPage() {
  const session = await currentSession();
  if (!session || !can(session, "centres.view")) return <Forbidden />;

  const canEdit = can(session, "centres.edit");
  const [t, centres, rules] = await Promise.all([
    getTranslations("centres"),
    getCentres(session),
    canEdit ? getTransferRules(session) : Promise.resolve(null),
  ]);
  const open = centres.filter((centre) => centre.isActive).length;

  return (
    <>
      <InboundHeader session={session} active="centres" subtitle={t("subtitle")} />
      <div className="flex items-start gap-4">
        <div className="flex min-w-0 flex-1 flex-col gap-4">
          <Card className="px-2 pb-1.5 pt-4">
            <div className="px-3.5 pb-2.5">
              <span className="font-semibold">{t("title")}</span>{" "}
              <span className="text-body text-muted">
                · {t("openCount", { count: open })} · {t("explainer")}
              </span>
            </div>
            <CentresTable
              centres={centres}
              canEdit={canEdit}
              canEditStock={can(session, "inventory.edit")}
            />
          </Card>
          {rules ? <HandoverCard rules={rules} canEdit={canEdit} /> : null}
        </div>
        {canEdit ? (
          <Card className="w-[340px] shrink-0 px-5 pb-5 pt-4.5">
            <AddCentreForm />
          </Card>
        ) : null}
      </div>
    </>
  );
}
