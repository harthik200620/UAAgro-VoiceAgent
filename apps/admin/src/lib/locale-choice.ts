/**
 * Which language this browser gets, and why the URL does not decide it.
 *
 * The panel is in English unless somebody chooses Hindi with the switch in
 * the sidebar. That choice is a per-browser setting, kept in a cookie, and it
 * outranks the locale prefix in the address: a `/hi/...` link opened by a
 * browser that has not chosen Hindi lands on the English page, because
 * otherwise a bookmark or an autocompleted address from months ago quietly
 * overrides the default and there is nothing the operator can do about it
 * except notice and retype the URL.
 *
 * The prefix is still in every address, because next-intl needs it to pick
 * the messages, and it still changes the language when it disagrees with the
 * setting -- it just changes it by moving the address to the setting rather
 * than the other way round. The switch changes the setting first (through
 * `/api/locale`, which sets the cookie and then redirects), so by the time a
 * `/hi/...` request arrives the cookie already says Hindi.
 *
 * Kept free of imports so the middleware, the route handler and the tests can
 * all use it without dragging next-intl into the edge runtime.
 */

export const LOCALE_COOKIE = "uaagro_locale";

export const LOCALES = ["en", "hi"] as const;

export type PanelLocale = (typeof LOCALES)[number];

/** English, until somebody says otherwise. */
export const DEFAULT_LOCALE: PanelLocale = "en";

export function isPanelLocale(value: string | null | undefined): value is PanelLocale {
  return typeof value === "string" && (LOCALES as readonly string[]).includes(value);
}

/** The language this browser has chosen, or the default when it has not. */
export function chosenLocale(cookie: string | null | undefined): PanelLocale {
  return isPanelLocale(cookie) ? cookie : DEFAULT_LOCALE;
}

/** The locale prefix of a path, and what follows it. */
function split(pathname: string): { locale: PanelLocale | null; rest: string } {
  const [, first = "", ...others] = pathname.split("/");
  if (!isPanelLocale(first)) return { locale: null, rest: pathname };
  const rest = others.length > 0 ? `/${others.join("/")}` : "";
  return { locale: first, rest };
}

/**
 * Where a request should go instead, or `null` to leave it alone.
 *
 * `/` goes to the chosen language. A path already prefixed with the chosen
 * language is left alone. A path prefixed with the other one is moved, which
 * is what makes a stale Hindi bookmark open in English.
 */
export function localeRedirect(pathname: string, cookie: string | null | undefined): string | null {
  const chosen = chosenLocale(cookie);
  if (pathname === "/" || pathname === "") return `/${chosen}`;

  const { locale, rest } = split(pathname);
  // Not a locale path at all: the middleware's matcher should not have sent it
  // here, and guessing a prefix for it would invent a route.
  if (locale === null) return null;
  if (locale === chosen) return null;
  return `/${chosen}${rest}`;
}

/**
 * A safe place to return to after the switch.
 *
 * The switch posts the page it was clicked on so the operator stays where
 * they were. It arrives as form input, so it is treated as untrusted: only a
 * path within this panel is accepted, never a full URL and never a
 * protocol-relative one, which would make the language switch an open
 * redirect.
 */
export function returnPath(next: string | null | undefined, locale: PanelLocale): string {
  const candidate = typeof next === "string" ? next : "";
  const safe =
    candidate.startsWith("/") && !candidate.startsWith("//") && !candidate.includes("\\")
      ? candidate
      : "/";
  // `usePathname` hands over a path with no locale prefix, but a caller that
  // includes one must not produce `/hi/hi/calls`.
  const { rest } = split(safe);
  const suffix = rest === "/" ? "" : rest;
  return `/${locale}${suffix}`;
}
