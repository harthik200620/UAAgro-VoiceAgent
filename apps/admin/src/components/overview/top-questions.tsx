import { useTranslations } from "next-intl";

import { Card } from "@/components/ui/card";
import { OutcomeChip } from "@/components/status/outcome-chip";
import type { Overview } from "@/lib/contract";
import { formatCount } from "@/lib/format";

/** What farmers asked most, and how calls ended: two short lists, each with a bar against its largest entry. */
export function TopQuestions({
  questions,
  outcomes,
}: {
  questions: Overview["topQuestions"];
  outcomes: Overview["outcomes"];
}) {
  const t = useTranslations("overview.questions");
  const largest = Math.max(1, ...questions.map((item) => item.count));

  return (
    <Card className="flex w-full flex-col gap-4 px-5.5 pb-4.5 pt-4.5 lg:w-[340px] lg:shrink-0">
      <div>
        <div className="mb-2 font-semibold">{t("title")}</div>
        {questions.length === 0 ? (
          <p className="text-small text-muted">{t("empty")}</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {questions.map((item) => (
              <li key={item.intent} className="flex flex-col gap-1">
                <div className="flex justify-between gap-3 text-small">
                  <span lang="hi" className="truncate">
                    {item.label}
                  </span>
                  <span className="font-mono text-label">{formatCount(item.count)}</span>
                </div>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-inset" aria-hidden="true">
                  <div className="h-full rounded-full bg-ink" style={{ width: `${(item.count / largest) * 100}%` }} />
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>

      <div className="border-t border-inset pt-3">
        <div className="mb-2 font-semibold">{t("outcomes")}</div>
        {outcomes.length === 0 ? (
          <p className="text-small text-muted">{t("noOutcomes")}</p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {outcomes.map((outcome) => (
              <li key={outcome.key} className="flex items-center justify-between gap-3">
                <OutcomeChip outcome={outcome.key} />
                <span className="font-mono text-label">{formatCount(outcome.count)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
