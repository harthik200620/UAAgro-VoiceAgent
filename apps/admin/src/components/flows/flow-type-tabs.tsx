import { getTranslations } from "next-intl/server";

import { Tabs } from "@/components/ui/tabs";
import type { FlowType } from "@/lib/contract";

/** Outbound · Inbound, above the script list. */
export async function FlowTypeTabs({ active }: { active: FlowType }) {
  const t = await getTranslations("flows");
  return (
    <Tabs
      label={t("tabsLabel")}
      items={(["outbound", "inbound"] as const).map((type) => ({
        href: `/flows?type=${type}`,
        label: t(`types.${type}`),
        active: type === active,
      }))}
    />
  );
}
