"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * A page that re-reads one JSON URL on a timer.
 *
 * The first render uses what the server fetched, so nothing flashes; after
 * that the panel's own route handler is asked again every `everyMs`, and on
 * the tab coming back into view. A failed refresh keeps the last good data
 * on screen and says so through `stale`, because a manager glancing at a
 * dashboard needs to know whether the figures are current far more than
 * they need an error dialog.
 */

export type Polled<T> = {
  data: T;
  /** When the data on screen was last fetched successfully; null until the first refresh. */
  refreshedAt: Date | null;
  /** The last refresh failed; `data` is from before it. */
  stale: boolean;
  refresh: () => void;
};

export function usePolling<T>(url: string, initial: T, everyMs: number): Polled<T> {
  const [data, setData] = useState(initial);
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  const [stale, setStale] = useState(false);
  const inFlight = useRef(false);

  const refresh = useCallback(() => {
    if (inFlight.current || document.hidden) return;
    inFlight.current = true;
    fetch(url, { cache: "no-store", headers: { accept: "application/json" } })
      .then(async (response) => {
        if (!response.ok) throw new Error(String(response.status));
        setData((await response.json()) as T);
        setRefreshedAt(new Date());
        setStale(false);
      })
      .catch(() => setStale(true))
      .finally(() => {
        inFlight.current = false;
      });
  }, [url]);

  useEffect(() => {
    const timer = window.setInterval(refresh, everyMs);
    const onVisible = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh, everyMs]);

  return { data, refreshedAt, stale, refresh };
}
