"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";
import { useState, useTransition } from "react";

import { saveCentre } from "@/app/actions/centres";
import { Chip } from "@/components/ui/chip";
import { EmptyState } from "@/components/ui/empty-state";
import { Icon } from "@/components/ui/icon";
import { Table, Td, Th } from "@/components/ui/table";
import { Toggle } from "@/components/ui/toggle";
import type { CentreRow } from "@/lib/contract";

import { CentreEditor } from "./centre-editor";
import { describeDays } from "./working-days";

/**
 * Every centre the session may see. The Open switch is the one control on
 * the row itself -- it is the thing a manager reaches for when the shutter
 * comes down early -- and everything else opens inline.
 */
export function CentresTable({
  centres: initial,
  canEdit,
  canEditStock,
}: {
  centres: CentreRow[];
  canEdit: boolean;
  canEditStock: boolean;
}) {
  const t = useTranslations("centres");
  const days = useTranslations("days");
  const [centres, setCentres] = useState(initial);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const replace = (centre: CentreRow) =>
    setCentres((current) => current.map((row) => (row.id === centre.id ? centre : row)));

  const setActive = (centre: CentreRow, isActive: boolean) =>
    startTransition(async () => {
      setError(null);
      const result = await saveCentre(centre.id, { isActive });
      if (result.ok) replace(result.value);
      else setError(result.message);
    });

  if (centres.length === 0) return <EmptyState>{t("empty")}</EmptyState>;

  return (
    <>
      {error ? (
        <p role="alert" className="px-3.5 pb-2 text-ui text-red-text">
          {error}
        </p>
      ) : null}
      <Table>
        <thead>
          <tr>
            <Th>{t("columns.centre")}</Th>
            <Th>{t("columns.manager")}</Th>
            <Th>{t("columns.managerNumber")}</Th>
            <Th>{t("columns.hours")}</Th>
            <Th>{t("columns.stock")}</Th>
            <Th align="center">{t("columns.open")}</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {centres.map((centre) => {
            const open = openId === centre.id;
            return (
              <CentreRows
                key={centre.id}
                centre={centre}
                open={open}
                onToggleOpen={() => setOpenId(open ? null : centre.id)}
                hours={`${centre.openTime}–${centre.closeTime}`}
                daysLabel={describeDays(centre.workingDays, days)}
                openControl={
                  canEdit ? (
                    <Toggle
                      checked={centre.isActive}
                      disabled={pending}
                      label={t("openLabel", { centre: centre.name })}
                      onChange={(value) => setActive(centre, value)}
                    />
                  ) : (
                    <Chip tone={centre.isActive ? "green" : "grey"}>
                      {centre.isActive ? t("open") : t("closed")}
                    </Chip>
                  )
                }
                editor={
                  open ? (
                    <CentreEditor
                      centre={centre}
                      canEdit={canEdit}
                      canEditStock={canEditStock}
                      onSaved={replace}
                    />
                  ) : null
                }
              />
            );
          })}
        </tbody>
      </Table>
    </>
  );
}

function CentreRows({
  centre,
  open,
  onToggleOpen,
  hours,
  daysLabel,
  openControl,
  editor,
}: {
  centre: CentreRow;
  open: boolean;
  onToggleOpen: () => void;
  hours: string;
  daysLabel: string;
  openControl: React.ReactNode;
  editor: React.ReactNode;
}) {
  const t = useTranslations("centres");

  return (
    <>
      <tr className={clsx("hover:bg-paper", open && "bg-paper")}>
        <Td>
          <button type="button" onClick={onToggleOpen} aria-expanded={open} className="text-left">
            <div className="whitespace-nowrap font-medium">{centre.district}</div>
            <div className="whitespace-nowrap text-label text-muted">
              {centre.block ? `${centre.block} · ` : ""}
              <span className="font-mono text-meta text-faint">{centre.code}</span>
            </div>
          </button>
        </Td>
        <Td>
          <span lang="hi" className="whitespace-nowrap">
            {centre.managerName ?? "—"}
          </span>
        </Td>
        <Td className="whitespace-nowrap">
          <span className="font-mono text-small">{centre.managerNumber ?? "—"}</span>
        </Td>
        <Td>
          <span className="whitespace-nowrap text-muted">
            {hours}
            <br />
            {daysLabel}
          </span>
        </Td>
        <Td>
          {centre.stock.length === 0 ? (
            <span className="text-muted">—</span>
          ) : (
            <div className="flex flex-wrap gap-1">
              {centre.stock.map((item) => (
                <span
                  key={item.inventoryId}
                  lang="hi"
                  className={clsx(
                    "inline-flex items-center gap-1 rounded-tag px-[7px] py-0.5 text-meta",
                    item.isAvailable ? "bg-green-bg text-green-text" : "bg-red-bg text-red-text",
                  )}
                  title={item.isAvailable ? t("inStock") : t("outOfStock")}
                >
                  <Icon name={item.isAvailable ? "check" : "close"} size={11} />
                  {item.productName}
                </span>
              ))}
            </div>
          )}
        </Td>
        <Td align="center">{openControl}</Td>
        <Td align="right">
          <button
            type="button"
            onClick={onToggleOpen}
            aria-label={open ? t("collapse") : t("expand")}
            className="inline-flex p-1"
          >
            <Icon
              name="chevronRight"
              size={14}
              className={clsx("text-faint transition-transform", open && "rotate-90")}
            />
          </button>
        </Td>
      </tr>
      {editor ? (
        <tr className="bg-paper">
          <Td colSpan={7} className="py-4">
            {editor}
          </Td>
        </tr>
      ) : null}
    </>
  );
}
