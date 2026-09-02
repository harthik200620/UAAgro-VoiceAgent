import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { getAnalytics } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Analytics.
 *
 * Two things here are worth more than the rest of the screen.
 *
 * **Cost per resolved query, next to cost per call.** §8 measures the cost of
 * an *outcome*. A screen showing only cost-per-call rewards a cheap call that
 * resolved nothing, which is the opposite of what anyone wants optimised.
 *
 * **STT confidence broken out by language and by centre.** §15.1 says plainly
 * that this surfaces which districts have accent or noise problems, and it is
 * a real finding: one centre with a bad handset looks exactly like a bad agent
 * until the number is split apart.
 */
export default async function AnalyticsPage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("analytics");

  if (!can(session, "calls.view")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const filters = await searchParams;
  const days = Number(typeof filters.days === "string" ? filters.days : 7) || 7;
  const data = await getAnalytics(session!, days);

  const components = Object.entries(data.costByComponent);
  const componentTotal = components.reduce((sum, [, value]) => sum + value, 0);

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className="mb-4 text-sm text-muted">{t("window", { days })}</p>

      <div className="mb-6 flex gap-2 text-sm">
        {[7, 30, 90].map((option) => (
          <a
            key={option}
            href={`?days=${option}`}
            className={
              option === days
                ? "font-semibold underline underline-offset-2"
                : "text-muted"
            }
          >
            {t("lastDays", { days: option })}
          </a>
        ))}
      </div>

      <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile label={t("costPerCall")} value={`₹${data.costPerCallRupees.toFixed(2)}`} />
        <Tile
          label={t("costPerResolved")}
          value={`₹${data.costPerResolvedRupees.toFixed(2)}`}
          hint={t("costPerResolvedHint")}
        />
      </div>

      <h2 className="mb-2 text-sm font-semibold">{t("costByComponent")}</h2>
      <div className="mb-6 overflow-x-auto">
        <table className="grid-dense w-full max-w-md border-collapse text-sm">
          <tbody>
            {components.map(([name, value]) => (
              <tr key={name} className="border-b border-slate-100 dark:border-slate-800">
                <td>{t(`components.${name}`)}</td>
                <td className="text-right tabular-nums">₹{value.toFixed(2)}</td>
                <td className="w-24 text-right text-xs text-muted">
                  {componentTotal > 0
                    ? `${((value / componentTotal) * 100).toFixed(0)}%`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="mb-2 text-sm font-semibold">{t("byLanguage")}</h2>
      <div className="mb-6 overflow-x-auto">
        <table className="grid-dense w-full min-w-[32rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("language")}</th>
              <th scope="col" className="text-right">{t("calls")}</th>
              <th scope="col" className="text-right">{t("confidence")}</th>
              <th scope="col" className="text-right">{t("transferRate")}</th>
            </tr>
          </thead>
          <tbody>
            {data.byLanguage.map((row) => (
              <tr key={row.language} className="border-b border-slate-100 dark:border-slate-800">
                <td>{row.language || "—"}</td>
                <td className="text-right tabular-nums">{row.calls}</td>
                <td className="text-right tabular-nums">
                  <Confidence value={row.avgConfidence} />
                </td>
                <td className="text-right tabular-nums">
                  {(row.transferRate * 100).toFixed(0)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2 className="mb-2 text-sm font-semibold">{t("byCentre")}</h2>
      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[32rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("centre")}</th>
              <th scope="col" className="text-right">{t("calls")}</th>
              <th scope="col" className="text-right">{t("confidence")}</th>
              <th scope="col" className="text-right">{t("resolutionRate")}</th>
            </tr>
          </thead>
          <tbody>
            {data.byCentre.map((row) => (
              <tr key={row.centreCode} className="border-b border-slate-100 dark:border-slate-800">
                <td className="font-mono text-xs">{row.centreCode}</td>
                <td className="text-right tabular-nums">{row.calls}</td>
                <td className="text-right tabular-nums">
                  <Confidence value={row.avgConfidence} />
                </td>
                <td className="text-right tabular-nums">
                  {(row.resolutionRate * 100).toFixed(0)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Tile({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded border border-slate-200 px-3 py-2 dark:border-slate-800">
      <p className="text-xs text-muted">{label}</p>
      <p className="text-xl tabular-nums">{value}</p>
      {hint ? <p className="mt-0.5 text-xs text-muted">{hint}</p> : null}
    </div>
  );
}

/**
 * §11.4 escalates on a rolling mean below 0.55, so that is the line drawn
 * here: a centre sitting under it is the one to send a better handset to.
 *
 * Null is rendered as an em dash rather than as zero. A language with no
 * confidence data is not a language whose audio is perfectly bad.
 */
function Confidence({ value }: { value: number | null }) {
  if (value === null) return <span className="text-muted">—</span>;
  return (
    <span className={value < 0.55 ? "text-warn" : ""}>{value.toFixed(2)}</span>
  );
}
