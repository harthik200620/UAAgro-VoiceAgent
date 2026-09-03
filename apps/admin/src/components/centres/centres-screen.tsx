"use client";

import { useTranslations } from "next-intl";
import dynamic from "next/dynamic";
import { useState, useTransition } from "react";

import { saveCentre } from "@/app/actions/centres";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { CentreRow } from "@/lib/contract";

import { CentreEditor } from "./centre-editor";
import { CentreList } from "./centre-list";
import type { Location } from "./centres-map";

/**
 * Leaflet reads `window` at import time, so the map is loaded in the browser
 * only; until then the card shows the same paper the tiles will cover.
 */
const CentresMap = dynamic(() => import("./centres-map").then((module) => module.CentresMap), {
  ssr: false,
  loading: () => <div className="h-full w-full animate-pulse bg-inset" aria-hidden="true" />,
});

/**
 * The map and the list share one selection: choosing a centre in either
 * flies the map to it and opens its editor below. While an editor is open,
 * a click on the map becomes that centre's location -- the way a manager
 * places a store that has no coordinates yet.
 */
export function CentresScreen({
  centres: initial,
  canEdit,
  canEditStock,
}: {
  centres: CentreRow[];
  canEdit: boolean;
  canEditStock: boolean;
}) {
  const t = useTranslations("centres");
  const [centres, setCentres] = useState(initial);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [picked, setPicked] = useState<Location | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const selected = centres.find((centre) => centre.id === selectedId) ?? null;

  // One primary at a time: the API clears the old one, and so does the list.
  const replace = (centre: CentreRow) =>
    setCentres((current) =>
      current.map((row) =>
        row.id === centre.id ? centre : centre.isPrimary ? { ...row, isPrimary: false } : row,
      ),
    );

  const patch = (centre: CentreRow, change: Parameters<typeof saveCentre>[1]) =>
    startTransition(async () => {
      setError(null);
      const result = await saveCentre(centre.id, change);
      if (result.ok) replace(result.value);
      else setError(result.message);
    });

  const select = (id: string | null) => {
    setSelectedId(id);
    setPicked(null);
  };

  return (
    <>
      {error ? (
        <p role="alert" className="text-ui text-red-text">
          {error}
        </p>
      ) : null}

      <div className="flex flex-wrap items-stretch gap-4">
        <Card className="relative isolate h-[460px] min-w-0 flex-1 basis-[520px] overflow-hidden">
          <CentresMap
            centres={centres}
            selectedId={selectedId}
            picked={picked}
            picking={canEdit && selected !== null}
            onSelect={select}
            onPick={setPicked}
          />
          {canEdit && selected ? (
            <div className="absolute left-3 top-3 z-[500] max-w-[320px] rounded-panel border border-line bg-surface/95 px-3 py-2 text-small shadow-card">
              {t("map.clickToPlace", { name: selected.name })}
            </div>
          ) : null}
        </Card>
        <CentreList
          centres={centres}
          selectedId={selectedId}
          canEdit={canEdit}
          pending={pending}
          onSelect={select}
          onSetPrimary={(centre) => patch(centre, { isPrimary: true })}
          onSetActive={(centre, isActive) => patch(centre, { isActive })}
        />
      </div>

      {selected ? (
        <Card className="px-6 pb-5 pt-4.5">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-baseline gap-2.5">
              <span className="text-md font-semibold">{selected.name}</span>
              <span className="font-mono text-meta text-faint">{selected.code}</span>
            </div>
            <Button variant="ghost" icon="close" onClick={() => select(null)}>
              {t("close")}
            </Button>
          </div>
          <CentreEditor
            key={selected.id}
            centre={selected}
            canEdit={canEdit}
            canEditStock={canEditStock}
            picked={picked}
            onSaved={replace}
          />
        </Card>
      ) : null}
    </>
  );
}
