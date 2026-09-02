"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { askQuestion } from "@/app/actions/knowledge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";
import type { KnowledgeAnswer } from "@/lib/contract";
import { formatMs } from "@/lib/format";

/**
 * "Try a question": what the agent would find, and -- on the second button
 * only, because it is a model call -- what it would say. The retrieval
 * numbers are shown because they are the number the phone farmer waits for.
 *
 * Asked as one kind of call or the other, because a document can be marked
 * for one of them: the operator is shown what *that* call would find.
 */
export function AskPanel({ direction }: { direction: "inbound" | "outbound" }) {
  const t = useTranslations("knowledge.ask");
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<KnowledgeAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const ask = (answer: boolean) =>
    startTransition(async () => {
      setError(null);
      const outcome = await askQuestion(question, answer, direction);
      if (outcome.ok) setResult(outcome.value);
      else setError(outcome.message);
    });

  return (
    <Card className="flex w-[380px] shrink-0 flex-col gap-3.5 px-5 pb-5 pt-4.5">
      <div className="font-semibold">{t("title")}</div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask(false);
        }}
        className="flex flex-col gap-2.5"
      >
        <label className="flex items-center gap-2.5 rounded-panel border border-line bg-surface px-3 py-2.5 focus-within:border-ink">
          <Icon name="search" className="text-muted" />
          <input
            lang="hi"
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder={t("placeholder")}
            aria-label={t("question")}
            className="min-w-0 flex-1 bg-transparent text-name outline-none placeholder:text-faint"
          />
        </label>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" variant="primary" icon="search" disabled={pending || !question.trim()}>
            {t("search")}
          </Button>
          <Button type="button" icon="mic" disabled={pending || !question.trim()} onClick={() => ask(true)}>
            {t("alsoAnswer")}
          </Button>
        </div>
      </form>

      {error ? (
        <p role="alert" className="text-ui text-red-text">
          {error}
        </p>
      ) : null}

      {result ? (
        <div className="flex flex-col gap-3.5" aria-live="polite">
          {result.answer ? (
            <div className="flex flex-col gap-1.5">
              <div className="text-label text-muted">{t("agentWouldSay")}</div>
              <div lang="hi" className="rounded-panel bg-inset px-3.5 py-3 text-name leading-[1.7]">
                {result.answer.text}
              </div>
            </div>
          ) : null}

          <div className="flex flex-col gap-2">
            <div className="text-label text-muted">{t("foundIn")}</div>
            {result.passages.length === 0 ? (
              <p className="text-small text-muted">{t("nothingFound")}</p>
            ) : (
              <ul className="flex flex-col gap-1.5 text-small">
                {result.passages.map((passage, index) => (
                  <li key={index} className="flex flex-col gap-0.5">
                    <div className="flex justify-between gap-2">
                      <span lang="hi" className="inline-flex items-center gap-1.5">
                        <Icon name="file" size={13} className="text-muted" />
                        {passage.documentTitle}
                        {passage.section ? ` · ${passage.section}` : ""}
                      </span>
                      <span className="font-mono text-meta text-muted">{passage.score.toFixed(2)}</span>
                    </div>
                    <p lang="hi" className="line-clamp-2 pl-[19px] text-label text-muted">
                      {passage.snippet}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex justify-between border-t border-inset pt-3 text-small text-muted">
            <span>
              {t("foundInTime")}{" "}
              <span className="font-mono text-label text-ink">{formatMs(result.retrievalMs)}</span>
            </span>
            {result.answer ? (
              <span>
                {t("fullAnswerIn")}{" "}
                <span className="font-mono text-label text-ink">{formatMs(result.answer.totalMs)}</span>
              </span>
            ) : null}
          </div>

          {result.degraded ? <p className="text-label text-amber-text">{t("degraded")}</p> : null}
          {result.answer?.note ? <p className="text-label text-faint">{result.answer.note}</p> : null}
        </div>
      ) : null}
    </Card>
  );
}
