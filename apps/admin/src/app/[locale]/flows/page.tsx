import { getTranslations, setRequestLocale } from "next-intl/server";

import { PublishButton } from "@/components/publish-button";
import { Link } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getFlows } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Flows & Prompts -- versioned agent configurations.
 *
 * The banner at the top is the important element, and it is the mirror of the
 * advisory screen's unapproved counter. A flow with no published version means
 * the worker cannot assemble an agent at all: `build_call_pipeline` raises
 * rather than falling back to a default prompt, because an agent speaking
 * words nobody approved is worse than one that hands the caller to a person.
 *
 * That failure is otherwise invisible from inside the panel. Everything looks
 * configured -- the versions are there, the prompts are written -- and the
 * phone simply does not work. So the state is stated here, in the place
 * somebody would go to fix it.
 *
 * The list carries no prompt text (§1 N6). Opening a version fetches its body.
 */
export default async function FlowsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("flows");

  if (!can(session, "flows.edit")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const rows = await getFlows(session!);
  const mayPublish = can(session, "flows.publish");

  const byFlow = new Map<string, typeof rows>();
  for (const row of rows) {
    byFlow.set(row.flowType, [...(byFlow.get(row.flowType) ?? []), row]);
  }
  const unpublished = [...byFlow.entries()].filter(
    ([, versions]) => !versions.some((v) => v.isPublished),
  );

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>

      {unpublished.length > 0 ? (
        <div
          role="status"
          className="mb-4 rounded border border-danger/40 bg-danger/10 px-3 py-2 text-sm"
        >
          <p className="font-medium text-danger">
            {t("noPublished", {
              flows: unpublished.map(([flowType]) => flowType).join(", "),
            })}
          </p>
          <p className="mt-1 text-xs text-muted">{t("noPublishedDetail")}</p>
        </div>
      ) : null}

      {[...byFlow.entries()].map(([flowType, versions]) => (
        <div key={flowType} className="mb-6">
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-muted">
            {t(`flowTypes.${flowType}`)}
          </h2>
          <div className="overflow-x-auto">
            <table className="grid-dense w-full min-w-[44rem] border-collapse text-sm">
              <thead className="border-b border-slate-300 text-left dark:border-slate-700">
                <tr>
                  <th scope="col">{t("version")}</th>
                  <th scope="col">{t("name")}</th>
                  <th scope="col">{t("state")}</th>
                  <th scope="col">{t("publishedBy")}</th>
                  <th scope="col">{t("tools")}</th>
                  <th scope="col">{t("changelog")}</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {versions.map((row) => (
                  <tr
                    key={row.id}
                    className="border-b border-slate-100 dark:border-slate-800"
                  >
                    <td className="tabular-nums">
                      <Link
                        href={`/flows/${row.id}`}
                        className="underline underline-offset-2"
                      >
                        v{row.version}
                      </Link>
                    </td>
                    <td>{row.name}</td>
                    <td>
                      <span className={row.isPublished ? "text-ok" : "text-muted"}>
                        {row.isPublished ? t("live") : t("draft")}
                      </span>
                    </td>
                    <td className="text-xs text-muted">
                      {row.publishedByName ?? "—"}
                    </td>
                    <td className="tabular-nums">{row.toolAllowlist.length}</td>
                    <td className="max-w-[20rem] truncate text-xs text-muted">
                      {row.changelog ?? "—"}
                    </td>
                    <td>
                      {!row.isPublished && mayPublish ? (
                        <PublishButton
                          configId={row.id}
                          label={t("publish")}
                          confirmLabel={t("confirmPublish")}
                          cancelLabel={t("cancel")}
                          confirmQuestion={t("publishQuestion", {
                            version: row.version,
                          })}
                        />
                      ) : null}
                      {/* §15.1: "instant rollback to any prior version is one
                          click". Rolling back *is* publishing the older
                          version, so it is the same control rather than a
                          second one that could drift from it. */}
                      {!row.isPublished && mayPublish && row.publishedAt ? (
                        <span className="ml-1 text-xs text-muted">
                          {t("rollbackHint")}
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}

      {!mayPublish ? (
        <p className="text-sm text-muted">{t("cannotPublish")}</p>
      ) : null}
    </section>
  );
}
