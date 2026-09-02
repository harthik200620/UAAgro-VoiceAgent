import { getTranslations, setRequestLocale } from "next-intl/server";

import { UnapprovedBanner } from "@/components/unapproved-banner";
import { ApproveButton } from "@/components/approve-button";
import { can } from "@/lib/rbac";
import { getAdvisoryRows } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Crop Advisory -- the screen that makes §9's safety control visible.
 *
 * The single most important element here is the banner, and §15.1 is specific
 * about it: "a permanent banner counting unapproved rows. Nothing unapproved is
 * ever served."
 *
 * Permanent matters. A dismissible banner is dismissed on day one and the
 * queue is never looked at again, and the failure that produces -- an agronomy
 * team that believes its content is live when the agent is refusing every
 * dosage question -- is invisible from inside the panel. So it renders whenever
 * the count is non-zero and has no close control.
 */
export default async function AdvisoryPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("advisory");

  if (!can(session, "advisory.view")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const { rows, unapproved } = await getAdvisoryRows(session!);
  const mayApprove = can(session, "advisory.approve");

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>

      <UnapprovedBanner count={unapproved} />

      {!mayApprove && unapproved > 0 ? (
        <p className="mb-3 text-sm text-muted">{t("cannotApprove")}</p>
      ) : null}

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[60rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("crop")}</th>
              <th scope="col">{t("stage")}</th>
              <th scope="col">{t("problem")}</th>
              <th scope="col">{t("product")}</th>
              <th scope="col">{t("dose")}</th>
              <th scope="col">{t("phi")}</th>
              <th scope="col">{t("precaution")}</th>
              <th scope="col">{t("state")}</th>
              <th scope="col">{t("approvedBy")}</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              // §16.2: a crop-protection row without a pre-harvest interval and
              // a precaution cannot be approved. The database refuses it with a
              // CHECK constraint; showing it here means the agronomist finds
              // out while editing rather than on submit.
              const incomplete =
                row.isCropProtection &&
                (row.phiDays === null || !row.precautionHi);

              return (
                <tr
                  key={row.id}
                  className="border-b border-slate-100 dark:border-slate-800"
                >
                  <td lang="hi">{row.cropHi}</td>
                  <td>{row.stage ?? "—"}</td>
                  <td lang="hi">{row.problemHi ?? "—"}</td>
                  <td lang="hi">{row.productHi}</td>
                  <td>{row.dose}</td>
                  <td className={row.isCropProtection && row.phiDays === null ? "text-danger" : ""}>
                    {row.phiDays === null ? "—" : row.phiDays}
                  </td>
                  {/* The precaution is what a farmer is told before they
                      spray. It stays in the language it was approved in. */}
                  <td className="max-w-[16rem]" lang="hi">
                    <span className={!row.precautionHi && row.isCropProtection ? "text-danger" : ""}>
                      {row.precautionHi ?? "—"}
                    </span>
                  </td>
                  <td>
                    <span
                      className={
                        row.approvalState === "approved" ? "text-ok" : "text-warn"
                      }
                    >
                      {t(`states.${row.approvalState}`)}
                    </span>
                  </td>
                  <td className="text-xs text-muted">
                    {row.approvedByName ?? "—"}
                  </td>
                  <td>
                    {row.approvalState !== "approved" && mayApprove ? (
                      <ApproveButton
                        recommendationId={row.id}
                        disabled={incomplete}
                        disabledReason={incomplete ? t("phiRequired") : undefined}
                        label={t("approve")}
                      />
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
