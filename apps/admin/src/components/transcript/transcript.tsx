import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { Icon, type IconName } from "@/components/ui/icon";
import type { Activity, TurnEvent } from "@/lib/contract";
import { formatClock, formatMs } from "@/lib/format";

import { LatencyChip } from "./latency-chip";

/**
 * A conversation, turn by turn, the same on the Live page and on a finished
 * call: a timestamp from the start of the call, who spoke, what they said,
 * and under each agent line how long the farmer waited to hear it.
 *
 * Everything spoken is marked `lang="hi"` -- it is the farmer's and the
 * agent's Hindi, not the panel's -- and the events between turns (a key
 * pressed, a WhatsApp sent, a hand-over) sit in the same column so the order
 * of what happened reads top to bottom.
 */
export type TranscriptItem =
  | { kind: "turn"; turn: TurnEvent }
  | { kind: "event"; at: number | null; type: string; text: string };

const EVENT_ICONS: Record<string, IconName> = {
  dtmf: "keypad",
  whatsapp_sent: "whatsapp",
  transfer: "transfer",
  ended: "phone",
};

export function Transcript({
  items,
  firstReplyIndex = null,
  live = null,
}: {
  items: TranscriptItem[];
  /** The agent turn to mark as the call's first reply. */
  firstReplyIndex?: number | null;
  /** What the agent is doing right now, on a call still in progress. */
  live?: { activity: Activity; text: string; at: number } | null;
}) {
  const t = useTranslations("transcript");

  return (
    <div className="flex flex-col gap-3.5">
      {items.map((item, index) =>
        item.kind === "turn" ? (
          <div key={`turn-${item.turn.turnIndex}`} className="flex items-start gap-3.5">
            <Stamp at={item.turn.at} />
            <Role role={item.turn.role} />
            <div className="min-w-0 flex-1">
              <p lang="hi" className="text-prose">
                {item.turn.text}
              </p>
              {item.turn.role === "agent" && (item.turn.latency || item.turn.tools.length > 0) ? (
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {item.turn.latency ? (
                    <LatencyChip
                      latency={item.turn.latency}
                      isFirstReply={item.turn.turnIndex === firstReplyIndex}
                    />
                  ) : null}
                  {item.turn.tools.map((tool, toolIndex) => (
                    <span
                      key={`${tool.name}-${toolIndex}`}
                      className="inline-flex items-center gap-1.5 rounded-tag border border-dashed border-line px-2 py-0.5 text-meta text-muted"
                    >
                      <Icon name="search" size={12} />
                      {tool.ms === null ? tool.name || "tool" : `${tool.name || "tool"} · ${formatMs(tool.ms)}`}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          </div>
        ) : (
          <div key={`event-${index}`} className="flex items-center gap-3.5">
            <Stamp at={item.at} />
            <div className="flex flex-1 items-center gap-2 rounded-btn bg-inset px-2.5 py-1.5 text-small text-muted">
              <Icon name={EVENT_ICONS[item.type] ?? "clock"} size={14} />
              <span>{item.text}</span>
            </div>
          </div>
        ),
      )}

      {live && live.activity !== "listening" ? (
        <div className="flex items-center gap-3.5" aria-live="polite">
          <Stamp at={live.at} />
          <Role role="agent" />
          <div className="flex items-center gap-2.5 text-body text-muted">
            <span className="inline-flex gap-[3px]" aria-hidden="true">
              {[0, 200, 400].map((delay) => (
                <i
                  key={delay}
                  className="inline-block h-1.5 w-1.5 animate-dots rounded-full bg-ink"
                  style={{ animationDelay: `${delay}ms` }}
                />
              ))}
            </span>
            {live.text}
          </div>
        </div>
      ) : null}

      {items.length === 0 && !live ? (
        <p className="py-6 text-center text-ui text-muted">{t("empty")}</p>
      ) : null}
    </div>
  );
}

function Stamp({ at }: { at: number | null }) {
  return (
    <div className="w-11.5 shrink-0 text-right">
      <span className="font-mono text-meta text-faint">{at === null ? "" : formatClock(at)}</span>
    </div>
  );
}

function Role({ role }: { role: TurnEvent["role"] }) {
  const t = useTranslations("transcript");
  return (
    <div className="w-14 shrink-0">
      <span
        className={clsx(
          "inline-block rounded-tag px-2 py-0.5 text-meta font-semibold",
          role === "agent" ? "bg-ink text-paper" : "bg-inset text-ink",
        )}
      >
        {t(role)}
      </span>
    </div>
  );
}
