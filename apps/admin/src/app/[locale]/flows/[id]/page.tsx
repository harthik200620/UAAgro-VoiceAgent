import { getTranslations, setRequestLocale } from "next-intl/server";

import { can } from "@/lib/rbac";
import { getFlowDetail } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * One agent configuration version (§15.1, §1 N6).
 *
 * The prompt is rendered by a Server Component and never sent to the browser
 * as data. That distinction matters: the text appears in the HTML because
 * somebody with `flows.edit` asked to read it, but no client bundle holds it,
 * no fetch from the browser can retrieve it, and it is not in a serialised
 * props payload for a Client Component to pick up.
 *
 * Read-only for now. §15.1 also wants a side-by-side version diff and a
 * browser sandbox test call before publish; both are listed in the panel's
 * known gaps rather than sketched here, because a prompt editor that saves
 * without a diff is how an unreviewed change reaches every caller.
 */
export default async function FlowDetailPage({
  params,
}: {
  params: Promise<{ locale: string; id: string }>;
}) {
  const { locale, id } = await params;
  setRequestLocale(locale);

  const session = await currentSession();
  const t = await getTranslations("flows");

  if (!can(session, "flows.edit")) {
    const errors = await getTranslations("errors");
    return <p className="text-danger">{errors("forbidden")}</p>;
  }

  const flow = await getFlowDetail(session!, id);

  const blocks: { label: string; value: string }[] = [
    { label: t("systemPrompt"), value: flow.systemPrompt },
    { label: t("greeting"), value: flow.greetingTemplate },
    { label: t("closing"), value: flow.closingTemplate },
  ];

  const settings: { label: string; value: Record<string, unknown> }[] = [
    { label: t("escalationRules"), value: flow.escalationRules },
    { label: t("languageRoutes"), value: flow.languageRoutes },
    { label: t("llmSettings"), value: flow.llmSettings },
    { label: t("ttsSettings"), value: flow.ttsSettings },
    { label: t("guardrails"), value: flow.guardrails },
  ];

  return (
    <section>
      <h1 className="text-lg font-semibold">
        {flow.name} <span className="text-muted">v{flow.version}</span>
      </h1>
      <p className="mb-4 text-sm text-muted">
        {t(`flowTypes.${flow.flowType}`)} ·{" "}
        <span className={flow.isPublished ? "text-ok" : ""}>
          {flow.isPublished ? t("live") : t("draft")}
        </span>
        {flow.publishedByName ? ` · ${flow.publishedByName}` : ""}
      </p>

      {blocks.map((block) => (
        <div key={block.label} className="mb-4">
          <h2 className="mb-1 text-sm font-semibold">{block.label}</h2>
          {/* `lang="hi"` because the prompt *is* Hindi -- it is the text the
              agent speaks, and §11.3's register rules are about that Hindi.
              Marking it also stops the browser rendering Devanagari in a font
              chosen for English.

              `whitespace-pre-wrap` because the prompt's line breaks are
              load-bearing: §6.1 caches on an exact prefix, so reflowing it
              here would show the reader something other than what is sent. */}
          <pre
            lang="hi"
            className="max-h-96 overflow-auto whitespace-pre-wrap rounded border border-slate-200 bg-slate-50 p-3 text-xs leading-relaxed dark:border-slate-800 dark:bg-slate-900"
          >
            {block.value}
          </pre>
        </div>
      ))}

      <div className="mb-4">
        <h2 className="mb-1 text-sm font-semibold">{t("tools")}</h2>
        <ul className="flex flex-wrap gap-1">
          {flow.toolAllowlist.map((tool) => (
            <li
              key={tool}
              className="rounded border border-slate-200 px-2 py-0.5 text-xs dark:border-slate-800"
            >
              {tool}
            </li>
          ))}
        </ul>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {settings.map((setting) => (
          <div key={setting.label}>
            <h2 className="mb-1 text-sm font-semibold">{setting.label}</h2>
            <pre className="max-h-64 overflow-auto rounded border border-slate-200 bg-slate-50 p-2 text-xs dark:border-slate-800 dark:bg-slate-900">
              {JSON.stringify(setting.value, null, 2)}
            </pre>
          </div>
        ))}
      </div>
    </section>
  );
}
