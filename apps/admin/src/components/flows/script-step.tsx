import type { ReactNode } from "react";

import { Icon } from "@/components/ui/icon";

/** One numbered step of a script: the ink circle, a title, the text, a line of help under it. */
export function ScriptStep({
  number,
  title,
  lockedLabel,
  hint,
  children,
}: {
  number: number;
  title: string;
  /** Present on the steps the law fixes: shown with a lock, never editable. */
  lockedLabel?: string;
  hint?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex gap-4">
      <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ink text-small font-semibold text-paper">
        {number}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-center justify-between gap-2.5">
          <span className="font-semibold">{title}</span>
          {lockedLabel ? (
            <span className="inline-flex items-center gap-1.5 text-meta text-muted">
              <Icon name="lock" size={12} />
              {lockedLabel}
            </span>
          ) : null}
        </div>
        {children}
        {hint ? <div className="text-label text-muted">{hint}</div> : null}
      </div>
    </div>
  );
}

const boxClass = "w-full rounded-panel border border-line px-3.5 py-3 text-prose leading-[1.7]";

/** A script line the operator may change. Grows with its text. */
export function ScriptText({
  value,
  onChange,
  label,
}: {
  value: string;
  onChange: (value: string) => void;
  label: string;
}) {
  return (
    <textarea
      lang="hi"
      aria-label={label}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      rows={Math.max(2, Math.ceil(value.length / 70))}
      className={`${boxClass} resize-y bg-surface text-ink focus:border-ink focus:outline-none`}
    />
  );
}

/** A script line fixed by law or by the platform: shown on inset, not editable. */
export function LockedText({ value }: { value: string }) {
  return (
    <div lang="hi" className={`${boxClass} min-h-[50px] bg-inset text-muted`}>
      {value}
    </div>
  );
}
