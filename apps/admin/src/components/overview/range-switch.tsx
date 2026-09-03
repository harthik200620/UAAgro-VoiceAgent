import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { Link } from "@/i18n/routing";
import type { OverviewRange } from "@/lib/contract";

const RANGES: readonly OverviewRange[] = ["today", "7d", "30d"];

/** Today · 7 days · 30 days, as links: the range is part of the address, so a view can be sent to someone. */
export function RangeSwitch({ active }: { active: OverviewRange }) {
  const t = useTranslations("overview.ranges");
  return (
    <nav
      aria-label={t("label")}
      className="inline-flex overflow-hidden rounded-btn border border-line bg-surface"
    >
      {RANGES.map((range) => (
        <Link
          key={range}
          href={{ pathname: "/", query: range === "today" ? {} : { range } }}
          aria-current={range === active ? "page" : undefined}
          className={clsx(
            "min-h-[36px] px-3.5 py-2 text-small font-medium transition-colors",
            range === active ? "bg-ink font-semibold text-paper hover:text-paper" : "text-ink hover:bg-inset",
          )}
        >
          {t(range)}
        </Link>
      ))}
    </nav>
  );
}
