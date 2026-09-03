import { getTranslations } from "next-intl/server";

import type { LoadFailure } from "@/server/load";

import { Card } from "./card";

/**
 * What a section shows when the control plane could not give it its data:
 * one sentence for why, in the operator's language, and nothing else. A
 * route the API does not serve yet, an outage and a refusal are different
 * sentences, and none of them is a stack trace.
 */
export async function NotAvailable({
  reason,
  message,
  compact = false,
}: {
  reason: LoadFailure;
  /** The API's own words, when it had any; shown instead of the generic line. */
  message: string | null;
  /** Inside another card's flow rather than standing alone. */
  compact?: boolean;
}) {
  const t = await getTranslations("unavailable");
  return (
    <Card role="status" className={compact ? "px-5 py-4" : "max-w-[640px] p-6"}>
      <div className={compact ? "text-body font-semibold" : "text-md font-semibold"}>
        {t(`${reason}.title`)}
      </div>
      <p className="mt-1 text-ui text-muted">{message ?? t(`${reason}.text`)}</p>
    </Card>
  );
}
