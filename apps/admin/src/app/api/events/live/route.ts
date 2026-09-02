import { proxyApi } from "@/server/proxy";

/** A stream is never static. */
export const dynamic = "force-dynamic";

/** The Live page's event stream, relayed with the session's bearer token. */
export function GET(request: Request): Promise<Response> {
  return proxyApi(request, "/admin/live/events", { sse: true });
}
