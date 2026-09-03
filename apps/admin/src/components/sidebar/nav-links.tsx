"use client";

import { clsx } from "clsx";

import { Icon, type IconName } from "@/components/ui/icon";
import { Link, usePathname } from "@/i18n/routing";
import type { NavKey } from "@/lib/nav";

const ICONS: Record<NavKey, IconName> = {
  overview: "overview",
  live: "live",
  calls: "phone",
  outbound: "outbound",
  knowledge: "book",
  centres: "pin",
  flows: "flows",
  data: "data",
};

/** The section list. A Client Component only because the active item needs the current path. */
export function NavLinks({
  label,
  items,
}: {
  label: string;
  items: { key: NavKey; href: string; label: string }[];
}) {
  const pathname = usePathname();

  return (
    <nav aria-label={label} className="mt-7.5 flex flex-col gap-0.5">
      {items.map((item) => {
        const active =
          item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(`${item.href}/`);
        return (
          <Link
            key={item.key}
            href={item.href}
            aria-current={active ? "page" : undefined}
            className={clsx(
              "flex items-center gap-3 rounded-btn px-3.5 py-2.5 text-base",
              active
                ? "bg-ink font-semibold text-paper hover:text-paper"
                : "font-medium text-ink hover:bg-line/60",
            )}
          >
            <Icon name={ICONS[item.key]} size={18} />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
