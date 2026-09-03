"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { summariseAgain } from "@/app/actions/calls";
import { Button } from "@/components/ui/button";
import { useRouter } from "@/i18n/routing";

/**
 * "Summarise again": the post-call summary rewritten by the model, then the
 * page re-read so the new words land where the old ones were. A model call,
 * so it never runs on load.
 */
export function SummariseButton({ callId }: { callId: string }) {
  const t = useTranslations("call");
  const router = useRouter();
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <Button
        icon="refresh"
        size="md"
        disabled={pending}
        onClick={() =>
          startTransition(async () => {
            setError(null);
            const result = await summariseAgain(callId);
            if (result.ok) router.refresh();
            else setError(result.message);
          })
        }
      >
        {pending ? t("summarising") : t("summariseAgain")}
      </Button>
      {error ? (
        <span role="alert" className="text-small text-red-text">
          {error}
        </span>
      ) : null}
    </span>
  );
}
