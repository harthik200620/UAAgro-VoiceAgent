import { useTranslations } from "next-intl";

import { Chip } from "@/components/ui/chip";
import { humanize, messageKey, outcomeTone } from "@/lib/tones";

/**
 * How a call ended, as a chip. Outcomes the panel knows get their translated
 * label; one it has never seen is still shown, humanised from its code, so a
 * new outcome on the API side never renders as a blank pill.
 */
export function OutcomeChip({ outcome, dtmf }: { outcome: string | null; dtmf?: string | null }) {
  const t = useTranslations("outcomes");
  if (!outcome) return <Chip tone="grey">{t("unknown")}</Chip>;
  const key = messageKey(outcome);
  const label = t.has(key) ? t(key) : humanize(outcome);
  return <Chip tone={outcomeTone(outcome)}>{dtmf ? `${label} · ${dtmf}` : label}</Chip>;
}
