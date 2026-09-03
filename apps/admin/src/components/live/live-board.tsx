"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useReducer, useRef, useState } from "react";

import { loadCallHistory } from "@/app/actions/live";
import { Last4 } from "@/components/status/last4";
import { languageName } from "@/components/status/language-name";
import { OutcomeChip } from "@/components/status/outcome-chip";
import { Transcript, type TranscriptItem } from "@/components/transcript/transcript";
import { Unavailable } from "@/components/ui/unavailable";
import { ButtonLink, buttonClass } from "@/components/ui/button";
import { Card, CardTitle } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Icon } from "@/components/ui/icon";
import { PageHeader } from "@/components/ui/page-header";
import { StatCard } from "@/components/ui/stat-card";
import { useEventStream } from "@/hooks/use-event-stream";
import { useNow } from "@/hooks/use-now";
import { Link } from "@/i18n/routing";
import type { LiveSnapshot, TelephonyStatus } from "@/lib/contract";
import { formatCount, formatLongDate, formatMs, formatTime } from "@/lib/format";
import {
  applyLiveEvent,
  initialLiveState,
  LIVE_EVENT_NAMES,
  parseLiveEvent,
  type CallNote,
  type CallTranscript,
} from "@/lib/live-reducer";

import { CallCard } from "./call-card";
import { FinishedTable } from "./finished-table";

/**
 * The Live screen after the server has drawn it: the snapshot becomes state,
 * `/api/events/live` feeds the reducer, and everything on the page follows.
 *
 * Selection is the one piece of state the reducer does not own. When the
 * selected call ends it stays on the right, marked ended, until the operator
 * picks another -- a transcript that vanished mid-sentence is worse than one
 * with a closing line.
 */
export function LiveBoard({
  snapshot,
  telephony,
  canIntervene,
  renderedAt,
}: {
  snapshot: LiveSnapshot;
  /** Null when the control plane does not report it; the board only loses the test-page link. */
  telephony: TelephonyStatus | null;
  canIntervene: boolean;
  /** The server's clock at render, so the first client frame matches the HTML. */
  renderedAt: string;
}) {
  const t = useTranslations("live");
  const languages = useTranslations("languages");
  const directions = useTranslations("directions");
  const [state, dispatch] = useReducer(applyLiveEvent, snapshot, initialLiveState);
  const [selectedId, setSelectedId] = useState<string | null>(snapshot.calls[0]?.id ?? null);
  const now = useNow(renderedAt, 1000);

  const handlers = useMemo(
    () =>
      Object.fromEntries(
        LIVE_EVENT_NAMES.map((name) => [
          name,
          (data: unknown) => {
            const event = parseLiveEvent(name, data);
            if (event) dispatch(event);
          },
        ]),
      ),
    [],
  );
  const stream = useEventStream("/api/events/live", handlers);

  // With nothing chosen, follow the oldest call so the right pane is never
  // blank while the phones are ringing.
  const firstCallId = state.calls[0]?.id ?? null;
  useEffect(() => {
    if (selectedId === null && firstCallId !== null) setSelectedId(firstCallId);
  }, [selectedId, firstCallId]);

  // A call that began before this page opened has turns the stream never
  // sent. Ask once per call; the reducer merges what comes back.
  const requested = useRef(new Set<string>());
  useEffect(() => {
    if (!selectedId || requested.current.has(selectedId)) return;
    requested.current.add(selectedId);
    const callId = selectedId;
    loadCallHistory(callId).then((history) => {
      if (history) dispatch({ name: "history", data: { callId, ...history } });
    });
  }, [selectedId]);

  const selectedCall = state.calls.find((call) => call.id === selectedId) ?? null;
  const selectedTranscript = selectedId ? state.transcripts[selectedId] : undefined;
  const selectedFinished = selectedId
    ? state.recent.find((call) => call.id === selectedId) ?? null
    : null;
  const { today } = state;

  return (
    <>
      <PageHeader
        title={t("title")}
        subtitle={`${formatLongDate(now)} · ${formatTime(now)} · ${t("subtitle")}`}
      >
        {stream !== "live" ? <Chip tone="grey">{t(`stream.${stream}`)}</Chip> : null}
        {telephony ? <TelephonyChips status={telephony} /> : null}
        <Chip tone="amber" size="lg" pulse={state.calls.length > 0}>
          <span aria-live="polite">{t("inProgress", { count: state.calls.length })}</span>
          <span className="font-normal opacity-80">· {t("ofCapacity", { capacity: state.capacity })}</span>
        </Chip>
      </PageHeader>

      <div className="flex gap-4">
        <StatCard
          label={t("stats.callsToday")}
          value={formatCount(today.calls)}
          note={t("stats.inOut", { inbound: today.inbound, outbound: today.outbound })}
        />
        <StatCard
          label={t("stats.firstReply")}
          value={today.firstReplyP50Ms === null ? "—" : formatCount(Math.round(today.firstReplyP50Ms))}
          unit="ms"
          note={t("stats.p95", { p95: formatMs(today.firstReplyP95Ms) })}
        />
        <StatCard
          label={t("stats.handled")}
          value={today.handledByAgentPct === null ? "—" : String(Math.round(today.handledByAgentPct))}
          unit="%"
          note={t("stats.handedOver", { count: today.transferred })}
        />
        <StatCard
          label={t("stats.offerAccepted")}
          value={formatCount(today.offersAccepted)}
          unit={`/ ${formatCount(today.offersPitched)}`}
          note={t("stats.pressedOne")}
        />
      </div>

      <div className="flex h-[470px] gap-4">
        <Card className="flex w-[372px] shrink-0 flex-col gap-2.5 px-4 pb-4 pt-4.5">
          <CardTitle className="px-1 pb-1.5" aside={t("updatesLive")}>
            {t("inProgressTitle")}
          </CardTitle>
          {state.calls.length === 0 ? (
            <p className="px-1 py-6 text-center text-ui text-muted">{t("noCalls")}</p>
          ) : (
            <ul className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
              {state.calls.map((call) => (
                <CallCard
                  key={call.id}
                  call={call}
                  selected={call.id === selectedId}
                  now={now}
                  onSelect={setSelectedId}
                />
              ))}
            </ul>
          )}
        </Card>

        <Card className="flex min-w-0 flex-1 flex-col gap-3.5 px-5.5 pb-4 pt-4.5">
          {selectedId && (selectedCall || selectedFinished) ? (
            <>
              <div className="flex items-center justify-between gap-3 border-b border-inset pb-3">
                <div className="flex min-w-0 items-center gap-3">
                  <span lang="hi" className="truncate text-md font-semibold">
                    {(selectedCall ?? selectedFinished)?.farmerName ?? t("unknownFarmer")}
                  </span>
                  <Last4
                    value={(selectedCall ?? selectedFinished)?.callerLast4 ?? null}
                    className="text-small"
                  />
                  <span className="text-faint">·</span>
                  <span className="truncate text-body text-muted">
                    {selectedCall
                      ? [
                          directions(selectedCall.direction),
                          selectedCall.centreName ?? selectedCall.centreCode ?? t("noCentre"),
                          languageName(selectedCall.language, languages),
                        ].join(" · ")
                      : t("callEnded")}
                  </span>
                </div>
                {selectedCall ? (
                  canIntervene ? (
                    <div className="flex items-center gap-2">
                      <Unavailable icon="mic" label={t("actions.listen")} reason={t("comingWithTelephony")} />
                      <Unavailable
                        icon="transfer"
                        label={t("actions.handOver")}
                        reason={t("comingWithTelephony")}
                      />
                      <Unavailable
                        icon="close"
                        label={t("actions.end")}
                        reason={t("comingWithTelephony")}
                        danger
                      />
                    </div>
                  ) : null
                ) : (
                  <ButtonLink href={`/calls/${selectedId}`} icon="chevronRight">
                    {t("openCall")}
                  </ButtonLink>
                )}
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto pr-1" aria-live="polite">
                <Transcript
                  items={transcriptItems(selectedTranscript, t)}
                  live={
                    selectedCall
                      ? {
                          activity: selectedCall.activity,
                          text: liveText(selectedCall.activity, selectedTranscript, t),
                          at: (now.getTime() - new Date(selectedCall.startedAt).getTime()) / 1000,
                        }
                      : null
                  }
                />
                {selectedFinished ? (
                  <div className="mt-3.5 flex items-center gap-2 text-small text-muted">
                    {t("endedWith")} <OutcomeChip outcome={selectedFinished.outcome} dtmf={selectedFinished.dtmf} />
                  </div>
                ) : null}
              </div>
            </>
          ) : (
            <p className="m-auto text-ui text-muted">{t("pickACall")}</p>
          )}
        </Card>
      </div>

      <Card className="px-2 pb-1.5 pt-4">
        <div className="flex items-baseline justify-between px-3.5 pb-2">
          <div className="font-semibold">{t("finishedToday")}</div>
          <Link href="/calls" className="text-small text-muted hover:text-ink">
            {t("allCalls")} →
          </Link>
        </div>
        <FinishedTable rows={state.recent} />
      </Card>
    </>
  );
}

/**
 * How calls reach the panel right now: the simulator (answered from a browser
 * page, nothing dialled), a line that is not configured yet, and -- in
 * development -- the worker's test page, opened in its own tab.
 */
function TelephonyChips({ status }: { status: TelephonyStatus }) {
  const t = useTranslations("live.telephony");
  return (
    <>
      {status.mode === "simulator" ? <Chip tone="grey">{t("simulator")}</Chip> : null}
      {!status.configured ? (
        <Chip tone="amber" className="max-w-[320px]">
          <span className="truncate" title={status.remedy ?? undefined}>
            {t("notConfigured")}
          </span>
        </Chip>
      ) : null}
      {status.browserCallUrl ? (
        <a
          href={status.browserCallUrl}
          target="_blank"
          rel="noopener noreferrer"
          className={buttonClass("secondary", "md")}
        >
          <Icon name="external" />
          <span>{t("testPage")}</span>
        </a>
      ) : null}
    </>
  );
}

type Translate = ReturnType<typeof useTranslations<"live">>;

/** Turns and notes in call order; notes without a timestamp go last. */
function transcriptItems(transcript: CallTranscript | undefined, t: Translate): TranscriptItem[] {
  if (!transcript) return [];
  const turns: TranscriptItem[] = transcript.turns.map((turn) => ({ kind: "turn", turn }));
  const notes: TranscriptItem[] = transcript.notes.map((note) => ({
    kind: "event",
    at: note.at,
    type: note.kind === "event" ? note.type : note.kind,
    text: noteText(note, t),
  }));
  return [...turns, ...notes].sort((a, b) => {
    const atA = a.kind === "turn" ? a.turn.at : (a.at ?? Number.POSITIVE_INFINITY);
    const atB = b.kind === "turn" ? b.turn.at : (b.at ?? Number.POSITIVE_INFINITY);
    return atA - atB;
  });
}

function noteText(note: CallNote, t: Translate): string {
  switch (note.kind) {
    case "dtmf": {
      const hint = t.has(`keys.${note.digit}`) ? ` — ${t(`keys.${note.digit}`)}` : "";
      return `${t("pressed", { digit: note.digit })}${hint}`;
    }
    case "transfer":
      return note.reason ? `${t("handedTo", { to: note.to })} · ${note.reason}` : t("handedTo", { to: note.to });
    case "ended":
      return t("callEnded");
    case "event":
      return note.text;
  }
}

function liveText(
  activity: LiveSnapshot["calls"][number]["activity"],
  transcript: CallTranscript | undefined,
  t: Translate,
): string {
  if (activity === "transferring") {
    const transfer = transcript?.notes.find((note) => note.kind === "transfer");
    return transfer && transfer.kind === "transfer"
      ? `${t("activity.transferring")} · ${transfer.to}`
      : t("activity.transferring");
  }
  return t(`activity.${activity}`);
}
