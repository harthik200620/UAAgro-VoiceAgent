import { getRequestConfig } from "next-intl/server";
import { routing, type Locale } from "./routing";

export default getRequestConfig(async ({ requestLocale }) => {
  const requested = await requestLocale;
  const locale: Locale = routing.locales.includes(requested as Locale)
    ? (requested as Locale)
    : routing.defaultLocale;

  return {
    locale,
    messages: (await import(`../messages/${locale}.json`)).default,
    // IST, because every timestamp in this panel is about when a farmer's
    // phone rang. A browser-local rendering would show a manager in another
    // timezone the wrong calling window.
    timeZone: "Asia/Kolkata",
  };
});
