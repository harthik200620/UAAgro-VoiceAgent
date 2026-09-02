"use client";

import { useState, useTransition } from "react";

import { toggleDnc } from "@/app/actions/farmers";

/**
 * Marking a farmer do-not-call, or un-marking them (§15.1, §18).
 *
 * Optimistic in one direction only. Turning the block *on* renders
 * immediately, because the worst case is a call not placed. Turning it off
 * waits for the server, because the worst case there is a screen that says a
 * farmer may be called when the write failed and they may not.
 */
export function DncToggle({
  farmerId,
  isDnc,
  onLabel,
  offLabel,
}: {
  farmerId: string;
  isDnc: boolean;
  onLabel: string;
  offLabel: string;
}) {
  const [current, setCurrent] = useState(isDnc);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <span className="inline-flex flex-col gap-0.5">
      <button
        type="button"
        disabled={pending}
        aria-pressed={current}
        onClick={() =>
          startTransition(async () => {
            setError(null);
            const next = !current;
            if (next) setCurrent(true);
            const result = await toggleDnc(farmerId, next);
            if (result.ok) setCurrent(result.isDnc);
            else {
              setCurrent(isDnc);
              setError(result.message);
            }
          })
        }
        className={`rounded border px-2 py-0.5 text-xs disabled:opacity-40 ${
          current
            ? "border-warn/50 text-warn"
            : "border-slate-300 text-muted dark:border-slate-700"
        }`}
      >
        {current ? onLabel : offLabel}
      </button>
      {error ? <span className="text-xs text-danger">{error}</span> : null}
    </span>
  );
}
