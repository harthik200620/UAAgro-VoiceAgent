import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { getOffers } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Offers.
 *
 * Validity is computed from today's date in IST rather than read from a
 * stored flag: an offer valid "until the 30th" ends at the close of the 30th
 * in Lucknow, and a screen that showed an expired offer as live is how it gets
 * pitched on a call the next morning.
 *
 * §15.1 also asks for a rendered TTS preview of the spoken pitch and a preview
 * of the WhatsApp message before saving. Neither is built -- both are in the
 * panel's known gaps. A preview that showed the template text without
 * rendering it through the same normaliser the phone path uses would mislead
 * about exactly the thing it exists to check: what the farmer actually hears.
 */
export default async function OffersPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("offers");

  if (!can(session, "campaigns.create")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const rows = await getOffers(session!);
  const live = rows.filter((row) => row.isActive);

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className="mb-4 text-sm text-muted">
        {t("liveCount", { live: live.length, total: rows.length })}
      </p>

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[50rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("code")}</th>
              <th scope="col">{t("name")}</th>
              <th scope="col">{t("discount")}</th>
              <th scope="col">{t("validFrom")}</th>
              <th scope="col">{t("validTo")}</th>
              <th scope="col">{t("template")}</th>
              <th scope="col">{t("state")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <td className="font-mono text-xs">{row.code}</td>
                <td>
                  {row.name}
                  {/* The pitch the agent speaks, in its own language. */}
                  <span lang="hi" className="block max-w-[24rem] truncate text-xs text-muted">
                    {row.descriptionHi}
                  </span>
                </td>
                <td className="tabular-nums">
                  {row.discountType === "percent"
                    ? `${row.discountValue}%`
                    : `₹${row.discountValue.toFixed(2)}`}
                </td>
                <td className="whitespace-nowrap text-xs">{row.validFrom}</td>
                <td className="whitespace-nowrap text-xs">{row.validTo}</td>
                <td className="text-xs">
                  {/* §14: a promotional WhatsApp message needs a registered
                      template. An offer without one cannot be followed up in
                      writing, which is most of what makes a campaign work. */}
                  <span className={row.whatsappTemplateName ? "" : "text-warn"}>
                    {row.whatsappTemplateName ?? t("noTemplate")}
                  </span>
                </td>
                <td>
                  <span className={row.isActive ? "text-ok" : "text-muted"}>
                    {row.isActive ? t("live") : t("expired")}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t("noOffers")}</p>
      ) : null}
    </section>
  );
}
