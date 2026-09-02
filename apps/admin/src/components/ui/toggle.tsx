"use client";

import { clsx } from "clsx";

/**
 * The 34x20 switch of the centres table. A real `role="switch"` button, so it
 * is keyboard-operable and announces its state; the invisible `::after` box
 * gives it a 40px hit target.
 */
export function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={clsx(
        "relative inline-flex h-5 w-[34px] shrink-0 rounded-full transition-colors after:absolute after:-inset-2.5 after:content-[''] disabled:opacity-50",
        checked ? "bg-green" : "bg-grey",
      )}
    >
      <span
        aria-hidden="true"
        className={clsx(
          "absolute top-0.5 h-4 w-4 rounded-full bg-surface transition-[left]",
          checked ? "left-4" : "left-0.5",
        )}
      />
    </button>
  );
}
