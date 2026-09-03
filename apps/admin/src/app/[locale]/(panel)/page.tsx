import { getTranslations } from "next-intl/server";

import { OverviewBoard } from "@/components/overview/overview-board";
import { RangeSwitch } from "@/components/overview/range-switch";
import { Forbidden } from "@/components/ui/forbidden";
import { NotAvailable } from "@/components/ui/not-available";
import { PageHeader } from "@/components/ui/page-header";
import type { OverviewRange } from "@/lib/contract";
import { can } from "@/lib/rbac";
import { getOverview } from "@/server/api";
import { load } from "@/server/load";
import { currentSession } from "@/server/session";

/** Never cached and never prerendered: the figures are the point. */
export const dynamic = "force-dynamic";

function asRange(value: string | string[] | undefined): OverviewRange {
  return value === "7d" || value === "30d" ? value : "today";
}

/**
 * Overview -- the home screen. The first read happens here on the server so
 * the page arrives drawn; the board keeps it current from the browser through
 * the panel's own route handler, never with a token of its own.
 */
export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const session = await currentSession();
  if (!session || !can(session, "calls.view")) return <Forbidden />;

  const range = asRange((await searchParams).range);
  const overview = await load(getOverview(session, range));

  if (!overview.ok) {
    const t = await getTranslations("overview");
    return (
      <>
        <PageHeader title={t("title")} subtitle={t("subtitle")}>
          <RangeSwitch active={range} />
        </PageHeader>
        <NotAvailable reason={overview.reason} message={overview.message} />
      </>
    );
  }

  // Keyed on the range so a switch starts the board afresh with the new figures.
  return (
    <OverviewBoard
      key={range}
      initial={overview.value}
      range={range}
      renderedAt={new Date().toISOString()}
    />
  );
}
