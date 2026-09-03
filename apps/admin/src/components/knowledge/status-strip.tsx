import { getTranslations } from "next-intl/server";

import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { NotAvailable } from "@/components/ui/not-available";
import type { KnowledgeStatus } from "@/lib/contract";
import { formatCount, formatDateTime, formatMs } from "@/lib/format";
import type { Loaded } from "@/server/load";

/**
 * One line across the top of Knowledge: whether the indexing worker is
 * alive, how many documents are in which state, how much is searchable,
 * and how fast the last search was. Without the status route the strip
 * says so in a sentence and the page goes on.
 */
export async function StatusStrip({ status }: { status: Loaded<KnowledgeStatus> }) {
  const t = await getTranslations("knowledge.health");
  if (!status.ok) return <NotAvailable compact reason={status.reason} message={status.message} />;

  const { worker, documents } = status.value;
  return (
    <Card className="flex flex-wrap items-center gap-x-6 gap-y-2 px-5.5 py-3.5 text-small">
      <span className="inline-flex items-center gap-2">
        <Chip tone={worker.alive ? "green" : "red"} pulse={worker.alive}>
          {worker.alive ? t("workerAlive") : t("workerDown")}
        </Chip>
        <span className="text-muted">
          {worker.lastSeenAt ? t("lastSeen", { time: formatDateTime(worker.lastSeenAt) }) : t("neverSeen")}
        </span>
      </span>
      <Figure label={t("indexed")} value={formatCount(documents.indexed)} />
      <Figure label={t("pending")} value={formatCount(documents.pending + documents.indexing)} />
      <Figure label={t("failed")} value={formatCount(documents.failed)} warn={documents.failed > 0} />
      <Figure
        label={t("pieces")}
        value={`${formatCount(status.value.embedded)} / ${formatCount(status.value.chunks)}`}
      />
      <Figure label={t("model")} value={status.value.embeddingModel} />
      <Figure label={t("retrieval")} value={formatMs(status.value.retrievalMs)} />
    </Card>
  );
}

function Figure({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return (
    <span className="inline-flex items-baseline gap-1.5">
      <span className="text-muted">{label}</span>
      <span className={warn ? "font-mono text-red-text" : "font-mono text-ink"}>{value}</span>
    </span>
  );
}
