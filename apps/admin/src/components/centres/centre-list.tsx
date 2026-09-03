"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Chip } from "@/components/ui/chip";
import { EmptyState } from "@/components/ui/empty-state";
import { Icon } from "@/components/ui/icon";
import { Toggle } from "@/components/ui/toggle";
import type { CentreRow } from "@/lib/contract";

import { describeDays } from "./working-days";

/**
 * Every centre the session may see, beside the map: the facts the agent
 * gives a farmer -- where, when, who, what -- and what has run out today.
 * Choosing one opens it on the map and in the editor; the switches on the
 * row are the two things a manager reaches for in a hurry.
 */
export function CentreList({
  centres,
  selectedId,
  canEdit,
  pending,
  onSelect,
  onSetPrimary,
  onSetActive,
}: {
  centres: CentreRow[];
  selectedId: string | null;
  canEdit: boolean;
  pending: boolean;
  onSelect: (id: string) => void;
  onSetPrimary: (centre: CentreRow) => void;
  onSetActive: (centre: CentreRow, isActive: boolean) => void;
}) {
  const t = useTranslations("centres");
  const days = useTranslations("days");
  const openNow = centres.filter((centre) => centre.isActive && centre.openNow).length;

  return (
    <Card className="flex h-[460px] w-full flex-col xl:w-[440px] xl:shrink-0">
      <div className="flex items-baseline justify-between border-b border-inset px-5 py-3.5">
        <span className="font-semibold">{t("listTitle", { count: centres.length })}</span>
        <span className="text-small text-muted">{t("openNowCount", { count: openNow })}</span>
      </div>
      {centres.length === 0 ? (
        <EmptyState>{t("empty")}</EmptyState>
      ) : (
        <ul className="min-h-0 flex-1 overflow-y-auto">
          {centres.map((centre) => {
            const selected = centre.id === selectedId;
            // Fields added on 3 September; an older control plane leaves them out.
            const services = centre.services ?? [];
            const stockOuts = centre.stockOuts ?? 0;
            return (
              <li
                key={centre.id}
                className={clsx("border-b border-inset last:border-b-0", selected && "bg-inset")}
              >
                <button
                  type="button"
                  onClick={() => onSelect(centre.id)}
                  aria-pressed={selected}
                  className="flex w-full flex-col gap-1.5 px-5 py-3.5 text-left hover:bg-paper"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-name font-semibold">{centre.name}</span>
                    {centre.isPrimary ? (
                      <Chip tone="ink">
                        <Icon name="star" size={11} />
                        {t("primary")}
                      </Chip>
                    ) : null}
                    {!centre.isActive ? (
                      <Chip tone="grey">{t("closed")}</Chip>
                    ) : centre.openNow ? (
                      <Chip tone="green">{t("openNow")}</Chip>
                    ) : (
                      <Chip tone="grey">{t("closedNow")}</Chip>
                    )}
                  </div>
                  <div className="text-small text-muted">
                    {[centre.district, centre.block].filter(Boolean).join(" · ")}{" "}
                    <span className="font-mono text-meta text-faint">{centre.code}</span>
                    {centre.latitude === null || centre.longitude === null ? (
                      <span className="ml-2 text-amber-text">{t("noLocation")}</span>
                    ) : null}
                  </div>
                  <div className="text-small text-muted">
                    {centre.openTime}–{centre.closeTime} · {describeDays(centre.workingDays, days)}
                  </div>
                  <div className="flex flex-wrap gap-x-3 text-small">
                    <span>
                      <span className="text-muted">{t("columns.manager")}</span>{" "}
                      {centre.managerName ?? "—"}
                      {centre.managerNumber ? (
                        <span className="ml-1.5 font-mono text-label text-muted">{centre.managerNumber}</span>
                      ) : null}
                    </span>
                    {centre.phone ? (
                      <span>
                        <span className="text-muted">{t("editor.phone")}</span>{" "}
                        <span className="font-mono text-label">{centre.phone}</span>
                      </span>
                    ) : null}
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-small">
                    <span className="text-muted">
                      {services.length > 0 ? services.join(" · ") : t("noServices")}
                    </span>
                    <span className={stockOuts > 0 ? "font-medium text-red-text" : "text-muted"}>
                      {t("stockOuts", { count: stockOuts })}
                    </span>
                  </div>
                </button>
                {canEdit ? (
                  <div className="flex items-center justify-between gap-3 px-5 pb-3">
                    {centre.isPrimary ? (
                      <span className="text-label text-muted">{t("isPrimaryHint")}</span>
                    ) : (
                      <Button icon="star" disabled={pending || !centre.isActive} onClick={() => onSetPrimary(centre)}>
                        {t("setPrimary")}
                      </Button>
                    )}
                    <label className="flex items-center gap-2 text-label text-muted">
                      {t("columns.open")}
                      <Toggle
                        checked={centre.isActive}
                        disabled={pending}
                        label={t("openLabel", { centre: centre.name })}
                        onChange={(value) => onSetActive(centre, value)}
                      />
                    </label>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
