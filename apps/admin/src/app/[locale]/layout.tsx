import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getMessages, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";

import { fontVariables } from "@/app/fonts";
import { isLocale } from "@/i18n/routing";

import "../globals.css";

export const metadata: Metadata = {
  title: "UA Agro — Kisan Sewa Kendra",
  description: "Control room for the UA Agro farmer helpline",
  // An operations tool, not a marketing site. Nothing here should be indexed,
  // previewed or shared.
  robots: { index: false, follow: false },
};

/**
 * The document: fonts, language, and the translation catalogue for Client
 * Components. The sidebar shell lives one level down in `(panel)` so the
 * sign-in screen can stand alone.
 */
export default async function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  if (!isLocale(locale)) notFound();
  setRequestLocale(locale);
  const messages = await getMessages();

  return (
    <html lang={locale} className={fontVariables}>
      <body>
        <NextIntlClientProvider locale={locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
