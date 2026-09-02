"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState, useTransition } from "react";

import { loadStock, saveCentre, setStockAvailable } from "@/app/actions/centres";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Field, inputClass } from "@/components/ui/field";
import { Toggle } from "@/components/ui/toggle";
import type { CentrePatch, CentreRow, StockRow } from "@/lib/contract";
import { formatCount } from "@/lib/format";

import { WEEK, type Weekday } from "./working-days";

type Draft = {
  name: string;
  nameHi: string;
  district: string;
  block: string;
  state: string;
  pincode: string;
  latitude: string;
  longitude: string;
  managerName: string;
  managerNumber: string;
  phone: string;
  openTime: string;
  closeTime: string;
  workingDays: string[];
};

function draftOf(centre: CentreRow): Draft {
  return {
    name: centre.name,
    nameHi: centre.nameHi ?? "",
    district: centre.district,
    block: centre.block ?? "",
    state: centre.state,
    pincode: centre.pincode ?? "",
    latitude: centre.latitude === null ? "" : String(centre.latitude),
    longitude: centre.longitude === null ? "" : String(centre.longitude),
    managerName: centre.managerName ?? "",
    managerNumber: centre.managerNumber ?? "",
    phone: centre.phone ?? "",
    openTime: centre.openTime,
    closeTime: centre.closeTime,
    workingDays: centre.workingDays,
  };
}

/** Only what changed goes to the API, so two people editing different fields do not overwrite each other. */
function patchFrom(before: Draft, after: Draft): CentrePatch {
  const patch: CentrePatch = {};
  const changed = (key: keyof Draft) => before[key] !== after[key];
  if (changed("name")) patch.name = after.name;
  if (changed("nameHi")) patch.nameHi = after.nameHi;
  if (changed("district")) patch.district = after.district;
  if (changed("block")) patch.block = after.block;
  if (changed("state")) patch.state = after.state;
  if (changed("pincode")) patch.pincode = after.pincode;
  if (changed("latitude") && after.latitude !== "") patch.latitude = Number(after.latitude);
  if (changed("longitude") && after.longitude !== "") patch.longitude = Number(after.longitude);
  if (changed("managerName")) patch.managerName = after.managerName;
  if (changed("managerNumber")) patch.managerNumber = after.managerNumber;
  if (changed("phone")) patch.phone = after.phone;
  if (changed("openTime")) patch.openTime = after.openTime;
  if (changed("closeTime")) patch.closeTime = after.closeTime;
  if (before.workingDays.join() !== after.workingDays.join()) patch.workingDays = after.workingDays;
  return patch;
}

/**
 * A centre opened inline: every field, and the stock list with its
 * availability switches. The stock comes from `/stock` on open, because the
 * row only carries the first six items.
 */
export function CentreEditor({
  centre,
  canEdit,
  canEditStock,
  onSaved,
}: {
  centre: CentreRow;
  canEdit: boolean;
  canEditStock: boolean;
  onSaved: (centre: CentreRow) => void;
}) {
  const t = useTranslations("centres.editor");
  const days = useTranslations("days");
  const [draft, setDraft] = useState(() => draftOf(centre));
  const [stock, setStock] = useState<StockRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pending, startTransition] = useTransition();

  useEffect(() => {
    let cancelled = false;
    loadStock(centre.id).then((result) => {
      if (cancelled) return;
      if (result.ok) setStock(result.value);
      else setError(result.message);
    });
    return () => {
      cancelled = true;
    };
  }, [centre.id]);

  const patch = patchFrom(draftOf(centre), draft);
  const dirty = Object.keys(patch).length > 0;

  const set = (key: keyof Draft) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setDraft((current) => ({ ...current, [key]: event.target.value }));

  const save = () =>
    startTransition(async () => {
      setError(null);
      setSaved(false);
      const result = await saveCentre(centre.id, patch);
      if (result.ok) {
        onSaved(result.value);
        setDraft(draftOf(result.value));
        setSaved(true);
      } else setError(result.message);
    });

  const switchStock = (row: StockRow, isAvailable: boolean) =>
    startTransition(async () => {
      setError(null);
      const result = await setStockAvailable(row.inventoryId, isAvailable);
      if (result.ok) {
        const updated = result.value;
        setStock((current) =>
          current ? current.map((item) => (item.inventoryId === updated.inventoryId ? updated : item)) : current,
        );
      } else setError(result.message);
    });

  const text = (key: keyof Draft, label: string, extra: Record<string, string> = {}) => (
    <Field label={label}>
      <input
        value={draft[key] as string}
        onChange={set(key)}
        disabled={!canEdit}
        className={inputClass}
        {...extra}
      />
    </Field>
  );

  return (
    <div className="flex items-start gap-6 px-1">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (dirty) save();
        }}
        className="grid flex-1 grid-cols-3 gap-3"
      >
        {text("name", t("name"))}
        <Field label={t("nameHi")}>
          <input lang="hi" value={draft.nameHi} onChange={set("nameHi")} disabled={!canEdit} className={inputClass} />
        </Field>
        <div className="font-mono text-small text-muted self-end pb-2.5">{centre.code}</div>
        {text("district", t("district"))}
        {text("block", t("block"))}
        {text("state", t("state"))}
        {text("pincode", t("pincode"), { inputMode: "numeric" })}
        {text("latitude", t("latitude"), { inputMode: "decimal" })}
        {text("longitude", t("longitude"), { inputMode: "decimal" })}
        <Field label={t("managerName")}>
          <input value={draft.managerName} onChange={set("managerName")} disabled={!canEdit} className={inputClass} />
        </Field>
        {text("managerNumber", t("managerNumber"), { type: "tel", className: `${inputClass} font-mono` })}
        {text("phone", t("phone"), { type: "tel", className: `${inputClass} font-mono` })}
        {text("openTime", t("opens"), { type: "time" })}
        {text("closeTime", t("closes"), { type: "time" })}
        <fieldset className="flex flex-col gap-[5px]">
          <legend className="text-label text-muted">{t("workingDays")}</legend>
          <div className="flex flex-wrap gap-1.5 pt-1">
            {WEEK.map((day: Weekday) => {
              const on = draft.workingDays.includes(day);
              return (
                <label
                  key={day}
                  className={`cursor-pointer rounded-tag border px-2 py-1 text-label ${on ? "border-ink bg-ink text-paper" : "border-line bg-surface text-muted"}`}
                >
                  <input
                    type="checkbox"
                    className="sr-only"
                    checked={on}
                    disabled={!canEdit}
                    onChange={() =>
                      setDraft((current) => ({
                        ...current,
                        workingDays: on
                          ? current.workingDays.filter((d) => d !== day)
                          : WEEK.filter((d) => d === day || current.workingDays.includes(d)),
                      }))
                    }
                  />
                  {days(day)}
                </label>
              );
            })}
          </div>
        </fieldset>
        {canEdit ? (
          <div className="col-span-3 flex items-center justify-end gap-3">
            {saved ? (
              <span role="status" className="text-small text-green-text">
                {t("saved")}
              </span>
            ) : null}
            <Button type="submit" variant="primary" icon="check" disabled={!dirty || pending}>
              {t("save")}
            </Button>
          </div>
        ) : null}
        {error ? (
          <p role="alert" className="col-span-3 text-ui text-red-text">
            {error}
          </p>
        ) : null}
      </form>

      <div className="w-[320px] shrink-0">
        <div className="mb-2 font-semibold">{t("stockTitle")}</div>
        {stock === null ? (
          <p className="text-small text-muted">{t("stockLoading")}</p>
        ) : stock.length === 0 ? (
          <p className="text-small text-muted">{t("stockEmpty")}</p>
        ) : (
          <ul className="flex flex-col">
            {stock.map((row) => (
              <li key={row.inventoryId} className="flex items-center justify-between gap-3 border-b border-inset py-2 text-body">
                <div className="min-w-0">
                  <div className="truncate font-medium">
                    {row.productName}
                  </div>
                  <div className="text-label text-muted">
                    {[
                      row.variantName,
                      row.price === null ? null : `₹${formatCount(row.price)}`,
                      row.stockQty === null ? null : t("qty", { count: row.stockQty }),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                </div>
                {canEditStock ? (
                  <Toggle
                    checked={row.isAvailable}
                    disabled={pending}
                    label={t("availableLabel", { product: row.productName })}
                    onChange={(value) => switchStock(row, value)}
                  />
                ) : (
                  <Chip tone={row.isAvailable ? "green" : "red"}>
                    {row.isAvailable ? t("inStock") : t("outOfStock")}
                  </Chip>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
