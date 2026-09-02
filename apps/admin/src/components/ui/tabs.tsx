import { clsx } from "clsx";

import { Link } from "@/i18n/routing";

/** The underlined tab row under a page title. Links, so each tab is a URL. */
export function Tabs({
  label,
  items,
}: {
  label: string;
  items: { href: string; label: string; active: boolean }[];
}) {
  return (
    <nav aria-label={label} className="flex border-b border-line">
      {items.map((item) => (
        <Link
          key={item.href}
          href={item.href}
          aria-current={item.active ? "page" : undefined}
          className={clsx(
            "-mb-px mr-5.5 border-b-2 px-0.5 py-2 text-base",
            item.active
              ? "border-ink font-semibold text-ink"
              : "border-transparent font-medium text-muted hover:text-ink",
          )}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}
