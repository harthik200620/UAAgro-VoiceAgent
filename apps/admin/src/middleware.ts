import createMiddleware from "next-intl/middleware";
import { NextResponse, type NextRequest } from "next/server";

import { routing } from "./i18n/routing";

const intl = createMiddleware(routing);

/**
 * Locale routing, plus a redirect for the retired Hindi prefix.
 *
 * The panel was briefly served at `/hi/...`, and links to those paths exist in
 * bookmarks, notes and chat history. `hi` is no longer a configured locale, so
 * the layout would call `notFound()` and a manager following an old link would
 * get a 404 that reads as "the panel is down" rather than "we moved". One
 * comparison per request avoids that.
 *
 * A 307 rather than a permanent redirect: §15 still wants a Hindi UI, so these
 * paths are expected to mean something again, and a 308 is cached by browsers
 * long past that.
 */
export default function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname === "/hi" || pathname.startsWith("/hi/")) {
    const url = request.nextUrl.clone();
    url.pathname = `/en${pathname.slice(3)}`;
    return NextResponse.redirect(url, 307);
  }

  return intl(request);
}

export const config = {
  /*
   * Listed explicitly rather than as one negative-lookahead catch-all.
   *
   * The catch-all that used to be here did not do what it read as. Written in
   * a TypeScript string, `"...\..."` is not an escaped dot -- JavaScript drops
   * the backslash, so the intended `.*\..*` ("a path containing a dot", i.e. a
   * static file) became `.*..*` ("a path of at least one character"). The
   * negative lookahead therefore excluded almost every route, and this
   * middleware ran only for `/`. Locale negotiation appeared to work because
   * the root is the one path that still matched.
   *
   * Two literal patterns cannot go wrong the same way: the root, and anything
   * under a locale prefix. Nothing else needs middleware -- `/_next`, static
   * files and API routes are all outside both.
   */
  matcher: ["/", "/(en|hi)/:path*"],
};
