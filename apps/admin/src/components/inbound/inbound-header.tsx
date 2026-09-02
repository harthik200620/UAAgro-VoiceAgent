import { getTranslations } from "next-intl/server";

import { PageHeader } from "@/components/ui/page-header";
import { Tabs } from "@/components/ui/tabs";
import { inboundTabsFor, type InboundTab } from "@/lib/nav";
import type { Session } from "@/lib/rbac";

/** "Inbound" and its three tabs, with only the tabs this session may open. */
export async function InboundHeader({
  session,
  active,
  subtitle,
}: {
  session: Session;
  active: InboundTab;
  subtitle: string;
}) {
  const t = await getTranslations("inbound");
  return (
    <>
      <PageHeader title={t("title")} subtitle={subtitle} />
      <Tabs
        label={t("tabsLabel")}
        items={inboundTabsFor(session).map((tab) => ({
          href: tab.href,
          label: t(`tabs.${tab.key}`),
          active: tab.key === active,
        }))}
      />
    </>
  );
}
