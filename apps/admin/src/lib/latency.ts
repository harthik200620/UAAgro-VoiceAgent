import type { TurnLatency } from "./contract";

/**
 * One agent reply, split into the parts the "speed of reply" card draws.
 *
 * The order is the order the audio travels: into the phone line, the turn
 * detector deciding the farmer has finished, speech to text, the model,
 * the voice, and back out over the line. The API reports the phone line as a
 * single `networkMs`, so it is shown as equal halves at each end -- the
 * split is presentation, the total is the API's.
 */

export type LatencyPartKey = "lineIn" | "turn" | "stt" | "think" | "voice" | "lineOut";

type LatencyPart = { key: LatencyPartKey; ms: number };

/** The bar's full width. A reply under this is the product target. */
export const LATENCY_SCALE_MS = 1000;

export function latencyParts(latency: TurnLatency): LatencyPart[] {
  const half = (latency.networkMs ?? 0) / 2;
  const parts: LatencyPart[] = [
    { key: "lineIn", ms: half },
    { key: "turn", ms: latency.turnMs ?? 0 },
    { key: "stt", ms: latency.sttMs ?? 0 },
    { key: "think", ms: latency.llmMs ?? 0 },
    { key: "voice", ms: latency.ttsMs ?? 0 },
    { key: "lineOut", ms: half },
  ];
  return parts.filter((part) => part.ms > 0);
}
