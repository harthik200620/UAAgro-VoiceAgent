import "server-only";

import { env } from "./env";
import { currentSession } from "./session";

/**
 * The bridge behind every Route Handler under `/api`.
 *
 * The browser can never hold the API token, yet an `<audio>` element, an
 * `EventSource` and a CSV download all need a URL they can fetch directly.
 * So the panel serves those URLs itself: the handler authenticates the caller
 * from the session cookie, opens the upstream request with the bearer token,
 * and relays the bytes. Nothing is buffered -- a recording is streamed as it
 * arrives and an event stream stays open -- and when the browser goes away
 * the upstream fetch is aborted, so a closed tab does not leave the control
 * plane writing events to nobody.
 */

type ProxyOptions = {
  method?: "GET" | "POST";
  body?: string;
  contentType?: string;
  /** Server-sent events: adds the headers that stop proxies buffering. */
  sse?: boolean;
};

export async function proxyApi(
  request: Request,
  path: string,
  options: ProxyOptions = {},
): Promise<Response> {
  const session = await currentSession();
  if (!session) {
    return Response.json(
      { error: { code: "unauthenticated", message: "Sign in to continue." } },
      { status: 401 },
    );
  }

  const controller = new AbortController();
  request.signal.addEventListener("abort", () => controller.abort());

  const headers = new Headers({ authorization: `Bearer ${session.accessToken}` });
  if (options.contentType) headers.set("content-type", options.contentType);
  if (options.sse) headers.set("accept", "text/event-stream");
  // An `<audio>` element seeks with Range requests; pass them through so the
  // scrubber works without downloading the whole recording first.
  const range = request.headers.get("range");
  if (range) headers.set("range", range);

  let upstream: Response;
  try {
    upstream = await fetch(`${env().apiBaseUrl}${path}`, {
      method: options.method ?? "GET",
      headers,
      ...(options.body === undefined ? {} : { body: options.body }),
      signal: controller.signal,
      cache: "no-store",
    });
  } catch {
    // 499 is nginx's code for "the client hung up first"; nobody reads it.
    if (controller.signal.aborted) return new Response(null, { status: 499 });
    return Response.json(
      { error: { code: "upstream_unreachable", message: "The control plane is not reachable." } },
      { status: 502 },
    );
  }

  const responseHeaders = new Headers({ "cache-control": "no-store" });
  for (const name of [
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "content-disposition",
  ]) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  if (options.sse && upstream.ok) {
    responseHeaders.set("content-type", "text/event-stream; charset=utf-8");
    responseHeaders.set("cache-control", "no-cache, no-transform");
    responseHeaders.set("connection", "keep-alive");
    responseHeaders.set("x-accel-buffering", "no");
  }

  return new Response(
    upstream.body ? relay(upstream.body, () => controller.abort()) : null,
    { status: upstream.status, headers: responseHeaders },
  );
}

/**
 * Pipe one stream into another chunk by chunk. A stream of our own rather
 * than the upstream body handed straight to the Response, so that the
 * consumer cancelling -- the browser closing the connection -- reaches the
 * upstream fetch as an abort.
 */
function relay(
  source: ReadableStream<Uint8Array>,
  abort: () => void,
): ReadableStream<Uint8Array> {
  const reader = source.getReader();
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) controller.close();
      else controller.enqueue(value);
    },
    cancel() {
      abort();
      reader.cancel().catch(() => undefined);
    },
  });
}
