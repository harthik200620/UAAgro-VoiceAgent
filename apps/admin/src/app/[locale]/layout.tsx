import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getMessages, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";

import { routing, type Locale } from "@/i18n/routing";
import { Nav } from "@/components/nav";
import { currentSession } from "@/server/session";
import "../globals.css";

export const metadata: Metadata = {
  title: "UA Agro — नवीन खुशहाली किसान सेवा केंद्र",
  description: "Operations panel",
  // §15: an operations tool, not a marketing site. Nothing here should be
  // indexed, previewed or shared.
  robots: { index: false, follow: false },
};

export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }));
}

export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  if (!routing.locales.includes(locale as Locale)) notFound();
  setRequestLocale(locale);

  const messages = await getMessages();
  const session = await currentSession();

  return (
    <html lang={locale} suppressHydrationWarning>
      <body className="bg-white text-slate-900 antialiased dark:bg-slate-950 dark:text-slate-100">
        <NextIntlClientProvider messages={messages}>
          <div className="flex min-h-screen flex-col md:flex-row">
            {/* Nav takes the session so it can hide controls the user cannot
                use. That is presentation only -- every capability is checked
                again on the server (see lib/rbac). */}
            <Nav session={session} />
            <main className="min-w-0 flex-1 p-4 md:p-6">{children}</main>
          </div>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
