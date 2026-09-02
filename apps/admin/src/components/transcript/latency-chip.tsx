import { useTranslations } from "next-intl";

import { Icon } from "@/components/ui/icon";
import type { TurnLatency } from "@/lib/contract";
import { formatMs } from "@/lib/format";

/**
 * "612 ms · STT 86 · LLM 258 · TTS 102" under an agent line. A scripted line
 * served from cache says so instead of listing parts it never went through.
 */
export function LatencyChip({
  latency,
  isFirstReply,
}: {
  latency: TurnLatency;
  isFirstReply: boolean;
}) {
  const t = useTranslations("transcript");

  const parts: string[] = [];
  if (latency.fromCache) {
    parts.push(t("fromCache"));
  } else {
    if (latency.sttMs !== undefined) parts.push(`STT ${Math.round(latency.sttMs)}`);
    if (latency.llmMs !== undefined) parts.push(`LLM ${Math.round(latency.llmMs)}`);
    if (latency.ttsMs !== undefined) parts.push(`TTS ${Math.round(latency.ttsMs)}`);
    if (isFirstReply) parts.push(t("firstReply"));
  }

  return (
    <span className="inline-flex items-center gap-1.5 rounded-tag bg-inset px-2 py-0.5 font-mono text-meta text-muted">
      <Icon name="clock" size={12} />
      {formatMs(latency.totalMs)}
      {parts.length > 0 ? <span className="text-faint">· {parts.join(" · ")}</span> : null}
    </span>
  );
}
