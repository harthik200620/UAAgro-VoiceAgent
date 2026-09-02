import createMiddleware from "next-intl/middleware";
import { NextResponse, type NextRequest } from "next/server";

import { routing } from "./i18n/routing";
import { LOCALE_COOKIE, localeRedirect } from "./lib/locale-choice";

const localeRouting = createMiddleware(routing);

/**
 * Locale routing only. Sign-in is not checked here: every page and Route
 * Handler reads the session itself, and one check that also knows the role is
 * easier to keep honest than a cookie-presence test repeated in two places.
 *
 * The panel is English unless this browser chose Hindi in the sidebar, and
 * that setting wins over the address -- see `lib/locale-choice.ts` for why an
 * old `/hi/...` bookmark opens in English rather than quietly overriding the
 * default.
 */
export default function middleware(request: NextRequest) {
  const destination = localeRedirect(
    request.nextUrl.pathname,
    request.cookies.get(LOCALE_COOKIE)?.value,
  );
  if (destination !== null && destination !== request.nextUrl.pathname) {
    const url = new URL(destination, request.url);
    url.search = request.nextUrl.search;
    return NextResponse.redirect(url);
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
   * both and must stay outside, because the `/api` proxies carry no locale --
   * `/api/locale`, which sets the language, is one of them.
   */
  matcher: ["/", "/(en|hi)/:path*"],
};
