import { can, type Capability, type Session } from "./rbac";

/**
 * The sidebar, filtered to what this user can reach.
 *
 * Filtering is presentation, not protection: every route checks its own
 * capability on the server, and hiding a link stops nobody who types the URL.
 * What it does stop is a centre manager spending a morning clicking into
 * screens that answer 403 -- which is how people conclude a tool is broken.
 */

export type NavKey =
  | "overview"
  | "live"
  | "calls"
  | "outbound"
  | "knowledge"
  | "centres"
  | "flows"
  | "data";

export type NavItem = {
  key: NavKey;
  href: string;
  /** Visible when the session holds any one of these. */
  anyOf: readonly Capability[];
};

const NAV_ITEMS: readonly NavItem[] = [
  { key: "overview", href: "/", anyOf: ["calls.view"] },
  { key: "live", href: "/live", anyOf: ["calls.view"] },
  { key: "calls", href: "/calls", anyOf: ["calls.view"] },
  { key: "outbound", href: "/outbound", anyOf: ["campaigns.view"] },
  { key: "knowledge", href: "/knowledge", anyOf: ["knowledge.view"] },
  { key: "centres", href: "/centres", anyOf: ["centres.view"] },
  { key: "flows", href: "/flows", anyOf: ["flows.edit"] },
  { key: "data", href: "/data", anyOf: ["data.view"] },
];

export function navItemsFor(session: Session | null): NavItem[] {
  return NAV_ITEMS.filter((item) => item.anyOf.some((capability) => can(session, capability)));
}

/**
 * Where the routes of the old "Inbound" grouping went, so a bookmark from
 * before 3 September still opens the right screen. The API's attention items
 * may still name the old paths too; `resolvePanelHref` puts them through the
 * same table.
 */
const MOVED: Record<string, string> = {
  "/inbound": "/knowledge",
  "/inbound/knowledge": "/knowledge",
  "/inbound/centres": "/centres",
  "/inbound/greeting": "/flows?type=inbound",
  "/outbound/knowledge": "/knowledge",
};

/** The route an old address now lives at, or the address itself when it never moved. */
export function movedRoute(path: string): string {
  const trimmed = path.length > 1 && path.endsWith("/") ? path.slice(0, -1) : path;
  return MOVED[trimmed] ?? trimmed;
}

/**
 * An `href` from the API as a panel route: only a path inside the panel is
 * accepted -- never a full URL, never a protocol-relative one -- and old
 * paths are moved. Anything else is no link at all.
 */
export function resolvePanelHref(href: string | null): string | null {
  if (!href || !href.startsWith("/") || href.startsWith("//") || href.includes("\\")) return null;
  const [path = "", search] = href.split("?");
  const moved = movedRoute(path);
  if (search === undefined || moved.includes("?")) return moved;
  return `${moved}?${search}`;
}
