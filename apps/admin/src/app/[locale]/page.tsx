import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { getDashboard } from "@/server/api";
import { currentSession } from "@/server/session";

/** One dashboard tile. §15.1: every tile drills through to a filtered list. */
function Tile({
  label,
  value,
  sub,
  tone = "normal",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "normal" | "warn" | "danger";
}) {
  const toneClass =
    tone === "danger" ? "text-danger" : tone === "warn" ? "text-warn" : "";
  return (
    <div className="rounded border border-slate-200 p-3 dark:border-slate-800">
      <p className="text-xs text-muted">{label}</p>
      <p className={`text-xl font-semibold tabular-nums ${toneClass}`}>{value}</p>
      {sub ? <p className="text-xs text-muted">{sub}</p> : null}
    </div>
  );
}

export default async function DashboardPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("dashboard");
  const errors = await getTranslations("errors");

  if (!can(session, "calls.view")) {
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const m = await getDashboard(session!, "today");
  const pct = (value: number) => `${Math.round(value * 100)}%`;

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Tile label={t("inboundCalls")} value={String(m.inboundCalls)} />
        <Tile label={t("outboundCalls")} value={String(m.outboundCalls)} />
        <Tile label={t("answerRate")} value={pct(m.answerRate)} />
        <Tile
          label={t("avgDuration")}
          value={`${Math.round(m.avgDurationSeconds)}s`}
        />
        <Tile label={t("resolutionRate")} value={pct(m.resolutionRate)} />
        {/* Containment and transfer are the pair an operator judges the agent
            by, so they sit next to each other rather than in tile order. */}
        <Tile label={t("containmentRate")} value={pct(m.containmentRate)} />
        <Tile label={t("transferRate")} value={pct(m.transferRate)} />
        <Tile
          label={t("liveConcurrency")}
          value={`${m.liveConcurrency}`}
          sub={`${m.concurrencyCapacity} ${t("capacity")}`}
          tone={
            m.liveConcurrency > m.concurrencyCapacity * 0.9 ? "danger" : "normal"
          }
        />
        <Tile
          label={t("spendToday")}
          value={`₹${m.spendTodayRupees.toFixed(0)}`}
          sub={`₹${m.budgetRupees.toFixed(0)} ${t("budget")}`}
          tone={m.spendTodayRupees > m.budgetRupees * 0.8 ? "warn" : "normal"}
        />
        <Tile
          label={t("latency")}
          value={`${m.latencyP95Ms} ms`}
          sub={`p50 ${m.latencyP50Ms} ms`}
          // §7's ceiling is a build-failing gate, so the dashboard shows a
          // breach as a breach rather than as a slightly larger number.
          tone={m.latencyP95Ms > 1500 ? "danger" : "normal"}
        />
        <Tile
          label={t("unhandledIntents")}
          value={String(m.unhandledIntents)}
          // §11.2: these accumulate so the operator can add answers. A rising
          // count is the clearest signal the agent is meeting questions nobody
          // anticipated, which is useful rather than alarming.
          tone={m.unhandledIntents > 0 ? "warn" : "normal"}
        />
        <Tile
          label={t("failedCalls")}
          value={String(m.failedCalls)}
          tone={m.failedCalls > 0 ? "danger" : "normal"}
        />
      </div>
    </section>
  );
}
