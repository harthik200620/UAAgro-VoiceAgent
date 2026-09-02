import { getTranslations, setRequestLocale } from "next-intl/server";

import { FORMAT_LOCALE } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getCalls } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Call Explorer -- "the workhorse".
 *
 * Filters are read from the URL rather than held in component state, so a
 * filtered view is a link. That is the difference between "the failed calls
 * from Barabanki yesterday" being something a manager can paste into a message
 * and something they have to describe in words.
 *
 * The caller column is four digits. It is not truncated for display -- four
 * digits is all the API returns, because §17 gives the response model nowhere
 * to put more.
 */
export default async function CallsPage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("calls");

  if (!can(session, "calls.view")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const filters = await searchParams;
  const one = (key: string) => {
    const value = filters[key];
    return typeof value === "string" && value ? value : undefined;
  };

  const query = new URLSearchParams();
  const outcome = one("outcome");
  const language = one("language");
  if (outcome) query.set("outcome", outcome);
  if (language) query.set("language", language);
  query.set("limit", "100");

  const { rows, total } = await getCalls(session!, query.toString());

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className="mb-3 text-sm text-muted">{t("resultCount", { total })}</p>

      {/* A GET form: submitting changes the URL, which is what makes a
          filtered view shareable and the back button work. */}
      <form className="mb-4 flex flex-wrap items-end gap-2 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted">{t("outcome")}</span>
          <input
            type="text"
            name="outcome"
            defaultValue={outcome ?? ""}
            placeholder={t("outcomePlaceholder")}
            className="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted">{t("language")}</span>
          <input
            type="text"
            name="language"
            defaultValue={language ?? ""}
            placeholder="hi-IN"
            className="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
          />
        </label>
        <button
          type="submit"
          className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          {t("apply")}
        </button>
      </form>

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[62rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("startedAt")}</th>
              <th scope="col">{t("callRef")}</th>
              <th scope="col">{t("direction")}</th>
              <th scope="col">{t("caller")}</th>
              <th scope="col">{t("centre")}</th>
              <th scope="col">{t("language")}</th>
              <th scope="col">{t("tier")}</th>
              <th scope="col">{t("duration")}</th>
              <th scope="col">{t("outcome")}</th>
              <th scope="col">{t("intent")}</th>
              <th scope="col">{t("cost")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <td className="whitespace-nowrap text-xs">
                  {new Date(row.startedAt).toLocaleString(FORMAT_LOCALE)}
                </td>
                <td className="font-mono text-xs">{row.callRef}</td>
                <td>{t(`directions.${row.direction}`)}</td>
                {/* Four digits, prefixed so nobody reads it as a whole
                    number. The API has no field for more (§17). */}
                <td className="font-mono text-xs">
                  {row.callerLast4 ? `••••${row.callerLast4}` : "—"}
                </td>
                <td>{row.centreCode ?? "—"}</td>
                <td>{row.language || "—"}</td>
                <td>
                  {/* §5.1 requires the tier to be labelled honestly rather
                      than every language looking equivalent. */}
                  <span className={row.qualityTier === "C" ? "text-warn" : ""}>
                    {row.qualityTier}
                  </span>
                </td>
                <td className="tabular-nums">{row.durationSeconds}s</td>
                <td>
                  <span
                    className={
                      row.outcome === "resolved"
                        ? "text-ok"
                        : row.outcome === "system_failure"
                          ? "text-danger"
                          : ""
                    }
                  >
                    {row.outcome || "—"}
                  </span>
                  {row.transferred ? (
                    <span className="ml-1 text-xs text-muted">
                      {t("transferred")}
                    </span>
                  ) : null}
                </td>
                <td className="text-xs">{row.intent ?? "—"}</td>
                <td className="tabular-nums">₹{row.costRupees.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t("noResults")}</p>
      ) : null}
    </section>
  );
}
