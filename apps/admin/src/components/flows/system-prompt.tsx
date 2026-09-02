import { getTranslations } from "next-intl/server";

/**
 * The prompt behind the script, read-only, folded away.
 *
 * Rendered by a Server Component and handed to the editor as children: the
 * text reaches the page because an ops manager asked to read it, but no
 * client bundle holds it and no fetch from the browser can retrieve it.
 * `whitespace-pre-wrap` because the line breaks are load-bearing -- the
 * model is cached on an exact prefix, and reflowing would show something
 * other than what is sent.
 */
export async function SystemPrompt({ prompt }: { prompt: string }) {
  const t = await getTranslations("flows.editor");
  return (
    <details className="border-t border-inset pt-4">
      <summary className="cursor-pointer text-small font-medium text-muted hover:text-ink">
        {t("systemPrompt")}
      </summary>
      <pre
        lang="hi"
        className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap rounded-panel border border-line bg-inset p-3.5 font-sans text-body leading-relaxed"
      >
        {prompt}
      </pre>
    </details>
  );
}
