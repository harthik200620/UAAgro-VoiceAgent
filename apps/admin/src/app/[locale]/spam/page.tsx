import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { getSpamRules } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Spam & Screening.
 *
 * The hit count on its own says nothing useful. A rule with four thousand hits
 * is either blocking four thousand robocalls or turning away four thousand
 * farmers, and from the count those are identical. The false-positive rate --
 * fed by §15.1's one-click "this was a real farmer" -- is what separates them,
 * so it sits next to the count rather than on a second screen.
 *
 * Rules are sorted by hits, and any rule rejecting more than one caller in ten
 * wrongly is marked. On a helpline, a false positive is a farmer who could not
 * get through and will not try again.
 */
export default async function SpamPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("spam");

  if (!can(session, "campaigns.control")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const rows = await getSpamRules(session!);
  const noisy = rows.filter((row) => row.isActive && row.falsePositiveRate > 0.1);

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>

      {noisy.length > 0 ? (
        <div
          role="status"
          className="mb-4 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm"
        >
          <p className="font-medium text-warn">
            {t("noisyRules", { count: noisy.length })}
          </p>
          <p className="mt-1 text-xs text-muted">{t("noisyRulesDetail")}</p>
        </div>
      ) : null}

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[44rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("ruleType")}</th>
              <th scope="col">{t("pattern")}</th>
              <th scope="col">{t("action")}</th>
              <th scope="col" className="text-right">{t("hits")}</th>
              <th scope="col" className="text-right">{t("falsePositives")}</th>
              <th scope="col" className="text-right">{t("falsePositiveRate")}</th>
              <th scope="col">{t("state")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <td>{row.ruleType}</td>
                <td className="max-w-[16rem] truncate font-mono text-xs">
                  {row.pattern ?? "—"}
                </td>
                <td>{row.action}</td>
                <td className="text-right tabular-nums">{row.hitCount}</td>
                <td className="text-right tabular-nums">
                  {row.falsePositiveCount}
                </td>
                <td
                  className={`text-right tabular-nums ${
                    row.falsePositiveRate > 0.1 ? "text-warn" : ""
                  }`}
                >
                  {row.hitCount === 0
                    ? "—"
                    : `${(row.falsePositiveRate * 100).toFixed(0)}%`}
                </td>
                <td>
                  <span className={row.isActive ? "text-ok" : "text-muted"}>
                    {row.isActive ? t("active") : t("inactive")}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t("noRules")}</p>
      ) : null}
    </section>
  );
}
