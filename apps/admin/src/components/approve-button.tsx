"use client";

import { useState, useTransition } from "react";

import { approveRecommendation } from "@/app/actions/advisory";

/**
 * Approving one crop recommendation (§9, §15.1).
 *
 * A Client Component because it needs pending state, but it holds no
 * authority: the Server Action it calls re-checks the capability, and the
 * database CHECK constraint refuses an incomplete crop-protection row whatever
 * this button does. `disabled` here is courtesy -- it tells the agronomist why
 * before they click, rather than after.
 */
export function ApproveButton({
  recommendationId,
  disabled,
  disabledReason,
  label,
}: {
  recommendationId: string;
  disabled: boolean;
  disabledReason?: string | undefined;
  label: string;
}) {
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <span className="inline-flex flex-col gap-1">
      <button
        type="button"
        disabled={disabled || pending}
        title={disabledReason}
        onClick={() =>
          startTransition(async () => {
            setError(null);
            const result = await approveRecommendation(recommendationId);
            // The server's own message, not a generic one. "needs a
            // pre-harvest interval" is actionable; "approval failed" is not.
            if (!result.ok) setError(result.message);
          })
        }
        className="rounded border border-slate-300 px-2 py-0.5 text-xs hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
      >
        {label}
      </button>
      {error ? <span className="text-xs text-danger">{error}</span> : null}
      {disabled && disabledReason ? (
        <span className="text-xs text-muted">{disabledReason}</span>
      ) : null}
    </span>
  );
}
