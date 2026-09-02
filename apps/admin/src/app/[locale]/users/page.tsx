import { getTranslations, setRequestLocale } from "next-intl/server";

import { FORMAT_LOCALE } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getUsers } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Users & Roles.
 *
 * The MFA column is the one to read first. §17 makes TOTP mandatory, so an
 * account without it is not a preference somebody has expressed -- it is the
 * account an attacker uses, and it is invisible unless a screen names it.
 *
 * Invite, deactivate and session revocation are not built; they are in the
 * panel's known gaps. What is here is the read that makes the gap survivable:
 * an operator can at least see who exists, what they can reach, and who is
 * unprotected.
 */
export default async function UsersPage({
  params,
}: {
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("users");

  if (!can(session, "users.manage")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const rows = await getUsers(session!);
  const withoutMfa = rows.filter((row) => row.isActive && !row.mfaEnrolled);

  return (
    <section>
      <h1 className="mb-3 text-lg font-semibold">{t("title")}</h1>

      {withoutMfa.length > 0 ? (
        <div
          role="status"
          className="mb-4 rounded border border-danger/40 bg-danger/10 px-3 py-2 text-sm"
        >
          <p className="font-medium text-danger">
            {t("missingMfa", { count: withoutMfa.length })}
          </p>
          <p className="mt-1 text-xs text-muted">{t("missingMfaDetail")}</p>
        </div>
      ) : null}

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[46rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("name")}</th>
              <th scope="col">{t("email")}</th>
              <th scope="col">{t("role")}</th>
              <th scope="col" className="text-right">{t("centres")}</th>
              <th scope="col">{t("mfa")}</th>
              <th scope="col">{t("state")}</th>
              <th scope="col">{t("lastLogin")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.id}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <td>{row.fullName}</td>
                <td className="text-xs">{row.email}</td>
                <td>{t(`roles.${row.role}`)}</td>
                <td className="text-right tabular-nums">
                  {/* An org-wide role has no centre rows and sees everything;
                      showing "0" there would read as "no access". */}
                  {["super_admin", "ops_manager", "auditor"].includes(row.role)
                    ? t("allCentres")
                    : row.centreIds.length}
                </td>
                <td>
                  <span className={row.mfaEnrolled ? "text-ok" : "text-danger"}>
                    {row.mfaEnrolled ? t("mfaOn") : t("mfaOff")}
                  </span>
                </td>
                <td>
                  <span className={row.isActive ? "" : "text-muted"}>
                    {row.isActive ? t("active") : t("inactive")}
                  </span>
                </td>
                <td className="whitespace-nowrap text-xs text-muted">
                  {row.lastLoginAt
                    ? new Date(row.lastLoginAt).toLocaleDateString(FORMAT_LOCALE)
                    : t("never")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
