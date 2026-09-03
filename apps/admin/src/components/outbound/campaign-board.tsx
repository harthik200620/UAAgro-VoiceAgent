"use client";

import { useTranslations } from "next-intl";
import { useMemo, useReducer, useState, useTransition } from "react";

import { controlCampaign, setCampaignConcurrency } from "@/app/actions/campaigns";
import { CampaignChip } from "@/components/status/campaign-chip";
import { Button, ButtonLink, buttonClass } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Dialog } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Icon } from "@/components/ui/icon";
import { PageHeader } from "@/components/ui/page-header";
import { Segmented } from "@/components/ui/segmented";
import { useEventStream } from "@/hooks/use-event-stream";
import {
  applyCampaignEvent,
  CAMPAIGN_EVENT_NAMES,
  parseCampaignEvent,
} from "@/lib/campaign-reducer";
import type { CampaignControl, CampaignDetail } from "@/lib/contract";
import { formatCount, formatMs, formatTime } from "@/lib/format";
import { humanize, messageKey } from "@/lib/tones";

import { ContactCard } from "./contact-card";
import { ProgressBar } from "./progress-bar";

const CONCURRENCY = [1, 5, 10, 20, 30].map((value) => ({ value, label: String(value) }));

/**
 * One campaign, live: the header with its controls, the numbers strip, and
 * the wall. The server's snapshot becomes state; `/api/events/campaigns/{id}`
 * feeds the reducer; a control's own response is applied at once so the
 * button reflects what just happened without waiting for the stream.
 */
export function CampaignBoard({
  campaign,
  canControl,
  canApprove,
  canCreate,
}: {
  campaign: CampaignDetail;
  canControl: boolean;
  canApprove: boolean;
  canCreate: boolean;
}) {
  const t = useTranslations("campaign");
  const exclusions = useTranslations("exclusions");
  const checks = useTranslations("checks");
  const [state, dispatch] = useReducer(applyCampaignEvent, campaign);
  const [pending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);
  const [confirmingStop, setConfirmingStop] = useState(false);

  const handlers = useMemo(
    () =>
      Object.fromEntries(
        CAMPAIGN_EVENT_NAMES.map((name) => [
          name,
          (data: unknown) => {
            const event = parseCampaignEvent(name, data);
            if (event) dispatch(event);
          },
        ]),
      ),
    [],
  );
  const stream = useEventStream(`/api/events/campaigns/${encodeURIComponent(campaign.id)}`, handlers);

  const run = (control: CampaignControl) =>
    startTransition(async () => {
      setError(null);
      const result = await controlCampaign(state.id, control);
      if (result.ok) dispatch({ name: "campaign.updated", data: result.value });
      else setError(result.message);
    });

  const changeConcurrency = (value: number) =>
    startTransition(async () => {
      setError(null);
      const result = await setCampaignConcurrency(state.id, value);
      if (result.ok) dispatch({ name: "campaign.updated", data: result.value });
      else setError(result.message);
    });

  const { counts } = state;
  const removedReasons = [
    ...new Set(
      state.contacts
        .filter((contact) => contact.status === "removed" && contact.removedReason)
        .map((contact) => contact.removedReason as string),
    ),
  ].map((reason) => (exclusions.has(messageKey(reason)) ? exclusions(messageKey(reason)) : humanize(reason)));

  return (
    <>
      <PageHeader title={t("title")} subtitle={t("subtitle")}>
        {stream !== "live" ? <Chip tone="grey">{t(`stream.${stream}`)}</Chip> : null}
        {state.status === "pending_approval" && canApprove && state.canApprove ? (
          <Button variant="primary" size="md" icon="check" disabled={pending} onClick={() => run("approve")}>
            {t("approve")}
          </Button>
        ) : null}
        {(state.status === "approved" || state.status === "scheduled") && canControl ? (
          <Button variant="primary" size="md" icon="play" disabled={pending} onClick={() => run("start")}>
            {t("start")}
          </Button>
        ) : null}
        {state.status === "running" && canControl ? (
          <Button size="md" icon="pause" disabled={pending} onClick={() => run("pause")}>
            {t("pause")}
          </Button>
        ) : null}
        {state.status === "paused" && canControl ? (
          <Button variant="primary" size="md" icon="play" disabled={pending} onClick={() => run("resume")}>
            {t("resume")}
          </Button>
        ) : null}
        {(state.status === "running" || state.status === "paused") && canControl ? (
          <Button variant="danger" size="md" icon="stop" disabled={pending} onClick={() => setConfirmingStop(true)}>
            {t("stop")}
          </Button>
        ) : null}
        {canCreate ? (
          <ButtonLink href="/outbound/new" variant="primary" size="md" icon="plus">
            {t("newCampaign")}
          </ButtonLink>
        ) : null}
      </PageHeader>

      {error ? (
        <p role="alert" className="text-ui text-red-text">
          {error}
        </p>
      ) : null}

      {state.blockedBy.length > 0 && state.status !== "running" && state.status !== "completed" ? (
        // Why it is not calling: the gate names what is missing.
        <p className="text-ui text-red-text">
          {t("blockedBy", {
            reasons: state.blockedBy
              .map((check) => (checks.has(messageKey(check)) ? checks(messageKey(check)) : humanize(check)))
              .join(" · "),
          })}
        </p>
      ) : null}

      <Card className="flex flex-col gap-4 px-6 pb-4.5 pt-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2.5">
              <span className="text-xl font-semibold">
                {state.name}
              </span>
              <CampaignChip status={state.status} />
            </div>
            <div className="mt-1 text-body text-muted">
              {t("flow")}{" "}
              <b className="font-semibold text-ink">
                {state.flowName ?? "—"}
                {state.flowVersion === null ? "" : ` · v${state.flowVersion}`}
              </b>
              {" · "}
              {state.startedAt ? t("startedAt", { time: formatTime(state.startedAt) }) : t("notStarted")}
              {" · "}
              {t("callingHours", { from: state.windowStart, to: state.windowEnd })}
            </div>
          </div>
          <div className="flex items-center gap-2.5 text-body text-muted">
            {t("callsAtATime")}
            <Segmented
              label={t("callsAtATime")}
              options={CONCURRENCY}
              value={state.maxConcurrent}
              onChange={changeConcurrency}
              disabled={!canControl || pending}
            />
          </div>
        </div>

        <ProgressBar counts={counts} label={t("progressLabel", { done: counts.done, total: counts.total })} />

        <div className="flex items-end justify-between gap-6">
          <div className="flex gap-8.5">
            <MiniStat label={t("counts.done")} value={counts.done} swatch="bg-green" />
            <MiniStat label={t("counts.noAnswer")} value={counts.noAnswer} swatch="bg-red" />
            <MiniStat label={t("counts.inCall")} value={counts.inCall} swatch="bg-amber" />
            <MiniStat label={t("counts.waiting")} value={counts.waiting} swatch="bg-grey" />
            <div className="w-px bg-line" aria-hidden="true" />
            <MiniStat label={t("counts.pressed1")} value={counts.pressed1} swatch="bg-ink" />
            <MiniStat label={t("counts.pressed2")} value={counts.pressed2} swatch="bg-ink" />
            <MiniStat label={t("counts.optedOut")} value={counts.optedOut} swatch="bg-ink" />
          </div>
          <div className="text-right text-small text-muted">
            {t("firstReplyOnCampaign")}
            <br />
            <span className="font-serif text-num-sm text-ink">{formatMs(state.firstReplyP50Ms)}</span>{" "}
            <span>{t("median")}</span>
          </div>
        </div>
      </Card>

      <Card className="flex items-center justify-between gap-4 px-5 py-3.5">
        <div className="flex items-center gap-3.5">
          <div className="font-semibold">{t("numbers")}</div>
          <div className="text-body text-muted">
            {t("pasted", { count: formatCount(counts.total), time: formatTime(state.createdAt) })}
            {" · "}
            {t("removedAutomatically", { count: counts.removed })}
            {removedReasons.length > 0 ? (
              <span className="text-faint"> ({removedReasons.join(", ")})</span>
            ) : null}
            {" · "}
            {t("toCall", { count: formatCount(counts.total - counts.removed) })}
          </div>
        </div>
        <a
          href={`/api/campaigns/${encodeURIComponent(state.id)}/export`}
          download
          className={buttonClass("secondary", "sm")}
        >
          <Icon name="download" />
          <span>{t("downloadResults")}</span>
        </a>
      </Card>

      <Card className="flex flex-col gap-3.5 px-5 pb-5 pt-4.5">
        <div className="flex items-center justify-between">
          <div className="font-semibold">
            {t("wallTitle")} <span className="font-normal text-muted">· {t("wallHint")}</span>
          </div>
          <div className="flex gap-4" aria-hidden="true">
            <LegendItem label={t("counts.done")} swatch="bg-green" />
            <LegendItem label={t("counts.noAnswer")} swatch="bg-red" />
            <LegendItem label={t("counts.inCall")} swatch="bg-amber" />
            <LegendItem label={t("counts.waiting")} swatch="bg-grey" />
          </div>
        </div>
        {state.contacts.length === 0 ? (
          <EmptyState>{t("noContacts")}</EmptyState>
        ) : (
          <ul className="grid grid-cols-10 gap-2" aria-live="polite">
            {state.contacts.map((contact) => (
              <ContactCard key={contact.id} contact={contact} />
            ))}
          </ul>
        )}
      </Card>

      <Dialog open={confirmingStop} onClose={() => setConfirmingStop(false)} title={t("stopTitle")}>
        <p className="text-ui text-muted">{t("stopQuestion")}</p>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirmingStop(false)}>
            {t("cancel")}
          </Button>
          <Button
            variant="danger"
            icon="stop"
            disabled={pending}
            onClick={() => {
              setConfirmingStop(false);
              run("stop");
            }}
          >
            {t("stop")}
          </Button>
        </div>
      </Dialog>
    </>
  );
}

function MiniStat({ label, value, swatch }: { label: string; value: number; swatch: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="flex items-center gap-1.5 text-label text-muted">
        <span aria-hidden="true" className={`h-2 w-2 rounded-sm ${swatch}`} />
        {label}
      </div>
      <div className="font-serif text-num">{formatCount(value)}</div>
    </div>
  );
}

function LegendItem({ label, swatch }: { label: string; swatch: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-label text-muted">
      <span className={`h-2.5 w-2.5 rounded-[3px] ${swatch}`} />
      {label}
    </span>
  );
}
