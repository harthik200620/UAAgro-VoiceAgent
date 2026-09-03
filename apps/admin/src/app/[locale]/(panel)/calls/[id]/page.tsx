import { getTranslations } from "next-intl/server";

import { AudioPlayer } from "@/components/call/audio-player";
import { DetailsCard } from "@/components/call/details-card";
import { EventsCard } from "@/components/call/events-card";
import { ReplySpeedCard } from "@/components/call/reply-speed-card";
import { ShareButton } from "@/components/call/share-button";
import { StatsCard } from "@/components/call/stats-card";
import { SummariseButton } from "@/components/call/summarise-button";
import { Last4 } from "@/components/status/last4";
import { OutcomeChip } from "@/components/status/outcome-chip";
import { Transcript, type TranscriptItem } from "@/components/transcript/transcript";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Forbidden } from "@/components/ui/forbidden";
import { Icon } from "@/components/ui/icon";
import { Unavailable } from "@/components/ui/unavailable";
import { Link } from "@/i18n/routing";
import { formatDateTime, formatDuration } from "@/lib/format";
import { can } from "@/lib/rbac";
import { humanize } from "@/lib/tones";
import { getCall } from "@/server/api";
import { currentSession } from "@/server/session";

export const dynamic = "force-dynamic";

/**
 * One call: the recording, what happened in a sentence, the transcript, and
 * how fast the agent answered. Read on the server in one request; only the
 * player, the share button and "Summarise again" carry JavaScript.
 */
export default async function CallPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const session = await currentSession();
  if (!session || !can(session, "calls.view")) return <Forbidden />;

  const call = await getCall(session, id);
  const t = await getTranslations("call");
  const directions = await getTranslations("directions");
  // Fields added on 3 September; an older control plane leaves them out and the page still reads.
  const intents = call.intents ?? [];

  const firstReply = call.turns.find(
    (turn) => turn.role === "agent" && turn.latency !== null && !turn.latency.fromCache,
  );
  const items: TranscriptItem[] = [
    ...call.turns.map((turn): TranscriptItem => ({ kind: "turn", turn })),
    ...call.events.map((event): TranscriptItem => ({ kind: "event", ...event })),
  ].sort((a, b) => (a.kind === "turn" ? a.turn.at : (a.at ?? 0)) - (b.kind === "turn" ? b.turn.at : (b.at ?? 0)));

  const length =
    call.durationSeconds === null
      ? null
      : call.durationSeconds < 60
        ? t("seconds", { count: Math.round(call.durationSeconds) })
        : formatDuration(call.durationSeconds);

  return (
    <>
      <div className="flex flex-col gap-3.5">
        <Link
          href={call.campaignId ? `/outbound/${call.campaignId}` : "/calls"}
          className="inline-flex items-center gap-1.5 text-body text-muted hover:text-ink"
        >
          <Icon name="chevronLeft" size={14} />
          {call.campaignId ? (
            <>
              {t("backToCampaign")} · <span>{call.campaignName ?? call.campaignId}</span>
            </>
          ) : (
            t("backToCalls")
          )}
        </Link>
        <div className="flex items-end justify-between gap-5">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-title-sm font-semibold">
                {call.farmerName ?? t("unknownFarmer")}
              </h1>
              <Last4 value={call.callerLast4} className="text-base" />
              <OutcomeChip outcome={call.outcome} />
              {call.isTest ? <Chip tone="grey">{t("testCall")}</Chip> : null}
            </div>
            <p className="mt-1.5 text-ui text-muted">
              {[
                directions(call.direction),
                call.centreName ? t("centreName", { name: call.centreName }) : call.centreCode,
                formatDateTime(call.startedAt),
                length,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
            {intents.length > 0 ? (
              <ul className="mt-2 flex flex-wrap gap-1.5" aria-label={t("intents")}>
                {intents.map((intent) => (
                  <li key={intent} className="rounded-tag bg-inset px-2 py-0.5 text-label text-muted">
                    {humanize(intent)}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
          <div className="flex gap-2.5">
            <Unavailable icon="phone" label={t("callAgain")} reason={t("comingWithTelephony")} size="md" />
            <SummariseButton callId={call.id} />
            <ShareButton />
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-start gap-4">
        <div className="flex min-w-0 flex-1 basis-[560px] flex-col gap-4">
          {call.recording.available ? (
            <AudioPlayer
              src={`/api/recordings/${encodeURIComponent(call.id)}`}
              durationSeconds={call.recording.durationSeconds}
              seed={call.id}
              downloadName={`${call.callRef}.wav`}
            />
          ) : (
            <Card className="px-5 py-4 text-ui text-muted">{t("noRecording")}</Card>
          )}

          <Card className="flex flex-col gap-2 px-5.5 py-4.5">
            <div className="text-small font-medium text-muted">{t("whatHappened")}</div>
            {call.summaryHi || call.summaryEn ? (
              <>
                {call.summaryHi ? (
                  <p lang="hi" className="text-lead">
                    {call.summaryHi}
                  </p>
                ) : null}
                {call.summaryEn ? <p className="text-body text-muted">{call.summaryEn}</p> : null}
              </>
            ) : (
              <p className="text-ui text-muted">{t("noSummary")}</p>
            )}
            {call.followUps.length > 0 ? (
              <ul className="mt-1 flex flex-wrap gap-1.5" aria-label={t("details.followUps")}>
                {call.followUps.map((followUp) => (
                  <li key={followUp} className="inline-flex items-center gap-1.5 rounded-tag border border-line px-2 py-0.5 text-label text-muted">
                    <Icon name="check" size={12} />
                    {followUp}
                  </li>
                ))}
              </ul>
            ) : null}
          </Card>

          <Card className="flex flex-col gap-3.5 px-5.5 pb-5 pt-4.5">
            <div className="flex items-center justify-between">
              <div className="font-semibold">{t("transcript")}</div>
              <div className="text-small text-muted">{t("transcriptNote")}</div>
            </div>
            <Transcript items={items} firstReplyIndex={firstReply?.turnIndex ?? null} />
          </Card>
        </div>

        <div className="flex w-full shrink-0 flex-col gap-4 xl:w-[380px]">
          <ReplySpeedCard turns={call.turns} firstReplyMs={call.firstReplyMs} />
          {call.stats ? <StatsCard stats={call.stats} /> : null}
          <DetailsCard call={call} />
          <EventsCard events={call.events} />
        </div>
      </div>
    </>
  );
}
