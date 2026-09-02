"use client";

import { useEffect } from "react";

import { useRouter } from "@/i18n/routing";

/**
 * Re-renders the page from the server every ten seconds while a document is
 * still being indexed, and stops the moment none is. The list itself stays a
 * Server Component; this is the only script the polling needs.
 */
export function RefreshWhilePending({ active }: { active: boolean }) {
  const router = useRouter();
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => router.refresh(), 10_000);
    return () => window.clearInterval(timer);
  }, [active, router]);
  return null;
}
