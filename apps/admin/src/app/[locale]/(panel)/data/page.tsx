import { getTranslations } from "next-intl/server";
import { Suspense } from "react";

import {
  AlsoRunningCard,
  HealthRows,
  HealthRowsPending,
} from "@/components/data/also-running-card";
import { ConnectionCard } from "@/components/data/connection-card";
import { SourcesPanel } from "@/components/data/sources-panel";
import { StorageTable } from "@/components/data/storage-table";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { NotAvailable } from "@/components/ui/not-available";
import { PageHeader } from "@/components/ui/page-header";
import { formatTime } from "@/lib/format";
import { can } from "@/lib/rbac";
import { getConnection, getDataSources, getStorage } from "@/server/api";
import { load } from "@/server/load";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * Data -- what the agent and the panel keep, where, and the client's own
 * database as a source for stores, products and stock. The platform's
 * tables are read-only here; the MySQL sources are the one thing on the
 * page that is set up by hand, and only by the super_admin.
 */
export default async function DataPage() {
  const session = await currentSession();
  if (!session || !can(session, "data.view")) return <Forbidden />;

  const [t, storage, connection, sources] = await Promise.all([
    getTranslations("data"),
    getStorage(session),
    can(session, "data.connection") ? getConnection(session) : Promise.resolve(null),
    load(getDataSources(session)),
  ]);

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")} />
      <div className="flex items-start gap-4">
        <Card className="min-w-0 flex-1 px-2 pb-2 pt-4">
          <div className="flex items-center justify-between px-3.5 pb-2.5">
            <span className="font-semibold">{t("storage.title")}</span>
            <span className="text-small text-muted">
              {t("storage.countedAt", { time: formatTime(storage.countedAt) })}
            </span>
          </div>
          <StorageTable storage={storage} />
        </Card>
        <div className="flex w-[380px] shrink-0 flex-col gap-4">
          {connection ? <ConnectionCard connection={connection} /> : null}
          {/* The two service probes are the only thing here that waits on
              something outside the panel, so the page does not wait with
              them: it renders, and the chips arrive when they answer. */}
          <AlsoRunningCard
            storage={storage}
            health={
              <Suspense fallback={<HealthRowsPending region={storage.recordings.region} />}>
                <HealthRows region={storage.recordings.region} />
              </Suspense>
            }
          />
        </div>
      </div>

      <section aria-labelledby="sources-title" className="flex flex-col gap-4">
        <div>
          <h2 id="sources-title" className="text-md font-semibold">
            {t("sources.title")}
          </h2>
          <p className="mt-1 max-w-[760px] text-ui text-muted">{t("sources.explainer")}</p>
        </div>
        {sources.ok ? (
          <SourcesPanel sources={sources.value} canWrite={can(session, "data.sources")} />
        ) : (
          <NotAvailable compact reason={sources.reason} message={sources.message} />
        )}
      </section>
    </>
  );
}
