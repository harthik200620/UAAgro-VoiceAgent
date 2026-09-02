import { clsx } from "clsx";

/**
 * The only form a farmer's number ever takes on screen: three dots and four
 * digits, in the mono face. The API has no field for more.
 */
export function Last4({ value, className }: { value: string | null; className?: string }) {
  return (
    <span className={clsx("font-mono text-muted", className)}>{value ? `···${value}` : "—"}</span>
  );
}
