import { redirect } from "@/i18n/routing";
import { inboundTabsFor } from "@/lib/nav";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/** `/inbound` is the section, not a screen: it opens on the first tab this user can see. */
export default async function InboundPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  const session = await currentSession();
  const first = inboundTabsFor(session)[0];
  redirect({ href: first?.href ?? "/", locale });
}
