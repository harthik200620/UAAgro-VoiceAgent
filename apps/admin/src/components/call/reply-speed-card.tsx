import { getTranslations } from "next-intl/server";

import { Card } from "@/components/ui/card";
import type { TurnEvent } from "@/lib/contract";
import { formatClock, formatCount, formatMs } from "@/lib/format";
import { LATENCY_SCALE_MS, latencyParts, type LatencyPartKey } from "@/lib/latency";

/** The single-hue ramp, one class per part; never a status colour. */
const PART_CLASS: Record<LatencyPartKey, string> = {
  lineIn: "bg-latency-line",
  turn: "bg-latency-turn",
  stt: "bg-latency-stt",
  think: "bg-latency-think",
  voice: "bg-latency-voice",
  lineOut: "bg-latency-line",
};

/**
 * "Speed of reply": the first reply as a numeral, then every reply the model
 * produced as a stacked bar against a one-second scale, and the first of
 * them broken into its parts. Lines served from cache are left out of the
 * bars -- at 50 ms they would be a sliver, and the sentence above says why.
 */
export async function ReplySpeedCard({
  turns,
  firstReplyMs,
}: {
  turns: TurnEvent[];
  firstReplyMs: number | null;
}) {
  const t = await getTranslations("call.speed");
  const replies = turns.filter(
    (turn) =>
      turn.role === "agent" && turn.latency !== null && turn.latency.totalMs !== null && !turn.latency.fromCache,
  );
  const first = replies[0];

  return (
    <Card className="flex flex-col gap-3.5 px-5.5 pb-5 pt-4.5">
      <div className="text-small font-medium text-muted">{t("title")}</div>
      <div className="flex items-baseline gap-2.5">
        <span className="font-serif text-num-xl">
          {firstReplyMs === null ? "—" : formatCount(Math.round(firstReplyMs))}
        </span>
        <span className="text-lg text-muted">ms</span>
        <span className="ml-auto text-small text-muted">{t("target")}</span>
      </div>
      <p className="text-label text-muted">{t("explainer")}</p>

      {replies.length === 0 ? (
        <p className="text-label text-faint">{t("allCached")}</p>
      ) : (
        <>
          <div className="flex flex-col gap-2">
            {replies.map((turn) => {
              const parts = turn.latency ? latencyParts(turn.latency) : [];
              return (
                <div key={turn.turnIndex} className="flex items-center gap-2.5">
                  <div className="w-[42px] shrink-0 font-mono text-meta text-muted">
                    {formatClock(turn.at)}
                  </div>
                  <div
                    role="img"
                    aria-label={t("barLabel", { at: formatClock(turn.at), ms: formatMs(turn.latency?.totalMs ?? null) })}
                    className="flex h-3.5 flex-1 gap-0.5 overflow-hidden rounded bg-inset"
                  >
                    {parts.map((part, index) => (
                      <div
                        key={`${part.key}-${index}`}
                        className={PART_CLASS[part.key]}
                        style={{ width: `${Math.min(100, (part.ms / LATENCY_SCALE_MS) * 100)}%` }}
                      />
                    ))}
                  </div>
                  <div className="w-14 shrink-0 text-right font-mono text-label">
                    {formatMs(turn.latency?.totalMs ?? null)}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="flex justify-between pl-13 pr-[66px] text-tiny text-faint" aria-hidden="true">
            <span>0</span>
            <span>500 ms</span>
            <span>1 s</span>
          </div>
        </>
      )}

      {first?.latency ? (
        <div className="flex flex-col gap-1.5 border-t border-inset pt-3">
          <div className="text-meta text-faint">{t("byPart", { at: formatClock(first.at) })}</div>
          {latencyParts(first.latency).map((part, index) => (
            <div key={`${part.key}-${index}`} className="flex items-center justify-between text-small">
              <span className="inline-flex items-center gap-2 text-muted">
                <span aria-hidden="true" className={`h-2.5 w-2.5 rounded-[3px] ${PART_CLASS[part.key]}`} />
                {t(`parts.${part.key}`)}
              </span>
              <span className="font-mono text-label">{formatMs(part.ms)}</span>
            </div>
          ))}
        </div>
      ) : null}
    </Card>
  );
}
