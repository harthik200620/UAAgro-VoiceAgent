"use client";

import { useEffect, useRef, useState } from "react";

/**
 * One `EventSource` against a panel-owned `/api/events/*` URL.
 *
 * The browser's own reconnect gives up the moment the server answers with a
 * non-200 -- a session that expired, a control plane restarting -- and never
 * tries again, which on a page that is supposed to be live means a manager
 * watching a frozen screen without knowing it. So reconnection is done here,
 * with exponential backoff from one second to thirty, and the status is
 * returned for the page to show.
 *
 * Handlers are read through a ref so a re-render never tears the connection
 * down; only a change of URL or of the set of event names does.
 */

type StreamStatus = "connecting" | "live" | "reconnecting";

type Handlers = Record<string, (data: unknown) => void>;

export function useEventStream(url: string, handlers: Handlers): StreamStatus {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const latest = useRef(handlers);
  useEffect(() => {
    latest.current = handlers;
  });

  const names = Object.keys(handlers).sort().join(",");

  useEffect(() => {
    let source: EventSource | null = null;
    let timer: number | undefined;
    let attempt = 0;
    let closed = false;

    const open = () => {
      source = new EventSource(url);
      for (const name of names.split(",")) {
        source.addEventListener(name, (message: MessageEvent<string>) => {
          let data: unknown;
          try {
            data = JSON.parse(message.data);
          } catch {
            return;
          }
          latest.current[name]?.(data);
        });
      }
      source.onopen = () => {
        attempt = 0;
        setStatus("live");
      };
      source.onerror = () => {
        source?.close();
        if (closed) return;
        setStatus("reconnecting");
        timer = window.setTimeout(open, Math.min(30_000, 1_000 * 2 ** attempt));
        attempt += 1;
      };
    };

    open();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      source?.close();
    };
  }, [url, names]);

  return status;
}
