import { LiveBoard } from "@/components/live/live-board";
import { Forbidden } from "@/components/ui/forbidden";
import { can } from "@/lib/rbac";
import { getLiveSnapshot, getTelephony } from "@/server/api";
import { optional } from "@/server/load";
import { currentSession } from "@/server/session";

/** Never cached and never prerendered: a cached live view is a contradiction. */
export const dynamic = "force-dynamic";

/**
 * Live -- the calls on the phones right now. The snapshot is fetched here, on
 * the server, and handed to the board as plain data; from then on the board
 * follows the event stream through the panel's own `/api/events/live`. The
 * telephony status is optional: without it the board only lacks the link to
 * the browser test page.
 */
export default async function LivePage() {
  const session = await currentSession();
  if (!session || !can(session, "calls.view")) return <Forbidden />;

  const [snapshot, telephony] = await Promise.all([
    getLiveSnapshot(session),
    optional(getTelephony(session)),
  ]);

  return (
    <LiveBoard
      snapshot={snapshot}
      telephony={telephony}
      canIntervene={can(session, "calls.intervene")}
      renderedAt={new Date().toISOString()}
    />
  );
}
