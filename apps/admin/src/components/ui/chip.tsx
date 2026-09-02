import { clsx } from "clsx";
import type { ReactNode } from "react";

import type { Tone } from "@/lib/tones";

/**
 * A status pill: a 7px dot and a word. The word is not optional -- the tone
 * is emphasis, the label is the meaning, and a colour-blind manager reads
 * the same thing everyone else does.
 */
const TONE: Record<Tone, { chip: string; dot: string }> = {
  amber: { chip: "bg-amber-bg text-amber-text", dot: "bg-amber" },
  green: { chip: "bg-green-bg text-green-text", dot: "bg-green" },
  red: { chip: "bg-red-bg text-red-text", dot: "bg-red" },
  grey: { chip: "bg-inset text-muted", dot: "bg-grey" },
  ink: { chip: "bg-ink text-paper", dot: "bg-paper" },
};

export function Chip({
  tone,
  children,
  pulse = false,
  size = "sm",
  className,
}: {
  tone: Tone;
  children: ReactNode;
  /** The amber ring of a call in progress. */
  pulse?: boolean;
  size?: "sm" | "lg";
  className?: string;
}) {
  return (
    <span
      className={clsx(
        "inline-flex items-center whitespace-nowrap rounded-full font-medium",
        size === "lg" ? "gap-2 px-3 py-1.5 text-body font-semibold" : "gap-1.5 px-[9px] py-[3px] text-label",
        TONE[tone].chip,
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={clsx(
          "shrink-0 rounded-full",
          size === "lg" ? "h-2 w-2" : "h-[7px] w-[7px]",
          TONE[tone].dot,
          pulse && "animate-live",
        )}
      />
      {children}
    </span>
  );
}
