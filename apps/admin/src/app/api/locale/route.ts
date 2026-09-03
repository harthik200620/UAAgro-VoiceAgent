import { NextResponse } from "next/server";

import { LOCALE_COOKIE, isPanelLocale, returnPath } from "@/lib/locale-choice";

export const dynamic = "force-dynamic";

/** A year: the language somebody picked should still be there next season. */
const A_YEAR = 60 * 60 * 24 * 365;

/**
 * The sidebar's language switch.
 *
 * A form post rather than a link, because the setting has to be written
 * before the next page is requested: the middleware sends a `/hi/...` request
 * back to English unless the cookie already says Hindi, and a cookie written
 * in the browser races a page Next may have prefetched. Setting it here and
 * redirecting afterwards leaves no room for that -- the browser follows the
 * redirect with the new cookie attached.
 *
 * The redirect is a path, not an absolute URL: the browser resolves it
 * against the origin it used, so a session opened on 127.0.0.1 is not sent
 * to `localhost` -- a different origin, where its cookies are not -- and
 * nothing from the request's headers is trusted to name a host.
 *
 * Nothing here is authenticated: the language of the chrome is not a
 * permission, and every page still checks the session for itself.
 */
export async function POST(request: Request): Promise<Response> {
  const form = await request.formData().catch(() => null);
  const asked = form?.get("locale");
  const locale = typeof asked === "string" ? asked : null;
  // 303, so the browser follows a POST with a GET rather than posting again.
  if (!isPanelLocale(locale)) return seeOther("/");

  const next = form?.get("next");
  const response = seeOther(returnPath(typeof next === "string" ? next : null, locale));
  response.cookies.set(LOCALE_COOKIE, locale, {
    path: "/",
    maxAge: A_YEAR,
    sameSite: "lax",
    // Readable by script, unlike the session: it is a display preference, and
    // nothing is authorised by it.
    httpOnly: false,
  });
  return response;
}

function seeOther(path: string): NextResponse {
  return new NextResponse(null, { status: 303, headers: { location: path } });
}
