import { can, type Capability, type Session } from "./rbac";

/**
 * The sidebar and the Inbound tabs, filtered to what this user can reach.
 *
 * Filtering is presentation, not protection: every route checks its own
 * capability on the server, and hiding a link stops nobody who types the URL.
 * What it does stop is a centre manager spending a morning clicking into
 * screens that answer 403 -- which is how people conclude a tool is broken.
 */

export type NavKey = "live" | "calls" | "outbound" | "inbound" | "flows" | "data";

type NavItem = {
  key: NavKey;
  href: string;
  /** Visible when the session holds any one of these. */
  anyOf: readonly Capability[];
};

const NAV_ITEMS: readonly NavItem[] = [
  { key: "live", href: "/", anyOf: ["calls.view"] },
  { key: "calls", href: "/calls", anyOf: ["calls.view"] },
  { key: "outbound", href: "/outbound", anyOf: ["campaigns.view"] },
  { key: "inbound", href: "/inbound", anyOf: ["knowledge.view", "centres.view"] },
  { key: "flows", href: "/flows", anyOf: ["flows.edit"] },
  { key: "data", href: "/data", anyOf: ["data.view"] },
];

export type InboundTab = "knowledge" | "centres" | "greeting";

const INBOUND_TABS: readonly { key: InboundTab; href: string; capability: Capability }[] = [
  { key: "knowledge", href: "/inbound/knowledge", capability: "knowledge.view" },
  { key: "centres", href: "/inbound/centres", capability: "centres.view" },
  { key: "greeting", href: "/inbound/greeting", capability: "flows.edit" },
];

export function navItemsFor(session: Session | null): NavItem[] {
  return NAV_ITEMS.filter((item) => item.anyOf.some((capability) => can(session, capability)));
}

export function inboundTabsFor(session: Session | null) {
  return INBOUND_TABS.filter((tab) => can(session, tab.capability));
}
