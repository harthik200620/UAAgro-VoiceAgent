"use client";

import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { approveAndStart } from "@/app/actions/campaigns";
import { CampaignChip } from "@/components/status/campaign-chip";
import { Button, ButtonLink } from "@/components/ui/button";
import type { CampaignImport } from "@/lib/contract";
import { formatCount } from "@/lib/format";
import { humanize, messageKey } from "@/lib/tones";

/**
 * What the compliance gate did with the list, in numbers an operator can act
 * on. "1,200 pasted, 340 to call" alone makes people look for a way around
 * the gate; "DND removed 610" makes them fix the list. Every check that
 * removed anyone is named, and a blocked campaign is shown as blocked rather
 * than as one with fewer numbers.
 */
export function GateResult({ result }: { result: CampaignImport }) {
  const t = useTranslations("newCampaign.gate");
  const checks = useTranslations("checks");
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const { campaign } = result;
  const blocked = campaign.blockedBy.length > 0;
  const checkLabel = (check: string) => {
    const key = messageKey(check);
    return checks.has(key) ? checks(key) : humanize(check);
  };

  return (
    <div className="flex flex-col gap-4.5">
      <div className="flex items-center gap-2.5">
        <span lang="hi" className="text-xl font-semibold">
          {campaign.name}
        </span>
        <CampaignChip status={campaign.status} />
      </div>

      <div className="flex gap-8">
        <Figure label={t("imported")} value={formatCount(result.imported)} />
        <Figure label={t("toCall")} value={formatCount(campaign.counts.total - campaign.counts.removed)} />
        <Figure label={t("removed")} value={formatCount(campaign.counts.removed)} />
      </div>

      {result.removedBy.length > 0 ? (
        <dl className="flex flex-col gap-1.5 text-ui">
          {result.removedBy.map((entry) => (
            <div key={entry.check} className="flex justify-between gap-4 border-b border-inset pb-1.5">
              <dt className="text-muted">{checkLabel(entry.check)}</dt>
              <dd className="font-mono">{formatCount(entry.count)}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {result.invalid.length > 0 ? (
        <div className="text-ui">
          <div className="text-label text-muted">{t("invalidLines", { count: result.invalid.length })}</div>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {result.invalid.map((line, index) => (
              <li key={`${line}-${index}`} className="rounded-tag bg-inset px-2 py-0.5 font-mono text-label">
                {line}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {blocked ? (
        <div role="alert" className="rounded-panel border border-red bg-red-bg px-3.5 py-3 text-ui text-red-text">
          <div className="font-semibold">{t("blocked")}</div>
          <ul className="mt-1 list-inside list-disc">
            {campaign.blockedBy.map((check) => (
              <li key={check}>{checkLabel(check)}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="text-ui text-red-text">
          {error}
        </p>
      ) : null}

      <div className="flex items-center justify-end gap-3">
        {!blocked && !campaign.canApprove ? (
          <span className="text-small text-muted">{t("needsAnotherApprover")}</span>
        ) : null}
        <ButtonLink href={`/outbound/${campaign.id}`} size="md">
          {t("open")}
        </ButtonLink>
        {!blocked && campaign.canApprove ? (
          <Button
            variant="primary"
            size="md"
            icon="play"
            disabled={pending}
            onClick={() =>
              startTransition(async () => {
                setError(null);
                const outcome = await approveAndStart(campaign.id);
                setError(outcome.message);
              })
            }
          >
            {pending ? t("starting") : t("approveAndStart")}
          </Button>
        ) : null}
      </div>
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="text-label text-muted">{label}</div>
      <div className="font-serif text-num">{value}</div>
    </div>
  );
}
