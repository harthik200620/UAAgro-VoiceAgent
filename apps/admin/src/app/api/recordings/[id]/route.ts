import { proxyApi } from "@/server/proxy";

export const dynamic = "force-dynamic";

/**
 * A call's recording, streamed for the `<audio>` element. Range requests
 * pass through so the scrubber seeks without fetching the whole file, and
 * the API's 404 for a call without a recording passes through unchanged.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  return proxyApi(request, `/admin/calls/${encodeURIComponent(id)}/recording`);
}
