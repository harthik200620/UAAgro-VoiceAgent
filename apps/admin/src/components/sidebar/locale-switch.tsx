"use client";

import { clsx } from "clsx";
import { useLocale } from "next-intl";

import { Link, routing, usePathname } from "@/i18n/routing";

const NAMES = { en: "EN", hi: "हिं" } as const;

/** EN / हिं: two links to the same page in the other language. */
export function LocaleSwitch({ label }: { label: string }) {
  const current = useLocale();
  const pathname = usePathname();

  return (
    <div
      role="group"
      aria-label={label}
      className="flex overflow-hidden rounded-btn border border-line bg-surface"
    >
      {routing.locales.map((locale) => {
        const active = locale === current;
        return (
          <Link
            key={locale}
            href={pathname}
            locale={locale}
            lang={locale}
            aria-current={active ? "true" : undefined}
            className={clsx(
              "flex-1 py-[7px] text-center text-small",
              active ? "bg-ink font-semibold text-paper hover:text-paper" : "text-muted hover:text-ink",
            )}
          >
            {NAMES[locale]}
          </Link>
        );
      })}
    </div>
  );
}
