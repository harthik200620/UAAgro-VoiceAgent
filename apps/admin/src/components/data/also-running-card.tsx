import { getTranslations } from "next-intl/server";
import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import type { ServiceHealthReport, StorageReport } from "@/lib/contract";
import { formatBytes, formatDate, formatMs } from "@/lib/format";
import { getServiceHealth } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * "Also running": Redis, the recordings store and the backups.
 *
 * The two health chips come from their own request, made here rather than by
 * the page, so the page can render while it is in flight -- a store that is
 * down answers by timing out, and the rest of the Data page has nothing to do
 * with it. `HealthRows` is what the page wraps in `<Suspense>`.
 */
export async function AlsoRunningCard({
  storage,
  health,
}: {
  storage: StorageReport;
  /** Server-rendered rows for the two services, or a placeholder while they load. */
  health: ReactNode;
}) {
  const t = await getTranslations("data.alsoRunning");

  return (
    <Card className="flex flex-col gap-1.5 px-5 py-4.5">
      <div className="mb-1.5 font-semibold">{t("title")}</div>
      {health}
      <Row label={t("backup")}>
        {storage.backups.schedule}
        {storage.backups.lastAt
          ? ` · ${t("lastBackup", { date: formatDate(storage.backups.lastAt), size: formatBytes(storage.backups.lastBytes) })}`
          : ` · ${t("noBackupYet")}`}
      </Row>
    </Card>
  );
}

/** The two probed services. Awaits the health request; render inside `<Suspense>`. */
export async function HealthRows({ region }: { region: string }) {
  const [t, session] = await Promise.all([getTranslations("data.alsoRunning"), currentSession()]);
  if (!session) return null;

  let health: ServiceHealthReport | null = null;
  try {
    health = await getServiceHealth(session);
  } catch {
    // A health check that cannot be made is itself an answer, and not one
    // worth failing the page over.
    health = null;
  }

  const chip = (ok: boolean | undefined) =>
    ok === undefined ? (
      <Chip tone="grey">{t("unknown")}</Chip>
    ) : (
      <Chip tone={ok ? "green" : "red"}>{ok ? t("ok") : t("down")}</Chip>
    );

  return (
    <>
      <Row label={t("redis")}>
        <span className="inline-flex items-center gap-2">
          Redis
          {health?.redis.latencyMs != null ? (
            <span className="font-mono text-small">· {formatMs(health.redis.latencyMs)}</span>
          ) : null}
          {chip(health?.redis.ok)}
        </span>
      </Row>
      <Row label={t("recordings")}>
        <span className="inline-flex items-center gap-2">
          S3 · {region}
          {chip(health?.storage.ok)}
        </span>
      </Row>
    </>
  );
}

/** The same two rows, before the probes have answered. */
export async function HealthRowsPending({ region }: { region: string }) {
  const t = await getTranslations("data.alsoRunning");
  return (
    <>
      <Row label={t("redis")}>
        <span className="inline-flex items-center gap-2">
          Redis
          <Chip tone="grey">{t("checking")}</Chip>
        </span>
      </Row>
      <Row label={t("recordings")}>
        <span className="inline-flex items-center gap-2">
          S3 · {region}
          <Chip tone="grey">{t("checking")}</Chip>
        </span>
      </Row>
    </>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-3 border-b border-inset py-[7px] text-body">
      <span className="text-muted">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  );
}
