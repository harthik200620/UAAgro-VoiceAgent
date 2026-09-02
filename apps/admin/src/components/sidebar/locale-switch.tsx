"use client";

import { clsx } from "clsx";
import { useLocale } from "next-intl";

import { usePathname } from "@/i18n/routing";
import { LOCALES } from "@/lib/locale-choice";

const NAMES = { en: "EN", hi: "हिं" } as const;

/**
 * EN / हिं: the panel's language, for this browser, from now on.
 *
 * Two submit buttons rather than two links. The language is a setting the
 * server records before it serves the next page (`/api/locale`), so that a
 * page opened later -- from a bookmark, from history, from a link a colleague
 * sent -- comes back in the language that was chosen here rather than the one
 * that happens to be in the address.
 */
export function LocaleSwitch({ label }: { label: string }) {
  const current = useLocale();
  const pathname = usePathname();

  return (
    <form
      method="post"
      action="/api/locale"
      role="group"
      aria-label={label}
      className="flex overflow-hidden rounded-btn border border-line bg-surface"
    >
      <input type="hidden" name="next" value={pathname} />
      {LOCALES.map((locale) => {
        const active = locale === current;
        return (
          <button
            key={locale}
            type="submit"
            name="locale"
            value={locale}
            lang={locale}
            aria-current={active ? "true" : undefined}
            className={clsx(
              "flex-1 cursor-pointer py-[7px] text-center text-small",
              active ? "bg-ink font-semibold text-paper" : "text-muted hover:text-ink",
            )}
          >
            {NAMES[locale]}
          </button>
        );
      })}
    </form>
  );
}
