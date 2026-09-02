import { getTranslations, setRequestLocale } from "next-intl/server";
import { redirect } from "next/navigation";

import { LoginForm } from "@/components/login-form";
import { Icon } from "@/components/ui/icon";
import { currentSession } from "@/server/session";

/** Never cached: it is a form that sets a cookie. */
export const dynamic = "force-dynamic";

/**
 * The front door: the green panel with the greeting, and the two-step form.
 *
 * Someone who is already signed in is sent to Live rather than shown a
 * second sign-in; a bookmark to `/login` should not sign anyone out.
 */
export default async function LoginPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);
  if (await currentSession()) redirect(`/${locale}`);
  const t = await getTranslations("login");

  return (
    <div className="flex min-h-screen">
      <section className="relative hidden w-[660px] shrink-0 flex-col justify-between overflow-hidden bg-brand px-14 py-12 text-paper lg:flex">
        <div
          aria-hidden="true"
          className="absolute -bottom-40 -right-[120px] h-[520px] w-[520px] rounded-full border border-paper/[.18]"
        />
        <div
          aria-hidden="true"
          className="absolute -bottom-[60px] right-10 h-[280px] w-[280px] rounded-full border border-paper/[.18]"
        />
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-[11px] bg-paper/[.14]">
            <Icon name="leaf" size={24} strokeWidth={1.8} />
          </div>
          <div className="leading-tight">
            <div className="text-md font-semibold">UA Agro</div>
            <div className="text-label opacity-75">Naveen Khushhali Kisan Sewa Kendra</div>
          </div>
        </div>
        <div>
          <p lang="hi" className="font-hero text-hero">
            नमस्ते।
          </p>
          <p className="mt-4.5 max-w-[440px] font-serif text-hero-sub opacity-95">{t("tagline")}</p>
        </div>
        <p className="text-small opacity-70">{t("controlRoom")}</p>
      </section>

      <div className="flex flex-1 items-center justify-center p-8">
        <LoginForm />
      </div>
    </div>
  );
}
