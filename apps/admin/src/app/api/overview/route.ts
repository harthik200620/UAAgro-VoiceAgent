import { proxyApi } from "@/server/proxy";

export const dynamic = "force-dynamic";

/**
 * The Overview's refresh, every thirty seconds from the page. The range is
 * checked here rather than passed through: the only three values the API
 * accepts are the only three a URL can carry.
 */
export function GET(request: Request): Promise<Response> {
  const asked = new URL(request.url).searchParams.get("range");
  const range = asked === "7d" || asked === "30d" ? asked : "today";
  return proxyApi(request, `/admin/overview?range=${range}`);
}
