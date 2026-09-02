import { getTranslations, setRequestLocale } from "next-intl/server";

import { FORMAT_LOCALE } from "@/i18n/routing";
import { can } from "@/lib/rbac";
import { getInventory } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * §15.1 Catalogue -- the per-centre inventory grid.
 *
 * §15.1 is specific that this is the screen used daily and must be a fast grid
 * rather than a modal per row, so it renders as one dense table with no
 * per-row dialogs.
 *
 * The propagation delay is displayed rather than assumed. §15.1 asks for it,
 * and the reason is concrete: staff who do not know how long an edit takes to
 * reach the agent conclude it did not work and make the edit twice.
 */
export default async function CataloguePage({
  params,
  searchParams,
}: {
  params: Promise<{ locale: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { locale } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("catalogue");

  if (!can(session, "catalogue.view")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const filters = await searchParams;
  const stockOut = filters.stock_out === "true";

  const query = new URLSearchParams({ limit: "300" });
  if (stockOut) query.set("stock_out", "true");

  const { rows, total, propagationSeconds } = await getInventory(
    session!,
    query.toString(),
  );

  return (
    <section>
      <h1 className="mb-1 text-lg font-semibold">{t("title")}</h1>
      <p className="mb-3 text-sm text-muted">
        {t("resultCount", { total })} · {t("propagation", { seconds: propagationSeconds })}
      </p>

      <div className="mb-3 flex gap-3 text-sm">
        <a
          href="?"
          className={!stockOut ? "font-semibold underline underline-offset-2" : "text-muted"}
        >
          {t("all")}
        </a>
        <a
          href="?stock_out=true"
          className={stockOut ? "font-semibold underline underline-offset-2" : "text-muted"}
        >
          {t("stockOut")}
        </a>
      </div>

      <div className="overflow-x-auto">
        <table className="grid-dense w-full min-w-[54rem] border-collapse text-sm">
          <thead className="border-b border-slate-300 text-left dark:border-slate-700">
            <tr>
              <th scope="col">{t("centre")}</th>
              <th scope="col">{t("sku")}</th>
              <th scope="col">{t("product")}</th>
              <th scope="col">{t("packSize")}</th>
              <th scope="col" className="text-right">{t("quantity")}</th>
              <th scope="col" className="text-right">{t("price")}</th>
              <th scope="col">{t("available")}</th>
              <th scope="col">{t("updated")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={`${row.centreId}:${row.variantId}`}
                className="border-b border-slate-100 dark:border-slate-800"
              >
                <td className="font-mono text-xs">{row.centreCode}</td>
                <td className="font-mono text-xs">{row.sku}</td>
                {/* Hindi product name inside an English page. */}
                <td lang="hi">{row.productHi}</td>
                <td className="whitespace-nowrap">{row.packSize}</td>
                {/* On hand minus reserved. Reserved stock is somebody else's,
                    and quoting it is how a farmer drives to a centre for a
                    product that is not there. */}
                <td className={`text-right tabular-nums ${row.quantity === 0 ? "text-warn" : ""}`}>
                  {row.quantity}
                </td>
                <td className="text-right tabular-nums">
                  ₹{row.sellingPriceRupees.toFixed(2)}
                </td>
                <td>
                  <span className={row.isAvailable ? "text-ok" : "text-warn"}>
                    {row.isAvailable ? t("yes") : t("no")}
                  </span>
                </td>
                <td className="whitespace-nowrap text-xs text-muted">
                  {new Date(row.updatedAt).toLocaleDateString(FORMAT_LOCALE)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {rows.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t("noResults")}</p>
      ) : null}
    </section>
  );
}
