import type { CampaignCounts } from "@/lib/contract";

/**
 * Done, not reached and in progress as one bar over the grey of what is
 * still waiting. Widths are shares of the whole list, so a campaign that is
 * half done looks half done whatever its size.
 */
export function ProgressBar({ counts, label }: { counts: CampaignCounts; label: string }) {
  const share = (count: number) => (counts.total === 0 ? 0 : (count / counts.total) * 100);
  const segments = [
    { key: "done", width: share(counts.done), className: "bg-green" },
    { key: "noAnswer", width: share(counts.noAnswer), className: "bg-red" },
    { key: "inCall", width: share(counts.inCall), className: "bg-amber" },
  ];

  return (
    <div
      role="img"
      aria-label={label}
      className="flex h-2.5 gap-0.5 overflow-hidden rounded-full bg-grey"
    >
      {segments
        .filter((segment) => segment.width > 0)
        .map((segment) => (
          <div key={segment.key} className={segment.className} style={{ width: `${segment.width}%` }} />
        ))}
    </div>
  );
}
