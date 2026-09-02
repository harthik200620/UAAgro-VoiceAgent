"use server";

import type { CallEvent, TurnEvent } from "@/lib/contract";
import { getCall } from "@/server/api";
import { guard } from "@/server/guard";

/**
 * The transcript so far of a call that began before the page opened.
 *
 * The event stream carries turns from the moment the browser connects; what
 * was said before that lives on the call row. A Server Action rather than a
 * Route Handler because the caller is a component, not an `<audio>` tag,
 * and it gets a typed result instead of a URL.
 */
export async function loadCallHistory(
  callId: string,
): Promise<{ turns: TurnEvent[]; events: CallEvent[] } | null> {
  // The only caller that answers `null` for all three refusals, and rightly:
  // it back-fills a transcript the stream will fill anyway, so there is
  // nothing to tell the operator and nothing to interrupt them with.
  const guarded = await guard("calls.view");
  if (!guarded.ok) return null;
  try {
    const call = await getCall(guarded.session, callId);
    return { turns: call.turns, events: call.events };
  } catch {
    // The live view is not worth breaking for missing history; the turns
    // arriving on the stream still show.
    return null;
  }
}
