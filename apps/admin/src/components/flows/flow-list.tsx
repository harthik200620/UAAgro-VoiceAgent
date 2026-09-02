import { clsx } from "clsx";
import { getTranslations } from "next-intl/server";

import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { Link } from "@/i18n/routing";
import { formatDate } from "@/lib/format";
import type { ScriptGroup } from "@/lib/flows";

import { NewScriptButton } from "./new-script-button";

/** The scripts of one direction, one row per name, the open one on inset. */
export async function FlowList({
  groups,
  currentId,
  sourceId,
}: {
  groups: ScriptGroup[];
  currentId: string | null;
  /** The script a new one is copied from. */
  sourceId: string | null;
}) {
  const t = await getTranslations("flows.list");

  return (
    <Card className="flex w-[260px] shrink-0 flex-col gap-1 px-2.5 py-3.5">
      <div className="flex items-center justify-between px-1.5 pb-2.5 pt-1">
        <span className="font-semibold">{t("title")}</span>
        <NewScriptButton sourceId={sourceId} />
      </div>
      {groups.length === 0 ? (
        <p className="px-1.5 py-4 text-small text-muted">{t("empty")}</p>
      ) : (
        groups.map((group) => {
          const target = group.live ?? group.newest;
          const active = group.versions.some((version) => version.id === currentId);
          return (
            <Link
              key={group.name}
              href={`/flows/${target.id}`}
              aria-current={active ? "page" : undefined}
              className={clsx("flex flex-col gap-1 rounded-panel px-3.5 py-3", active ? "bg-inset" : "hover:bg-paper")}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-base font-semibold">
                  {group.name}
                </span>
                <Chip tone={group.state === "live" ? "green" : "grey"}>{t(`state.${group.state}`)}</Chip>
              </div>
              <div className="text-label text-muted">
                {group.state === "live" && group.live
                  ? t("liveMeta", {
                      version: group.live.version,
                      date: formatDate(group.live.publishedAt ?? group.live.updatedAt),
                      campaigns: group.live.usedByCampaigns,
                    })
                  : group.state === "draft"
                    ? t("draftMeta", { version: group.newest.version })
                    : t("retiredMeta", { version: group.newest.version, date: formatDate(group.newest.updatedAt) })}
              </div>
            </Link>
          );
        })
      )}
    </Card>
  );
}
