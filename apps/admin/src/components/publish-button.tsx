"use client";

import { useState, useTransition } from "react";

import { publish } from "@/app/actions/flows";

/**
 * Publishing one agent configuration version (§15.1).
 *
 * Confirms first. Publish changes what the agent says to every caller from the
 * next call onwards, and unlike most actions in this panel there is no queue
 * or review between the click and a farmer hearing the result. The confirm
 * step is not ceremony -- it is the only gap.
 *
 * The question arrives already translated *and* already interpolated. An
 * earlier version took the raw string and substituted the version number
 * here, which meant next-intl was asked to render a message with an
 * unfilled `{version}` and threw a formatting error on every load of this
 * screen.
 */
export function PublishButton({
  configId,
  label,
  confirmLabel,
  cancelLabel,
  confirmQuestion,
}: {
  configId: string;
  label: string;
  confirmLabel: string;
  cancelLabel: string;
  confirmQuestion: string;
}) {
  const [pending, startTransition] = useTransition();
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (asking) {
    return (
      <span className="inline-flex flex-col gap-1">
        <span className="text-xs">{confirmQuestion}</span>
        <span className="flex gap-1">
          <button
            type="button"
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                setError(null);
                const result = await publish(configId);
                if (result.ok) setAsking(false);
                else setError(result.message);
              })
            }
            className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
          >
            {confirmLabel}
          </button>
          <button
            type="button"
            onClick={() => setAsking(false)}
            className="rounded px-2 py-0.5 text-xs text-muted hover:underline"
          >
            {cancelLabel}
          </button>
        </span>
        {error ? <span className="text-xs text-danger">{error}</span> : null}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setAsking(true)}
      className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-100 dark:border-slate-700 dark:hover:bg-slate-800"
    >
      {label}
    </button>
  );
}
