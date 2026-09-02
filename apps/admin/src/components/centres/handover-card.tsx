import { clsx } from "clsx";
import { getTranslations } from "next-intl/server";

import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";
import type { TransferRules } from "@/lib/contract";

import { FallbackNumberForm } from "./fallback-number-form";

/**
 * "When a call is handed to a person": the reasons, as configured, and the
 * two numbers that shape the hand-over. Only the fallback number is editable
 * from here -- the reasons are policy and live in configuration.
 */
export async function HandoverCard({ rules, canEdit }: { rules: TransferRules; canEdit: boolean }) {
  const t = await getTranslations("centres.handover");

  return (
    <Card className="flex flex-col gap-3 px-5.5 pb-5 pt-4.5">
      <div className="flex items-center gap-2 font-semibold">
        <Icon name="transfer" />
        {t("title")}
      </div>
      {rules.reasons.length === 0 ? (
        <p className="text-ui text-muted">{t("noReasons")}</p>
      ) : (
        <ul className="grid grid-cols-2 gap-x-7 gap-y-2.5 text-ui">
          {rules.reasons.map((reason) => (
            <li key={reason.key} className={clsx("flex gap-2.5", !reason.enabled && "text-muted")}>
              <Icon
                name={reason.enabled ? "check" : "close"}
                className={reason.enabled ? "text-green" : "text-faint"}
              />
              <span>
                {reason.label}
                {!reason.enabled ? ` · ${t("off")}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="flex items-center justify-between gap-4 border-t border-inset pt-3 text-body text-muted">
        <span>
          {t("timing", { whisper: rules.whisperSeconds, ring: rules.ringTimeoutSeconds })}
        </span>
        <FallbackNumberForm current={rules.fallbackNumber} canEdit={canEdit} />
      </div>
    </Card>
  );
}
