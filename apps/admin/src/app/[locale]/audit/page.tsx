import { getTranslations, setRequestLocale } from "next-intl/server";

import { FORMAT_LOCALE } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getAudit } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Audit Log -- filterable, exportable, tamper-evident.
 *
 * The chain state is checked on every load and rendered at the top, not
 * hidden behind a button. §17 makes the log tamper-evident; a page that
 * displayed a broken chain as ordinary history would be worse than no page,
 * because it would give an auditor confidence they have not earned.
 *
 * Before and after are shown as-is. The server writes only what changed and
 * never the values §23-6 forbids -- a phone decryption records its *purpose*,
 * not the number -- so there is nothing to redact here, and redacting in the
 * view would mean the log itself was untrustworthy.
 */
export default async function AuditPage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("audit");

  if (!can(session, "audit.view")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const filters = await searchParams;
  const one = (key: string) =>
    typeof filters[key] === "string" && filters[key] ? (filters[key] as string) : undefined;

  const query = new URLSearchParams({ limit: "100" });
  const action = one("action");
  const resourceType = one("resource_type");
  if (action) query.set("action", action);
  if (resourceType) query.set("resource_type", resourceType);

  const page = await getAudit(session!, query.toString());

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className="mb-3 text-sm text-muted">{t("resultCount", { total: page.total })}</p>

      <div
        role="status"
        className={`mb-4 rounded border px-3 py-2 text-sm ${
          page.chainIntact
            ? "border-ok/40 bg-ok/10"
            : "border-danger/40 bg-danger/10"
        }`}
      >
        <p className={page.chainIntact ? "text-ok" : "font-medium text-danger"}>
          {page.chainIntact
            ? t("chainIntact")
            : t("chainBroken", { index: page.brokenAt ?? 0 })}
        </p>
        {!page.chainIntact ? (
          <p className="mt-1 text-xs text-muted">{t("chainBrokenDetail")}</p>
        ) : null}
      </div>

      <form className="mb-4 flex flex-wrap items-end gap-2 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted">{t("action")}</span>
          <input
            type="text"
            name="action"
            defaultValue={action ?? ""}
            className="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted">{t("resource")}</span>
          <input
            type="text"
            name="resource_type"
            defaultValue={resourceType ?? ""}
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
        <table className="grid-dense w-full min-w-[58rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col" className="text-right">#</th>
              <th scope="col">{t("at")}</th>
              <th scope="col">{t("actor")}</th>
              <th scope="col">{t("action")}</th>
              <th scope="col">{t("resource")}</th>
              <th scope="col">{t("before")}</th>
              <th scope="col">{t("after")}</th>
            </tr>
          </thead>
          <tbody>
            {page.rows.map((row) => (
              <tr
                key={row.chainIndex}
                className="border-b border-slate-100 align-top dark:border-slate-800"
              >
                <td className="text-right font-mono text-xs tabular-nums">
                  {row.chainIndex}
                </td>
                <td className="whitespace-nowrap text-xs">
                  {new Date(row.at).toLocaleString(FORMAT_LOCALE)}
                </td>
                <td className="text-xs">
                  {/* A background job has no actor. Rendering it as "system"
                      rather than blank distinguishes "nobody did this" from
                      "we failed to record who". */}
                  {row.actorName ?? t("systemActor")}
                </td>
                <td>{row.action}</td>
                <td className="text-xs">
                  {row.resourceType}
                  {row.resourceId ? (
                    <span className="block font-mono text-muted">
                      {row.resourceId.slice(0, 8)}
                    </span>
                  ) : null}
                </td>
                <td className="max-w-[14rem] break-words font-mono text-xs text-muted">
                  {row.before ? JSON.stringify(row.before) : "—"}
                </td>
                <td className="max-w-[14rem] break-words font-mono text-xs">
                  {row.after ? JSON.stringify(row.after) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
