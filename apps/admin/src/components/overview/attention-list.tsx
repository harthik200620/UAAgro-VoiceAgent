import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Icon } from "@/components/ui/icon";
import { Link } from "@/i18n/routing";
import type { AttentionItem } from "@/lib/contract";
import { formatDateTime } from "@/lib/format";
import { resolvePanelHref } from "@/lib/nav";
import { sortAttention } from "@/lib/overview";
import { severityTone } from "@/lib/tones";

/**
 * "Needs attention": what a person has to do, most urgent first. The title
 * is translated from the item's kind rather than shown as the API wrote it,
 * so a Hindi reader gets a Hindi sentence; the detail is the API's and may
 * name a centre or a product.
 */
export function AttentionList({ items }: { items: AttentionItem[] }) {
  const t = useTranslations("overview.attention");
  const sorted = sortAttention(items);

  return (
    <Card className="flex flex-col px-5 pb-3 pt-4.5">
      <div className="flex items-baseline justify-between pb-2">
        <span className="font-semibold">{t("title")}</span>
        <span className="text-small text-muted">{t("count", { count: sorted.length })}</span>
      </div>
      {sorted.length === 0 ? (
        <p className="flex items-center gap-2 py-6 text-ui text-muted">
          <Icon name="check" className="text-green" />
          {t("empty")}
        </p>
      ) : (
        <ul className="flex flex-col">
          {sorted.map((item, index) => {
            const href = resolvePanelHref(item.href);
            const body = (
              <>
                <Chip tone={severityTone(item.severity)} className="mt-0.5 shrink-0">
                  {t(`severity.${item.severity}`)}
                </Chip>
                <div className="min-w-0 flex-1">
                  <div className="text-body font-medium">{t(`kinds.${item.kind}`, { count: item.count })}</div>
                  {item.detail ? <div className="text-small text-muted">{item.detail}</div> : null}
                  {item.at ? (
                    <div className="mt-0.5 font-mono text-meta text-faint">{formatDateTime(item.at)}</div>
                  ) : null}
                </div>
                {href ? <Icon name="chevronRight" size={14} className="mt-1 text-faint" /> : null}
              </>
            );
            const className = clsx(
              "flex items-start gap-3 border-b border-inset py-3 last:border-b-0",
              href && "-mx-2 rounded-panel px-2 hover:bg-paper",
            );
            return (
              <li key={`${item.kind}-${index}`}>
                {href ? (
                  <Link href={href} className={className}>
                    {body}
                  </Link>
                ) : (
                  <div className={className}>{body}</div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
