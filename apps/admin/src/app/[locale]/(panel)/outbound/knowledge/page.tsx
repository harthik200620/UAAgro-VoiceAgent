import { redirect } from "@/i18n/routing";

export const dynamic = "force-dynamic";

/** The outbound "Knowledge" tab is the Knowledge section now: one corpus, one screen, a scope on every document. */
export default async function OutboundKnowledgeRedirect({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  redirect({ href: "/knowledge", locale });
}
