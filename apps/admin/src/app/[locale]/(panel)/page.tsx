import { LiveBoard } from "@/components/live/live-board";
import { Forbidden } from "@/components/ui/forbidden";
import { can } from "@/lib/rbac";
import { getLiveSnapshot } from "@/server/api";
import { currentSession } from "@/server/session";

/** Never cached and never prerendered: a cached live view is a contradiction. */
export const dynamic = "force-dynamic";

/**
 * Live -- the home screen. The snapshot is fetched here, on the server, and
 * handed to the board as plain data; from then on the board follows the
 * event stream through the panel's own `/api/events/live`.
 */
export default async function LivePage() {
  const session = await currentSession();
  if (!session || !can(session, "calls.view")) return <Forbidden />;

  const snapshot = await getLiveSnapshot(session);

  return (
    <LiveBoard
      snapshot={snapshot}
      canIntervene={can(session, "calls.intervene")}
      renderedAt={new Date().toISOString()}
    />
  );
}
