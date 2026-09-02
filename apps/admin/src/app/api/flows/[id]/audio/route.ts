import { proxyApi } from "@/server/proxy";

export const dynamic = "force-dynamic";

/**
 * "Play in Hindi voice": the text of one script step rendered by the
 * configured voice. A vendor call on the API side, so it is only made on an
 * explicit click, and only the text the operator typed travels.
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;

  let text: unknown;
  try {
    ({ text } = (await request.json()) as { text?: unknown });
  } catch {
    text = undefined;
  }
  if (typeof text !== "string" || text.trim() === "") {
    return Response.json(
      { error: { code: "validation_error", message: "Give the voice something to say." } },
      { status: 422 },
    );
  }

  return proxyApi(request, `/admin/flows/${encodeURIComponent(id)}/audio`, {
    method: "POST",
    body: JSON.stringify({ text }),
    contentType: "application/json",
  });
}
