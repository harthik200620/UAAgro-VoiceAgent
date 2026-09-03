"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { askQuestion } from "@/app/actions/knowledge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";
import { Segmented } from "@/components/ui/segmented";
import type { KnowledgeAnswer } from "@/lib/contract";
import { formatMs } from "@/lib/format";

type Direction = "inbound" | "outbound";

/**
 * "Try a question": what the agent would find, and -- when asked to answer
 * like the phone would, because that is a model call -- what it would say.
 * The retrieval numbers are shown because they are the number the farmer
 * waits for.
 *
 * Asked as one kind of call or the other, because a document can be marked
 * for one of them: the operator is shown what *that* call would find.
 */
export function AskPanel() {
  const t = useTranslations("knowledge.ask");
  const [question, setQuestion] = useState("");
  const [direction, setDirection] = useState<Direction>("inbound");
  const [answer, setAnswer] = useState(false);
  const [result, setResult] = useState<KnowledgeAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const ask = () =>
    startTransition(async () => {
      setError(null);
      const outcome = await askQuestion(question, answer, direction);
      if (outcome.ok) setResult(outcome.value);
      else setError(outcome.message);
    });

  return (
    <Card className="flex flex-col gap-3.5 px-5 pb-5 pt-4.5">
      <div className="font-semibold">{t("title")}</div>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          ask();
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
        <div className="flex flex-wrap items-center gap-3">
          <Segmented<Direction>
            label={t("asCall")}
            options={[
              { value: "inbound", label: t("helpline") },
              { value: "outbound", label: t("campaignCall") },
            ]}
            value={direction}
            onChange={setDirection}
            disabled={pending}
          />
          <label className="flex items-center gap-2 text-small text-muted">
            <input
              type="checkbox"
              checked={answer}
              onChange={(event) => setAnswer(event.target.checked)}
              disabled={pending}
              className="h-4 w-4"
            />
            {t("likeThePhone")}
          </label>
        </div>
        <div>
          <Button type="submit" variant="primary" icon={answer ? "mic" : "search"} disabled={pending || !question.trim()}>
            {pending ? t("asking") : answer ? t("askAndAnswer") : t("search")}
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
