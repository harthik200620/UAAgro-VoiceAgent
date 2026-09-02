"use client";

import { useTranslations } from "next-intl";
import { useMemo, useRef, useState } from "react";

import { buttonClass } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Icon } from "@/components/ui/icon";
import { formatDuration } from "@/lib/format";

const RATES = [1, 1.25, 1.5, 2] as const;
const BAR_COUNT = 96;

/**
 * The recording: a plain `<audio>` pointed at the panel's own
 * `/api/recordings/{id}`, and a waveform that follows it.
 *
 * The bars are decorative -- a fixed pattern seeded from the call id, so the
 * same call always draws the same shape -- and fetch nothing. Real peaks
 * would mean downloading and decoding the whole file before the first
 * second plays; the split between ink and grey is what the operator reads,
 * and that follows `currentTime` exactly. The waveform is a slider, so the
 * arrow keys scrub too.
 */
export function AudioPlayer({
  src,
  durationSeconds,
  seed,
  downloadName,
}: {
  src: string;
  durationSeconds: number | null;
  seed: string;
  downloadName: string;
}) {
  const t = useTranslations("call.player");
  const audio = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(durationSeconds ?? 0);
  const [rate, setRate] = useState<(typeof RATES)[number]>(1);
  const [failed, setFailed] = useState(false);
  const bars = useMemo(() => waveformBars(seed), [seed]);
  const progress = duration > 0 ? time / duration : 0;

  const toggle = () => {
    const element = audio.current;
    if (!element) return;
    if (element.paused) void element.play();
    else element.pause();
  };

  const seekTo = (fraction: number) => {
    const element = audio.current;
    if (!element || duration === 0) return;
    element.currentTime = Math.min(duration, Math.max(0, fraction)) * duration;
    setTime(element.currentTime);
  };

  const cycleRate = () => {
    const next = RATES[(RATES.indexOf(rate) + 1) % RATES.length] ?? 1;
    setRate(next);
    if (audio.current) audio.current.playbackRate = next;
  };

  return (
    <Card className="flex flex-wrap items-center gap-4 px-5 py-4">
      <audio
        ref={audio}
        src={src}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
        onLoadedMetadata={(event) => {
          if (Number.isFinite(event.currentTarget.duration)) setDuration(event.currentTarget.duration);
        }}
        onError={() => setFailed(true)}
      />
      <button
        type="button"
        onClick={toggle}
        disabled={failed}
        aria-label={playing ? t("pause") : t("play")}
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-ink text-paper disabled:opacity-50"
      >
        <Icon name={playing ? "pause" : "play"} size={20} />
      </button>

      <div
        role="slider"
        tabIndex={0}
        aria-label={t("position")}
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={Math.round(time)}
        aria-valuetext={`${formatDuration(time)} / ${formatDuration(duration)}`}
        onClick={(event) => {
          const box = event.currentTarget.getBoundingClientRect();
          seekTo((event.clientX - box.left) / box.width);
        }}
        onKeyDown={(event) => {
          if (duration === 0) return;
          if (event.key === "ArrowRight") seekTo(progress + 5 / duration);
          if (event.key === "ArrowLeft") seekTo(progress - 5 / duration);
          if (event.key === " " || event.key === "Enter") {
            event.preventDefault();
            toggle();
          }
        }}
        className="min-w-[200px] flex-1 cursor-pointer"
      >
        <svg width="100%" height="40" viewBox="0 0 672 40" preserveAspectRatio="none" aria-hidden="true">
          {bars.map((height, index) => (
            <rect
              key={index}
              x={index * 7}
              y={(40 - height) / 2}
              width="4"
              height={height}
              rx="2"
              className={index / BAR_COUNT <= progress ? "fill-ink" : "fill-grey"}
            />
          ))}
        </svg>
      </div>

      <div className="flex shrink-0 items-center gap-3.5">
        <span className="font-mono text-body">
          {formatDuration(time)} / {formatDuration(duration)}
        </span>
        <button
          type="button"
          onClick={cycleRate}
          aria-label={t("speed")}
          className="relative px-1 text-small text-muted after:absolute after:-inset-2.5 after:content-[''] hover:text-ink"
        >
          {rate}×
        </button>
        <a href={src} download={downloadName} className={buttonClass("ghost", "sm")}>
          <Icon name="download" />
          <span>{t("download")}</span>
        </a>
      </div>

      {failed ? (
        <p role="alert" className="w-full text-small text-red-text">
          {t("failed")}
        </p>
      ) : null}
    </Card>
  );
}

/** 96 bar heights between 6 and 37, the same for the same seed. */
function waveformBars(seed: string): number[] {
  let hash = 2166136261;
  for (const char of seed) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  let state = hash >>> 0;
  const next = () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let value = Math.imul(state ^ (state >>> 15), 1 | state);
    value = (value + Math.imul(value ^ (value >>> 7), 61 | value)) ^ value;
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
  return Array.from({ length: BAR_COUNT }, () => 6 + Math.round(next() * 31));
}
