import { getTranslations } from "next-intl/server";

import { signOut } from "@/app/actions/auth";
import { Icon } from "@/components/ui/icon";
import { initials } from "@/lib/format";
import { navItemsFor } from "@/lib/nav";
import type { Session } from "@/lib/rbac";

import { LocaleSwitch } from "./locale-switch";
import { NavLinks } from "./nav-links";

/**
 * The 224px column on every screen: the logo, the six sections this user can
 * reach, the language toggle and who is signed in.
 *
 * "किसान सेवा केंद्र" under the wordmark is the organisation's name, not a
 * label, so it is the same in both UIs and marked as Hindi for the font.
 */
export async function Sidebar({ session }: { session: Session }) {
  const t = await getTranslations("nav");
  const roles = await getTranslations("roles");
  const items = navItemsFor(session).map((item) => ({
    key: item.key,
    href: item.href,
    label: t(item.key),
  }));

  return (
    <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col border-r border-line bg-inset px-3.5 pb-4.5 pt-5.5">
      <div className="flex items-center gap-3 px-2 pt-1">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-brand">
          <Icon name="leaf" size={22} strokeWidth={1.8} className="text-paper" />
        </div>
        <div className="leading-tight">
          <div className="text-prose font-semibold leading-tight tracking-[-0.01em]">UA Agro</div>
          <div className="text-meta text-muted">
            Kisan Sewa Kendra
          </div>
        </div>
      </div>

      <NavLinks label={t("sections")} items={items} />

      <div className="mt-auto flex flex-col gap-3.5">
        <LocaleSwitch label={t("language")} />
        <div className="flex items-center gap-2.5 px-1.5 py-1">
          <div
            aria-hidden="true"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-line bg-surface text-label font-semibold"
          >
            {initials(session.name) || "•"}
          </div>
          <div className="min-w-0 leading-tight">
            <div className="truncate text-body font-medium" title={roles(session.role)}>
              {session.name || roles(session.role)}
            </div>
            {/* A form, not a link: signing out changes state, and a GET that
                signs you out is a link any page can prefetch. */}
            <form action={signOut}>
              <button
                type="submit"
                className="relative text-meta text-muted after:absolute after:-inset-y-2.5 after:inset-x-0 after:content-[''] hover:text-ink"
              >
                {t("signOut")}
              </button>
            </form>
          </div>
        </div>
      </div>
    </aside>
  );
}
