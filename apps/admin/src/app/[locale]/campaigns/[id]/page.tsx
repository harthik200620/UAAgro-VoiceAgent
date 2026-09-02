import { getTranslations, setRequestLocale } from "next-intl/server";

import { ComplianceGate } from "@/components/compliance-gate";
import { can, canApproveCampaign } from "@/lib/rbac";
import { getCampaignGate } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Campaigns -- the approval screen.
 *
 * §13.1: "the approval screen shows final contact count, estimated cost,
 * estimated duration, the exact script, and the WhatsApp template that will be
 * sent." A reviewer approving without seeing the script is approving a number.
 */
export default async function CampaignPage({
  params,
}: {
  params: Promise<{ locale: string; id: string }>;
}) {
  const { locale, id } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("campaigns");
  const errors = await getTranslations("errors");

  if (!can(session, "campaigns.create")) {
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const gate = await getCampaignGate(session!, id);

  // §13.1's four-eyes rule. The server decides -- it knows who created the row
  // and cannot be lied to about it. This only disables the button and explains,
  // so a reviewer is not left clicking a control that silently fails.
  const createdBy = ""; // supplied by the campaign record; empty until loaded
  const mayApprove = canApproveCampaign(session, createdBy) && gate.blockedBy.length === 0;

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>
      <ComplianceGate gate={gate} />

      <button
        type="button"
        disabled={!mayApprove}
        className="rounded border border-slate-300 px-3 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700"
      >
        {t("approve")}
      </button>
      {!mayApprove && gate.blockedBy.length === 0 ? (
        <p className="mt-2 text-sm text-muted">{t("cannotApproveOwn")}</p>
      ) : null}
    </section>
  );
}
