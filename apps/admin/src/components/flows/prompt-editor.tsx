"use client";

import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";

/** The API refuses a longer persona; the counter turns red before that. */
const MAX_CHARS = 12_000;

export function wordCount(text: string): number {
  return text.trim().split(/\s+/).filter(Boolean).length;
}

/**
 * The persona behind the helpline, editable by the people who run it: who
 * the agent is, what it may promise, when it hands over. Saved with the
 * script as a new draft and live on the next call once published.
 * "Restore default" brings back the seed wording without saving anything.
 */
export function PromptEditor({
  value,
  defaultValue,
  disabled,
  onChange,
}: {
  value: string;
  /** The seed wording, when the control plane reports it; without it there is nothing to restore. */
  defaultValue: string | null;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const t = useTranslations("flows.prompt");
  const words = wordCount(value);
  const overLong = value.length > MAX_CHARS;
  const isDefault = defaultValue !== null && value === defaultValue;

  return (
    <div className="flex flex-col gap-2.5 border-t border-inset pt-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="font-semibold">{t("title")}</div>
          <div className="text-small text-muted">{t("explainer")}</div>
        </div>
        {defaultValue !== null ? (
          <Button icon="refresh" disabled={disabled || isDefault} onClick={() => onChange(defaultValue)}>
            {t("restoreDefault")}
          </Button>
        ) : null}
      </div>
      <textarea
        lang="hi"
        aria-label={t("title")}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        rows={16}
        spellCheck={false}
        className="w-full resize-y rounded-panel border border-line bg-surface px-3.5 py-3 font-sans text-body leading-relaxed text-ink focus:border-ink focus:outline-none disabled:bg-inset"
      />
      <div className="flex justify-between text-label">
        <span className="text-muted">{t("words", { count: words })}</span>
        <span className={overLong ? "font-medium text-red-text" : "text-faint"}>
          {t("chars", { count: value.length, max: MAX_CHARS })}
        </span>
      </div>
    </div>
  );
}
