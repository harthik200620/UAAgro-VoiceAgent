import createMiddleware from "next-intl/middleware";
import { NextResponse, type NextRequest } from "next/server";

import { LOCALE_COOKIE, isLocale, routing } from "./i18n/routing";

const localeRouting = createMiddleware(routing);

/**
 * Locale routing only. Sign-in is not checked here: every page and Route
 * Handler reads the session itself, and one check that also knows the role is
 * easier to keep honest than a cookie-presence test repeated in two places.
 *
 * `/` opens in English unless this browser chose Hindi with the sidebar
 * switch, which leaves a cookie behind. Nothing else is consulted: the
 * browser's language list is not a choice the operator made.
 */
export default function middleware(request: NextRequest) {
  if (request.nextUrl.pathname === "/") {
    const chosen = request.cookies.get(LOCALE_COOKIE)?.value;
    if (chosen && isLocale(chosen) && chosen !== routing.defaultLocale) {
      return NextResponse.redirect(new URL(`/${chosen}`, request.url));
    }
  }
  return localeRouting(request);
}

export const config = {
  /*
   * Two literal patterns rather than one negative-lookahead catch-all.
   *
   * Written in a TypeScript string, `"...\..."` is not an escaped dot --
   * JavaScript drops the backslash -- so the usual `.*\..*` idiom for "a
   * static file" silently becomes "any path", and the middleware stops
   * running for every route but `/`. The root and the two locale prefixes are
   * all that need negotiating; `/_next`, static files and `/api` are outside
   * both and must stay outside, because the `/api` proxies carry no locale.
   */
  matcher: ["/", "/(en|hi)/:path*"],
};
