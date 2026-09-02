import { createNavigation } from "next-intl/navigation";
import { defineRouting } from "next-intl/routing";

import { DEFAULT_LOCALE, LOCALES } from "@/lib/locale-choice";

/**
 * Two UIs, one set of data.
 *
 * The chrome -- labels, headings, buttons -- comes in English and in Hindi,
 * because the people who run this panel are an ops manager in Lucknow and the
 * centre managers in the districts, and the second group should not have to
 * work in English. Everything the panel *displays* stays exactly as the
 * database holds it: farmer names, transcripts, the scripts the agent speaks.
 * Those are content, and translating them would misrepresent what the farmer
 * actually heard.
 *
 * The choice lives in the URL prefix rather than in the session, so a Hindi
 * link pasted into a chat opens in Hindi for whoever follows it.
 *
 * The panel opens in English. `localeDetection` is off so that a browser
 * whose language list happens to start with Hindi is not routed there
 * uninvited; Hindi is the switch in the sidebar, and the setting it records
 * decides the language of every page after that -- see `lib/locale-choice.ts`.
 */
export const routing = defineRouting({
  locales: LOCALES,
  defaultLocale: DEFAULT_LOCALE,
  localePrefix: "always",
  localeDetection: false,
});

export type Locale = (typeof routing.locales)[number];

export function isLocale(value: string): value is Locale {
  return (routing.locales as readonly string[]).includes(value);
}

/**
 * The locale for dates, times and numbers -- not for text -- in both UIs.
 *
 * `en` alone is US English: it renders 1 September 2026 as "9/1/2026", which
 * an operator in Lucknow reads as the ninth of January. `en-IN` gives
 * day-first dates and lakh/crore digit grouping, which is what everybody using
 * this panel expects whichever language the labels are in.
 */
export const FORMAT_LOCALE = "en-IN";

export const { Link, redirect, usePathname, useRouter } = createNavigation(routing);
