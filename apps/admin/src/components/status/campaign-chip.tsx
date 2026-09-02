import { useTranslations } from "next-intl";

import { Chip } from "@/components/ui/chip";
import type { CampaignStatus } from "@/lib/contract";
import { campaignTone } from "@/lib/tones";

/** A campaign's state as a chip: amber while it runs, green when it is done, red when it was stopped. */
export function CampaignChip({ status }: { status: CampaignStatus }) {
  const t = useTranslations("campaignStatus");
  return (
    <Chip tone={campaignTone(status)} pulse={status === "running"}>
      {t(status)}
    </Chip>
  );
}
