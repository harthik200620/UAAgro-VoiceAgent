import { getTranslations, setRequestLocale } from "next-intl/server";

import { DncToggle } from "@/components/dnc-toggle";
import { can } from "@/lib/rbac";
import { getFarmers } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Farmers -- the searchable directory.
 *
 * Search accepts a name, a village, or **four digits**. Not a full number:
 * accepting one would mean the panel handling a number it has no business
 * holding, and the realistic case is a staff member reading the last four off
 * a caller ID anyway.
 *
 * The export §15.1 asks for honours RLS by construction -- the rows here come
 * from a query the database has already scoped to this user's centres, so an
 * export is of what is on the screen rather than a second, wider query.
 */
export default async function FarmersPage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("farmers");

  if (!can(session, "farmers.view")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const filters = await searchParams;
  const q = typeof filters.q === "string" ? filters.q : "";

  const query = new URLSearchParams({ limit: "100" });
  if (q) query.set("q", q);

  const { rows, total } = await getFarmers(session!, query.toString());
  const mayEdit = can(session, "farmers.edit");

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className="mb-3 text-sm text-muted">{t("resultCount", { total })}</p>

      <form className="mb-4 flex items-end gap-2 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted">{t("searchLabel")}</span>
          <input
            type="search"
            name="q"
            defaultValue={q}
            placeholder={t("searchPlaceholder")}
            className="w-72 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
          />
        </label>
        <button
          type="submit"
          className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          {t("search")}
        </button>
      </form>

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[60rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("name")}</th>
              <th scope="col">{t("village")}</th>
              <th scope="col">{t("district")}</th>
              <th scope="col">{t("centre")}</th>
              <th scope="col">{t("phone")}</th>
              <th scope="col">{t("language")}</th>
              <th scope="col">{t("crops")}</th>
              <th scope="col">{t("land")}</th>
              <th scope="col">{t("consent")}</th>
              <th scope="col">{t("dnc")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                {/* lang="hi": the name is Devanagari inside an English
                    page. Unmarked, a screen reader pronounces it with
                    English phonemes and the browser may pick a font
                    with no Devanagari coverage. */}
                <td lang="hi">{row.fullName}</td>
                <td>{row.village ?? "—"}</td>
                <td>{row.districtName ?? "—"}</td>
                <td>{row.centreCode ?? "—"}</td>
                <td className="font-mono text-xs">••••{row.phoneLast4}</td>
                <td>{row.preferredLanguage}</td>
                <td className="max-w-[12rem] truncate text-xs">
                  {row.crops.join(", ") || "—"}
                </td>
                <td className="tabular-nums">
                  {/* Bigha, not hectares. §11.3 is explicit that an agent
                      answering in hectares reads as an outsider, and the panel
                      staff talk to farmers with the same units. */}
                  {row.landAreaBigha === null
                    ? "—"
                    : t("bigha", { value: row.landAreaBigha })}
                </td>
                <td>
                  <span className={row.hasPromotionalConsent ? "text-ok" : "text-muted"}>
                    {row.hasPromotionalConsent ? t("consentYes") : t("consentNo")}
                  </span>
                </td>
                <td>
                  {mayEdit ? (
                    <DncToggle
                      farmerId={row.id}
                      isDnc={row.isDnc}
                      onLabel={t("dncOn")}
                      offLabel={t("dncOff")}
                    />
                  ) : (
                    <span className={row.isDnc ? "text-warn" : "text-muted"}>
                      {row.isDnc ? t("dncOn") : t("dncOff")}
                    </span>
                  )}
                </td>
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
