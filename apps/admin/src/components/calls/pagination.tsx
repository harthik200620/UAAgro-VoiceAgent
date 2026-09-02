import { getTranslations } from "next-intl/server";

import { buttonClass } from "@/components/ui/button";
import { Link } from "@/i18n/routing";
import { formatCount } from "@/lib/format";

/** "1–50 of 312" with Previous / Next, each a link that keeps every filter. */
export async function Pagination({
  total,
  offset,
  pageSize,
  query,
}: {
  total: number;
  offset: number;
  pageSize: number;
  query: Record<string, string>;
}) {
  const t = await getTranslations("calls.pagination");
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(total, offset + pageSize);
  const pageHref = (nextOffset: number) => ({
    pathname: "/calls",
    query: nextOffset > 0 ? { ...query, offset: String(nextOffset) } : query,
  });

  return (
    <div className="flex items-center gap-3 text-small text-muted">
      <span>{t("range", { first: formatCount(first), last: formatCount(last), total: formatCount(total) })}</span>
      {offset > 0 ? (
        <Link href={pageHref(Math.max(0, offset - pageSize))} className={buttonClass("secondary", "sm")}>
          {t("previous")}
        </Link>
      ) : null}
      {last < total ? (
        <Link href={pageHref(offset + pageSize)} className={buttonClass("secondary", "sm")}>
          {t("next")}
        </Link>
      ) : null}
    </div>
  );
}
