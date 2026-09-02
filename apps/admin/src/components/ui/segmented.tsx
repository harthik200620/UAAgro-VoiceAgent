"use client";

import { clsx } from "clsx";

/** The 1 · 5 · 10 · 20 · 30 control: a radio group drawn as one bar. */
export function Segmented<T extends string | number>({
  label,
  options,
  value,
  onChange,
  disabled = false,
}: {
  label: string;
  options: readonly { value: T; label: string }[];
  value: T;
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className="inline-flex overflow-hidden rounded-btn border border-line bg-surface"
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={String(option.value)}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={disabled}
            onClick={() => onChange(option.value)}
            className={clsx(
              "min-h-[40px] min-w-[40px] px-3 text-small font-medium transition-colors disabled:cursor-not-allowed",
              selected ? "bg-ink font-semibold text-paper" : "text-ink hover:bg-inset",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
