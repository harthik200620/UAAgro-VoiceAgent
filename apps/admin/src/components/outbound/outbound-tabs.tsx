import { getTranslations } from "next-intl/server";

import { Tabs } from "@/components/ui/tabs";
import { outboundTabsFor, type OutboundTab } from "@/lib/nav";
import type { Session } from "@/lib/rbac";

/**
 * "Campaigns" and "Knowledge", under the Outbound title.
 *
 * The knowledge tab is not a second knowledge base: it is the same documents,
 * shown as an offer call would see them. A farmer who presses 2 is answered
 * by the same agent the helpline uses, so the material it may quote belongs
 * where the campaign is set up as much as where the helpline is.
 */
export async function OutboundTabs({ session, active }: { session: Session; active: OutboundTab }) {
  const t = await getTranslations("outboundTabs");
  const tabs = outboundTabsFor(session);
  if (tabs.length < 2) return null;

  return (
    <Tabs
      label={t("label")}
      items={tabs.map((tab) => ({
        href: tab.href,
        label: t(tab.key),
        active: tab.key === active,
      }))}
    />
  );
}
