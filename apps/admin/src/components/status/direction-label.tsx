import { useTranslations } from "next-intl";

import { Icon } from "@/components/ui/icon";
import type { Direction } from "@/lib/contract";

/** "↙ Inbound" / "↗ Outbound", with the arrow the mockups use for each. */
export function DirectionLabel({ direction, detail }: { direction: Direction; detail?: string }) {
  const t = useTranslations("directions");
  return (
    <span className="inline-flex items-center gap-1.5 text-muted">
      <Icon name={direction} size={13} />
      {detail ? `${t(direction)} · ${detail}` : t(direction)}
    </span>
  );
}
