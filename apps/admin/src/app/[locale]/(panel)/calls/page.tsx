import { getTranslations } from "next-intl/server";

import { CallsFilters, type CallFilters } from "@/components/calls/calls-filters";
import { CallsTable } from "@/components/calls/calls-table";
import { Pagination } from "@/components/calls/pagination";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { can } from "@/lib/rbac";
import { getCalls, getCentres, type CallsQuery } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;

/**
 * Calls -- every call, filtered from the URL.
 *
 * The filters are read from the query string rather than held in state, so
 * a filtered view is a link. The centre list only loads for a session that
 * may see centres; the API scopes the calls themselves either way.
 */
export default async function CallsPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const session = await currentSession();
  if (!session || !can(session, "calls.view")) return <Forbidden />;

  const t = await getTranslations("calls");
  const params = await searchParams;
  const one = (key: string): string | undefined => {
    const value = params[key];
    return typeof value === "string" && value ? value : undefined;
  };

  const values: CallFilters = {};
  const query: CallsQuery = { limit: PAGE_SIZE, offset: Math.max(0, Number(one("offset") ?? 0) || 0) };
  const from = one("from");
  const to = one("to");
  const direction = one("direction");
  const outcome = one("outcome");
  const centre = one("centre");
  const q = one("q");
  if (from) values.from = query.from = from;
  if (to) values.to = query.to = to;
  if (direction) values.direction = query.direction = direction;
  if (outcome) values.outcome = query.outcome = outcome;
  if (centre) values.centre = query.centreId = centre;
  if (q) values.q = query.q = q;

  const [calls, centres] = await Promise.all([
    getCalls(session, query),
    can(session, "centres.view") ? getCentres(session) : Promise.resolve([]),
  ]);

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <Card className="px-5 py-4">
        <CallsFilters values={values} centres={centres} />
      </Card>
      <Card className="px-2 pb-1.5 pt-4">
        <div className="flex items-baseline justify-between px-3.5 pb-2">
          <div className="font-semibold">{t("results", { total: calls.total })}</div>
          <Pagination
            total={calls.total}
            offset={query.offset}
            pageSize={PAGE_SIZE}
            query={Object.fromEntries(
              Object.entries(values).filter((entry): entry is [string, string] => Boolean(entry[1])),
            )}
          />
        </div>
        <CallsTable rows={calls.rows} />
      </Card>
    </>
  );
}
