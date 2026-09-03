import { redirect } from "@/i18n/routing";
import { movedRoute } from "@/lib/nav";

export const dynamic = "force-dynamic";

/**
 * The "Inbound" grouping of the panel before 3 September 2026. Its screens
 * are sections of their own now; a bookmark or a link in an old message
 * still lands where it meant to.
 */
export default async function InboundRedirect({
  params,
}: {
  params: Promise<{ locale: string; rest?: string[] }>;
}) {
  const { locale, rest = [] } = await params;
  const moved = movedRoute(rest.length > 0 ? `/inbound/${rest.join("/")}` : "/inbound");
  const [pathname = "/", search] = moved.split("?");
  redirect({
    href: search ? { pathname, query: Object.fromEntries(new URLSearchParams(search)) } : pathname,
    locale,
  });
}
