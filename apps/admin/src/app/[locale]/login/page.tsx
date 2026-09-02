import { getTranslations, setRequestLocale } from "next-intl/server";

import { LoginForm } from "@/components/login-form";

/** Never cached: it is a form that sets a cookie. */
export const dynamic = "force-dynamic";

/**
 * §15's sign-in screen.
 *
 * The labels are resolved on the server and handed down as props, so the
 * Client Component holds no translation catalogue and no configuration -- the
 * panel's `server-only` boundary stays intact through the one screen that has
 * to be interactive.
 */
export default async function LoginPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);
  const t = await getTranslations("login");

  return (
    <LoginForm
      labels={{
        title: t("title"),
        email: t("email"),
        password: t("password"),
        submit: t("submit"),
        mfaTitle: t("mfaTitle"),
        mfaHint: t("mfaHint"),
        enrolTitle: t("enrolTitle"),
        enrolHint: t("enrolHint"),
        secretLabel: t("secretLabel"),
        code: t("code"),
        verify: t("verify"),
      }}
    />
  );
}
