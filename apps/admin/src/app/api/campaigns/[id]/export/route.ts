import { proxyApi } from "@/server/proxy";

export const dynamic = "force-dynamic";

/** The campaign's results as CSV -- name, last4, status, outcome, dtmf, attempts, callId. Never a full number. */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  return proxyApi(request, `/admin/campaigns/${encodeURIComponent(id)}/export.csv`);
}
