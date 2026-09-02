import { getTranslations } from "next-intl/server";

import { signOut } from "@/app/actions/auth";

import { Link } from "@/i18n/routing";
import { can, type Capability, type Session } from "@/lib/rbac";

/**
 * The screen list of §15.1, filtered to what this user can actually reach.
 *
 * Filtering is presentation, not protection: every route checks its own
 * capability on the server, and hiding a link stops nobody who types the URL.
 * What it does stop is a centre manager spending their morning clicking into
 * screens that return 403 -- which is how people conclude a tool is broken.
 *
 * §15.1's "Call Routing & Numbers" is absent rather than linked. §10 defines
 * no table for DIDs, number-to-flow mapping, business hours or transfer
 * chains, so there is nothing behind it -- and a nav entry leading to an empty
 * page is a worse answer than saying so in the docs.
 */

type Item = {
  href: string;
  labelKey: string;
  capability: Capability;
};

const ITEMS: readonly Item[] = [
  { href: "/", labelKey: "dashboard", capability: "calls.view" },
  { href: "/live", labelKey: "liveCalls", capability: "calls.listen" },
  { href: "/calls", labelKey: "callExplorer", capability: "calls.view" },
  { href: "/farmers", labelKey: "farmers", capability: "farmers.view" },
  { href: "/catalogue", labelKey: "catalogue", capability: "catalogue.view" },
  { href: "/advisory", labelKey: "advisory", capability: "advisory.view" },
  { href: "/knowledge", labelKey: "knowledge", capability: "knowledge.upload" },
  { href: "/flows", labelKey: "flows", capability: "flows.edit" },
  { href: "/offers", labelKey: "offers", capability: "campaigns.create" },
  { href: "/campaigns", labelKey: "campaigns", capability: "campaigns.create" },
  { href: "/spam", labelKey: "spam", capability: "campaigns.control" },
  { href: "/analytics", labelKey: "analytics", capability: "calls.view" },
  { href: "/users", labelKey: "users", capability: "users.manage" },
  { href: "/audit", labelKey: "audit", capability: "audit.view" },
  { href: "/settings", labelKey: "settings", capability: "settings.edit" },
];

export async function Nav({ session }: { session: Session | null }) {
  const t = await getTranslations("nav");
  const auth = await getTranslations("login");
  const visible = ITEMS.filter((item) => can(session, item.capability));

  return (
    <nav
      aria-label="Sections"
      className="shrink-0 border-b border-slate-200 bg-slate-50 md:w-52 md:border-b-0 md:border-r dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="px-3 py-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted">
          UA Agro
        </p>
        {session ? (
          <>
            <p className="truncate text-sm" title={session.name}>
              {session.name}
            </p>
            {/* A plain form, not a link: signing out changes state, and a GET
                that logs you out is a link any page can prefetch. */}
            <form action={signOut}>
              <button
                type="submit"
                className="mt-0.5 text-xs text-muted underline underline-offset-2"
              >
                {auth("signOut")}
              </button>
            </form>
          </>
        ) : (
          <Link href="/login" className="text-sm underline underline-offset-2">
            {auth("signIn")}
          </Link>
        )}
      </div>
      {/* Horizontal scroll on a phone, a column on a desktop. §15 says
          managers will open this on a phone, and a 16-item vertical list on a
          375px screen pushes the actual content below the fold. */}
      <ul className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:overflow-visible">
        {visible.map((item) => (
          <li key={item.href} className="shrink-0">
            <Link
              href={item.href}
              className="block whitespace-nowrap rounded px-2 py-1.5 text-sm hover:bg-slate-200 dark:hover:bg-slate-800"
            >
              {t(item.labelKey)}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}
