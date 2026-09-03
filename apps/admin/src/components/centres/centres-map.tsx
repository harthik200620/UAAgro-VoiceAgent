"use client";

import "leaflet/dist/leaflet.css";

import L from "leaflet";
import { useEffect, useMemo } from "react";
import { MapContainer, Marker, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";

import type { CentreRow } from "@/lib/contract";

export type Location = { latitude: number; longitude: number };

/** Where the map opens with nothing to show: the state capital. */
const LUCKNOW: [number, number] = [26.85, 80.95];
const WIDE_ZOOM = 7;
const CENTRE_ZOOM = 12;

/**
 * The pins are drawn here rather than taken from Leaflet's image set: an ink
 * pin for a centre, the brand green for the primary one, a muted pin for a
 * location just picked and not yet saved. Leaflet's default marker images
 * are therefore never requested, which is also why their URLs need no
 * repair under a bundler.
 */
function pin(fill: string, size: number): L.DivIcon {
  const width = size;
  const height = Math.round(size * 1.36);
  return L.divIcon({
    className: "",
    iconSize: [width, height],
    iconAnchor: [width / 2, height],
    tooltipAnchor: [0, -height + 4],
    html: `<svg width="${width}" height="${height}" viewBox="0 0 22 30" aria-hidden="true"><path d="M11 29s-9-9.6-9-17a9 9 0 0 1 18 0c0 7.4-9 17-9 17z" fill="${fill}" stroke="#FFFFFF" stroke-width="1.5"/><circle cx="11" cy="12" r="3.2" fill="#FFFFFF"/></svg>`,
  });
}

const PINS = {
  centre: pin("#17160F", 22),
  selected: pin("#17160F", 28),
  primary: pin("#1E5B3A", 28),
  primarySelected: pin("#1E5B3A", 34),
  picked: pin("#6B6759", 22),
};

function iconFor(centre: CentreRow, selected: boolean): L.DivIcon {
  if (centre.isPrimary) return selected ? PINS.primarySelected : PINS.primary;
  return selected ? PINS.selected : PINS.centre;
}

type Placed = CentreRow & { latitude: number; longitude: number };

function isPlaced(centre: CentreRow): centre is Placed {
  return centre.latitude !== null && centre.longitude !== null;
}

export function CentresMap({
  centres,
  selectedId,
  picked,
  picking,
  onSelect,
  onPick,
}: {
  centres: CentreRow[];
  selectedId: string | null;
  /** A location clicked on the map for the open editor, not saved yet. */
  picked: Location | null;
  /** Whether a click on the map is a location for the selected centre. */
  picking: boolean;
  onSelect: (id: string) => void;
  onPick: (location: Location) => void;
}) {
  const placed = useMemo(() => centres.filter(isPlaced), [centres]);
  const selected = placed.find((centre) => centre.id === selectedId) ?? null;

  return (
    <MapContainer
      center={LUCKNOW}
      zoom={WIDE_ZOOM}
      scrollWheelZoom={false}
      className="h-full w-full bg-inset"
      attributionControl
    >
      <TileLayer
        url="https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors'
      />
      <FitAll placed={placed} />
      <FollowSelection selected={selected} />
      <PickLocation picking={picking} onPick={onPick} />
      {placed.map((centre) => (
        <Marker
          key={centre.id}
          position={[centre.latitude, centre.longitude]}
          icon={iconFor(centre, centre.id === selectedId)}
          zIndexOffset={centre.id === selectedId ? 1000 : centre.isPrimary ? 500 : 0}
          eventHandlers={{ click: () => onSelect(centre.id) }}
        >
          <Tooltip direction="top">
            <span className="font-sans text-small">
              {centre.name}
              {centre.isPrimary ? " ★" : ""}
            </span>
          </Tooltip>
        </Marker>
      ))}
      {picked ? <Marker position={[picked.latitude, picked.longitude]} icon={PINS.picked} interactive={false} /> : null}
    </MapContainer>
  );
}

/** On first paint, every pin in view; with none, the state at a glance. */
function FitAll({ placed }: { placed: Placed[] }) {
  const map = useMap();
  const count = placed.length;
  useEffect(() => {
    if (count === 0) {
      map.setView(LUCKNOW, WIDE_ZOOM);
      return;
    }
    map.fitBounds(
      L.latLngBounds(placed.map((centre) => [centre.latitude, centre.longitude] as [number, number])),
      { padding: [32, 32], maxZoom: CENTRE_ZOOM },
    );
    // Only the first paint: a pin moved by the editor should not yank the view around.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, count]);
  return null;
}

/** The chosen centre glides into view; the zoom only ever tightens. */
function FollowSelection({ selected }: { selected: Placed | null }) {
  const map = useMap();
  useEffect(() => {
    if (!selected) return;
    map.flyTo([selected.latitude, selected.longitude], Math.max(map.getZoom(), CENTRE_ZOOM), {
      duration: 0.6,
    });
  }, [map, selected]);
  return null;
}

/** A click is a location while an editor is open; the cursor says so. */
function PickLocation({ picking, onPick }: { picking: boolean; onPick: (location: Location) => void }) {
  const map = useMapEvents({
    click(event) {
      if (!picking) return;
      onPick({
        latitude: Number(event.latlng.lat.toFixed(6)),
        longitude: Number(event.latlng.lng.toFixed(6)),
      });
    },
  });
  useEffect(() => {
    map.getContainer().style.cursor = picking ? "crosshair" : "";
  }, [map, picking]);
  return null;
}
