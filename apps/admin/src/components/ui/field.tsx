import { clsx } from "clsx";
import type { ReactNode } from "react";

/** One class for every text control, so a select and an input sit level in a row. */
export const inputClass =
  "w-full rounded-btn border border-line bg-surface px-3 py-[9px] text-ui text-ink placeholder:text-faint focus:border-ink focus:outline-none disabled:bg-inset disabled:text-muted";

/** A label wrapping its control: the association is structural, so every input has a name a screen reader can say. */
export function Field({
  label,
  hint,
  children,
  className,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={clsx("flex flex-col gap-[5px]", className)}>
      <span className="text-label text-muted">{label}</span>
      {children}
      {hint ? <span className="text-label text-faint">{hint}</span> : null}
    </label>
  );
}
