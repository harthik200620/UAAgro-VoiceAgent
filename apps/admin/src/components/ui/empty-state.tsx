import { clsx } from "clsx";
import type { ReactNode } from "react";

/** What an empty list says instead of nothing. Every list has one. */
export function EmptyState({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p className={clsx("px-4 py-10 text-center text-ui text-muted", className)}>{children}</p>
  );
}
