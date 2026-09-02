import { getTranslations, setRequestLocale } from "next-intl/server";

import { FORMAT_LOCALE } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getKbDocuments } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Knowledge Base -- documents and their embedding state.
 *
 * The two counts are shown apart on purpose. `uaagro-kb ingest` completes
 * BM25-only when the embedding model is unavailable, and those chunks retrieve
 * far worse without ever looking broken: a document showing 40 chunks and 0
 * embedded is the whole explanation for "the agent cannot find this", and it
 * is invisible if the screen shows one number.
 *
 * The upload flow and the retrieval playground §15.1 asks for are not built.
 * They are listed in the panel's known gaps rather than stubbed, because a
 * playground that showed plausible-looking rankings without running the real
 * hybrid retrieval would be worse than none -- it is precisely the screen a
 * non-engineer would trust.
 */
export default async function KnowledgePage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("knowledge");

  if (!can(session, "knowledge.upload")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const rows = await getKbDocuments(session!);
  const unembedded = rows.filter(
    (row) => row.chunkCount > 0 && row.embeddedCount < row.chunkCount,
  );
  const unpublished = rows.filter((row) => !row.isPublished);

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>

      {unembedded.length > 0 ? (
        <div
          role="status"
          className="mb-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm"
        >
          <p className="font-medium text-warn">
            {t("missingVectors", { count: unembedded.length })}
          </p>
          <p className="mt-1 text-xs text-muted">{t("missingVectorsDetail")}</p>
        </div>
      ) : null}

      {unpublished.length > 0 ? (
        <p className="mb-3 text-sm text-muted">
          {t("unpublished", { count: unpublished.length })}
        </p>
      ) : null}

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[48rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("document")}</th>
              <th scope="col">{t("source")}</th>
              <th scope="col">{t("language")}</th>
              <th scope="col">{t("version")}</th>
              <th scope="col" className="text-right">{t("chunks")}</th>
              <th scope="col" className="text-right">{t("embedded")}</th>
              <th scope="col">{t("state")}</th>
              <th scope="col">{t("updated")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const partial =
                row.chunkCount > 0 && row.embeddedCount < row.chunkCount;
              return (
                <tr
                  key={row.id}
                  className="border-b border-slate-100 dark:border-slate-800"
                >
                  <td lang="hi">{row.title}</td>
                  <td className="text-xs">{row.sourceType}</td>
                  <td>{row.language}</td>
                  <td className="tabular-nums">v{row.version}</td>
                  <td className="text-right tabular-nums">{row.chunkCount}</td>
                  <td
                    className={`text-right tabular-nums ${partial ? "text-warn" : ""}`}
                  >
                    {row.embeddedCount}
                  </td>
                  <td>
                    {/* §9: nothing unpublished reaches a caller. Shown as a
                        state rather than an error -- draft is where a document
                        is supposed to sit until an agronomist signs for it. */}
                    <span className={row.isPublished ? "text-ok" : "text-muted"}>
                      {row.isPublished ? t("published") : t("draft")}
                    </span>
                  </td>
                  <td className="whitespace-nowrap text-xs text-muted">
                    {new Date(row.updatedAt).toLocaleDateString(FORMAT_LOCALE)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t("noDocuments")}</p>
      ) : null}
    </section>
  );
}
