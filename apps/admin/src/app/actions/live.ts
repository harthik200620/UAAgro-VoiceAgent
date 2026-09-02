"use server";

import type { CallEvent, TurnEvent } from "@/lib/contract";
import { can } from "@/lib/rbac";
import { getCall } from "@/server/api";
import { currentSession } from "@/server/session";

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
  const session = await currentSession();
  if (!session || !can(session, "calls.view")) return null;
  try {
    const call = await getCall(session, callId);
    return { turns: call.turns, events: call.events };
  } catch {
    // The live view is not worth breaking for missing history; the turns
    // arriving on the stream still show.
    return null;
  }
}
