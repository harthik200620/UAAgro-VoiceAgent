"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { previewScript } from "@/app/actions/flows";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";
import type { FlowPreview } from "@/lib/contract";

/**
 * "How it will sound": the step as the voice will read it, with every digit
 * and date the API turned into words marked. The preview is a cheap call
 * and follows the text with a short debounce; the audio is a vendor call and
 * only plays on a click.
 */
export function PreviewPanel({ flowId, text, step }: { flowId: string; text: string; step: number }) {
  const t = useTranslations("flows.preview");
  const [preview, setPreview] = useState<FlowPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [loadingAudio, setLoadingAudio] = useState(false);
  const audio = useRef<HTMLAudioElement>(null);
  const objectUrl = useRef<string | null>(null);

  // Debounced, and guarded so a slow answer for text the operator has already
  // changed cannot land on top of the newer one.
  useEffect(() => {
    if (!text.trim()) {
      setPreview(null);
      return;
    }
    let stale = false;
    const timer = window.setTimeout(() => {
      void previewScript(flowId, text).then((result) => {
        if (stale) return;
        if (result.ok) {
          setPreview(result.value);
          setError(null);
        } else setError(result.message);
      });
    }, 600);
    return () => {
      stale = true;
      window.clearTimeout(timer);
    };
  }, [flowId, text]);

  useEffect(
    () => () => {
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    },
    [],
  );

  const play = async () => {
    setAudioError(null);
    setLoadingAudio(true);
    try {
      const response = await fetch(`/api/flows/${encodeURIComponent(flowId)}/audio`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!response.ok) {
        let message = t("audioFailed");
        try {
          const payload = (await response.json()) as { error?: { message?: string; remedy?: string } };
          message = payload.error?.remedy ?? payload.error?.message ?? message;
        } catch {
          // No JSON body: the generic line stands.
        }
        setAudioError(message);
        return;
      }
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
      objectUrl.current = URL.createObjectURL(await response.blob());
      if (audio.current) {
        audio.current.src = objectUrl.current;
        await audio.current.play();
      }
    } catch {
      setAudioError(t("audioFailed"));
    } finally {
      setLoadingAudio(false);
    }
  };

  return (
    <Card className="flex flex-col gap-3 px-5 py-4.5">
      <div className="flex items-center gap-2 font-semibold">
        <Icon name="mic" />
        {t("title")}
      </div>
      <div className="text-small text-muted">{t("explainer", { step })}</div>

      {preview ? (
        <>
          <div lang="hi" className="rounded-panel bg-inset px-3.5 py-3 text-name leading-[1.75]">
            {highlight(preview.spoken, preview.substitutions.map((s) => s.to)).map((piece, index) =>
              typeof piece === "string" ? (
                <span key={index}>{piece}</span>
              ) : (
                <mark key={index} className="rounded-[3px] bg-amber-bg px-[3px] text-amber-text">
                  {piece.mark}
                </mark>
              ),
            )}
          </div>
          <dl className="flex flex-col gap-1.5 text-small text-muted">
            {preview.substitutions.map((substitution, index) => (
              <div key={index} className="flex justify-between gap-3">
                <dt>{substitution.from}</dt>
                <dd lang="hi" className="text-ink">
                  {substitution.to}
                </dd>
              </div>
            ))}
            <div className="flex justify-between gap-3">
              <dt>{t("words")}</dt>
              <dd className="text-ink">{t("wordsValue", { words: preview.words, seconds: Math.round(preview.seconds) })}</dd>
            </div>
          </dl>
        </>
      ) : (
        <p className="text-small text-faint">{error ?? t("empty")}</p>
      )}

      <div>
        <Button icon="play" disabled={!text.trim() || loadingAudio} onClick={() => void play()}>
          {loadingAudio ? t("rendering") : t("play")}
        </Button>
      </div>
      <audio ref={audio} className="hidden" />
      {audioError ? (
        <p role="alert" className="text-small text-red-text">
          {audioError}
        </p>
      ) : null}
    </Card>
  );
}

/** Split the spoken text around each substituted phrase, first occurrence, left to right. */
function highlight(spoken: string, marks: string[]): (string | { mark: string })[] {
  const pieces: (string | { mark: string })[] = [];
  let cursor = 0;
  while (cursor < spoken.length) {
    let bestAt = -1;
    let best = "";
    for (const mark of marks) {
      if (!mark) continue;
      const at = spoken.indexOf(mark, cursor);
      if (at !== -1 && (bestAt === -1 || at < bestAt)) {
        bestAt = at;
        best = mark;
      }
    }
    if (bestAt === -1) break;
    if (bestAt > cursor) pieces.push(spoken.slice(cursor, bestAt));
    pieces.push({ mark: best });
    cursor = bestAt + best.length;
  }
  if (cursor < spoken.length) pieces.push(spoken.slice(cursor));
  return pieces;
}
