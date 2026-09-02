import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { getLiveCalls } from "@/server/api";
import { currentSession } from "@/server/session";

/** Never cached and never prerendered: a cached live view is a contradiction. */
export const dynamic = "force-dynamic";
export const revalidate = 0;

/**
 * §15.1 Live Calls -- every call in flight.
 *
 * Read from the ``calls`` rows the media path writes as it goes (§1 N8),
 * rather than from the worker's memory. The database already knows; asking
 * every media process would mean the panel holding a connection to each one to
 * draw a table.
 *
 * §15.1 also asks for a streaming transcript and for listen, barge-in and
 * force-transfer. None of those are here. Each needs a live channel out of the
 * media path that does not exist yet, and each is an intervention on a call in
 * progress -- the wrong kind of thing to approximate with something that looks
 * similar. The turn count is the honest substitute: it moves while a call is
 * live, so the page shows progress rather than pretending to show speech.
 */
export default async function LiveCallsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("live");

  if (!can(session, "calls.listen")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const { rows, concurrency, capacity } = await getLiveCalls(session!);
  const nearCapacity = capacity > 0 && concurrency / capacity > 0.8;

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className={`mb-4 text-sm ${nearCapacity ? "text-warn" : "text-muted"}`}>
        {t("concurrency", { concurrency, capacity })}
      </p>

      {rows.length === 0 ? (
        <p className="text-sm text-muted">{t("noCalls")}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="grid-dense w-full min-w-[46rem] border-collapse text-sm">
            <thead className="border-b border-slate-300 text-left dark:border-slate-700">
              <tr>
                <th scope="col">{t("callRef")}</th>
                <th scope="col">{t("direction")}</th>
                <th scope="col">{t("caller")}</th>
                <th scope="col">{t("centre")}</th>
                <th scope="col">{t("language")}</th>
                <th scope="col" className="text-right">{t("elapsed")}</th>
                <th scope="col" className="text-right">{t("turns")}</th>
                <th scope="col">{t("intent")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  className="border-b border-slate-100 dark:border-slate-800"
                >
                  <td className="font-mono text-xs">{row.callRef}</td>
                  <td>{row.direction}</td>
                  <td className="font-mono text-xs">
                    {row.callerLast4 ? `••••${row.callerLast4}` : "—"}
                  </td>
                  <td>{row.centreCode ?? "—"}</td>
                  <td>{row.language || "—"}</td>
                  <td className="text-right tabular-nums">
                    {Math.floor(row.elapsedSeconds / 60)}:
                    {String(row.elapsedSeconds % 60).padStart(2, "0")}
                  </td>
                  <td className="text-right tabular-nums">{row.turnCount}</td>
                  <td className="text-xs">{row.lastIntent ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-4 text-xs text-muted">{t("interventionsUnavailable")}</p>
    </section>
  );
}
