import createMiddleware from "next-intl/middleware";

import { routing } from "./i18n/routing";

/**
 * Locale routing only. Sign-in is not checked here: every page and Route
 * Handler reads the session itself, and one check that also knows the role is
 * easier to keep honest than a cookie-presence test repeated in two places.
 */
export default createMiddleware(routing);

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
