import { proxyApi } from "@/server/proxy";

export const dynamic = "force-dynamic";

/** One campaign's event stream: contacts and counts as they change. */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  return proxyApi(request, `/admin/campaigns/${encodeURIComponent(id)}/events`, { sse: true });
}
