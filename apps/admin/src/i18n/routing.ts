import { defineRouting } from "next-intl/routing";
import { createNavigation } from "next-intl/navigation";

/**
 * The panel's chrome is English. The data is not.
 *
 * Every label, heading, column and button here is English. Everything the
 * panel *displays* stays in whatever language it was written or spoken in:
 * farmer names and villages in Devanagari, product names as the catalogue
 * holds them ("यूरिया", not "urea"), call transcripts in the language of the
 * call, agent prompts and greetings exactly as published. Those are content,
 * and translating them in the view would misrepresent what is in the database
 * and what the farmer actually heard.
 *
 * §15 asks for a Hindi UI as well, on the reasoning that a centre manager in
 * Barabanki should not have to work in English. That is not built at the
 * moment by decision rather than by omission -- and `messages/hi.json` is a
 * complete translation, kept precisely so turning it back on is this file
 * plus nothing else:
 *
 *     locales: ["en", "hi"],  defaultLocale: "hi",
 *
 * Dates and numbers are formatted `en-IN` regardless (see `FORMAT_LOCALE`):
 * the language is English, the conventions are Indian, and 1/9/2026 means the
 * first of September to everybody who will read this.
 */
export const routing = defineRouting({
  locales: ["en"],
  defaultLocale: "en",
  localePrefix: "always",
});

export type Locale = (typeof routing.locales)[number];

/**
 * The locale used for dates, times and numbers -- not for text.
 *
 * `en` alone is US English: it renders 1 September 2026 as "9/1/2026", which
 * an operator in Lucknow reads as the ninth of January. `en-IN` gives
 * day-first dates and lakh/crore digit grouping, which is what everybody using
 * this panel expects.
 */
export const FORMAT_LOCALE = "en-IN";

export const { Link, redirect, usePathname, useRouter, getPathname } =
  createNavigation(routing);
