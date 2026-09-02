"use client";

import { useEffect, useState } from "react";

/**
 * A clock that ticks, for elapsed counters and the header's time.
 *
 * Starts from the server's timestamp so the first client render matches the
 * HTML it hydrates, then follows the browser's clock.
 */
export function useNow(initialIso: string, everyMs: number): Date {
  const [now, setNow] = useState(() => new Date(initialIso));
  useEffect(() => {
    setNow(new Date());
    const timer = window.setInterval(() => setNow(new Date()), everyMs);
    return () => window.clearInterval(timer);
  }, [everyMs]);
  return now;
}
