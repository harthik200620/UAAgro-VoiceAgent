import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { currentSession } from "@/server/session";
import { apiFetch, type MaskedSecretList } from "@/server/settings";

/**
 * §15.1 Settings -- vendor keys, write-only.
 *
 * "Write-only; masked after save, never returned by any API." The API returns
 * presence and a four-character hint and nothing else, so there is no code path
 * here that could render a key even if someone wrote one: the value never
 * arrives.
 *
 * The hint exists because the alternative is worse. Without it an operator
 * facing a failing integration cannot tell whether the key is wrong or the
 * vendor is down, and the only way to find out is to overwrite a working key.
 */
export default async function SettingsPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("settings");
  const errors = await getTranslations("errors");

  if (!can(session, "settings.edit")) {
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const { secrets } = await apiFetch<MaskedSecretList>("/admin/settings/secrets", {
    session: session!,
    revalidate: 0,
  });

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>
      <h2 className="mb-1 text-base font-medium">{t("vendorKeys")}</h2>
      <p className="mb-3 text-sm text-muted">{t("writeOnly")}</p>

      <table className="grid-dense w-full max-w-2xl border-collapse text-sm">
        <tbody>
          {secrets.map((secret) => (
            <tr
              key={secret.name}
              className="border-b border-slate-100 dark:border-slate-800"
            >
              <th scope="row" className="font-mono font-normal">
                {secret.name}
              </th>
              <td className={secret.isSet ? "text-ok" : "text-warn"}>
                {secret.isSet ? t("keyIsSet") : t("keyNotSet")}
              </td>
              <td className="font-mono text-muted">{secret.hint ?? "—"}</td>
              <td className="text-xs text-muted">{secret.updatedAt ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
