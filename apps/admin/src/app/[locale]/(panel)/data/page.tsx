import { getTranslations } from "next-intl/server";
import { Suspense } from "react";

import {
  AlsoRunningCard,
  HealthRows,
  HealthRowsPending,
} from "@/components/data/also-running-card";
import { ConnectionCard } from "@/components/data/connection-card";
import { StorageTable } from "@/components/data/storage-table";
import { Card } from "@/components/ui/card";
import { Forbidden } from "@/components/ui/forbidden";
import { PageHeader } from "@/components/ui/page-header";
import { formatTime } from "@/lib/format";
import { can } from "@/lib/rbac";
import { getConnection, getStorage } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/** Data -- what the agent and the panel keep, and where. Nothing here is edited by hand. */
export default async function DataPage() {
  const session = await currentSession();
  if (!session || !can(session, "data.view")) return <Forbidden />;

  const [t, storage, connection] = await Promise.all([
    getTranslations("data"),
    getStorage(session),
    can(session, "data.connection") ? getConnection(session) : Promise.resolve(null),
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
    </>
  );
}
