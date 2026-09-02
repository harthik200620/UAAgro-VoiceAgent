import { redirect } from "next/navigation";

import { Sidebar } from "@/components/sidebar/sidebar";
import { currentSession } from "@/server/session";

/**
 * Every screen but sign-in: the sidebar and a 30px/40px content column.
 *
 * No session means the login page, whatever was asked for. The pages below
 * check their own capability on top of this -- a signed-in read_only user
 * reaches the shell and is then told, in words, what they cannot open.
 */
export default async function PanelLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  const session = await currentSession();
  if (!session) redirect(`/${locale}/login`);

  return (
    <div className="flex min-h-screen">
      <Sidebar session={session} />
      <main className="flex min-w-0 flex-1 flex-col gap-5.5 px-10 pb-9 pt-7.5">{children}</main>
    </div>
  );
}
