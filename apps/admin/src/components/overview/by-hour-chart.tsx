"use client";

import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Table, Td, Th } from "@/components/ui/table";
import type { ByHourRow, OverviewRange } from "@/lib/contract";
import { formatCount } from "@/lib/format";
import { byHourTotals, chartScale, labelEvery, labelledBars } from "@/lib/overview";

/**
 * Calls by hour (or by day over 7 and 30 days), inbound and outbound side by
 * side. Plain SVG: two bars per column, a hairline grid, the peak of each
 * series labelled and nothing else, a legend with the totals, and a native
 * tooltip on every column. The same rows are one click away as a table,
 * which is also what a screen reader gets.
 */

const WIDTH = 760;
const HEIGHT = 240;
const PAD = { top: 22, right: 12, bottom: 30, left: 40 };
const PLOT_W = WIDTH - PAD.left - PAD.right;
const PLOT_H = HEIGHT - PAD.top - PAD.bottom;
/** Between the two bars of one column, in the surface colour. */
const BAR_GAP = 2;
const BAR_MAX = 24;
const CORNER = 4;

const SERIES = ["inbound", "outbound"] as const;
type Series = (typeof SERIES)[number];
const FILL: Record<Series, string> = { inbound: "fill-series-inbound", outbound: "fill-series-outbound" };
const SWATCH: Record<Series, string> = { inbound: "bg-series-inbound", outbound: "bg-series-outbound" };

export function ByHourChart({ rows, range }: { rows: ByHourRow[]; range: OverviewRange }) {
  const t = useTranslations("overview.chart");
  const [asTable, setAsTable] = useState(false);
  const titleId = useId();
  const totals = byHourTotals(rows);
  const columnLabel = range === "today" ? t("columns.hour") : t("columns.day");

  return (
    <Card className="flex flex-col gap-3 px-5.5 pb-4 pt-4.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div id={titleId} className="font-semibold">
            {t("title")}
          </div>
          <div className="text-small text-muted">{t("subtitle")}</div>
        </div>
        <div className="flex items-center gap-4">
          <ul className="flex items-center gap-4 text-small text-muted" aria-label={t("legend")}>
            {SERIES.map((series) => (
              <li key={series} className="inline-flex items-center gap-1.5">
                <span aria-hidden="true" className={`h-2.5 w-2.5 rounded-[3px] ${SWATCH[series]}`} />
                {t(series)}
                <span className="font-mono text-label text-ink">{formatCount(totals[series])}</span>
              </li>
            ))}
          </ul>
          <Button icon={asTable ? "chart" : "table"} onClick={() => setAsTable((value) => !value)}>
            {asTable ? t("showChart") : t("showTable")}
          </Button>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="py-8 text-center text-ui text-muted">{t("empty")}</p>
      ) : asTable ? (
        <RowsTable rows={rows} columnLabel={columnLabel} />
      ) : (
        <Bars rows={rows} titleId={titleId} />
      )}
    </Card>
  );
}

function Bars({ rows, titleId }: { rows: ByHourRow[]; titleId: string }) {
  const t = useTranslations("overview.chart");
  const scale = chartScale(rows);
  const peaks = labelledBars(rows);
  const every = labelEvery(rows.length);
  const slot = PLOT_W / rows.length;
  // Two bars share a column, each capped so the column keeps some air.
  const barWidth = Math.max(3, Math.min(BAR_MAX, (slot * 0.72 - BAR_GAP) / 2));
  const y = (value: number) => PAD.top + PLOT_H - (value / scale.max) * PLOT_H;
  const baseline = PAD.top + PLOT_H;

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      width="100%"
      role="img"
      aria-labelledby={titleId}
      className="block h-auto w-full font-sans"
    >
      {scale.ticks.map((tick) => (
        <g key={tick}>
          <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(tick)} y2={y(tick)} className="stroke-line" strokeWidth="1" />
          <text x={PAD.left - 8} y={y(tick) + 4} textAnchor="end" className="fill-faint text-[11px]">
            {formatCount(tick)}
          </text>
        </g>
      ))}
      <line x1={PAD.left} x2={WIDTH - PAD.right} y1={baseline} y2={baseline} className="stroke-line" strokeWidth="1" />

      {rows.map((row, index) => {
        const left = PAD.left + index * slot;
        const centre = left + slot / 2;
        const firstX = centre - BAR_GAP / 2 - barWidth;
        return (
          <g key={row.hour} className="group">
            <title>{t("tooltip", { label: row.hour, inbound: row.inbound, outbound: row.outbound })}</title>
            <rect x={left} y={PAD.top} width={slot} height={PLOT_H} className="fill-transparent group-hover:fill-inset" />
            {SERIES.map((series, position) => {
              const value = row[series];
              const x = firstX + position * (barWidth + BAR_GAP);
              return (
                <g key={series}>
                  {value > 0 ? (
                    <path d={bar(x, y(value), barWidth, baseline - y(value))} className={FILL[series]} />
                  ) : null}
                  {peaks[series] === index ? (
                    <text x={x + barWidth / 2} y={y(value) - 6} textAnchor="middle" className="fill-ink text-[11px] font-medium">
                      {formatCount(value)}
                    </text>
                  ) : null}
                </g>
              );
            })}
            {index % every === 0 ? (
              <text x={centre} y={HEIGHT - 10} textAnchor="middle" className="fill-muted text-[11px]">
                {row.hour}
              </text>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}

/** A bar with a 4px rounded top and a square foot on the baseline. */
function bar(x: number, top: number, width: number, height: number): string {
  const r = Math.min(CORNER, height, width / 2);
  const bottom = top + height;
  return [
    `M${x},${bottom}`,
    `L${x},${top + r}`,
    `Q${x},${top} ${x + r},${top}`,
    `L${x + width - r},${top}`,
    `Q${x + width},${top} ${x + width},${top + r}`,
    `L${x + width},${bottom}`,
    "Z",
  ].join(" ");
}

function RowsTable({ rows, columnLabel }: { rows: ByHourRow[]; columnLabel: string }) {
  const t = useTranslations("overview.chart");
  const totals = byHourTotals(rows);
  return (
    <Table>
      <thead>
        <tr>
          <Th>{columnLabel}</Th>
          <Th align="right">{t("inbound")}</Th>
          <Th align="right">{t("outbound")}</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.hour}>
            <Td>
              <span className="font-mono text-small">{row.hour}</span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatCount(row.inbound)}</span>
            </Td>
            <Td align="right">
              <span className="font-mono text-small">{formatCount(row.outbound)}</span>
            </Td>
          </tr>
        ))}
        <tr>
          <Td className="font-semibold">{t("total")}</Td>
          <Td align="right">
            <span className="font-mono text-small font-semibold">{formatCount(totals.inbound)}</span>
          </Td>
          <Td align="right">
            <span className="font-mono text-small font-semibold">{formatCount(totals.outbound)}</span>
          </Td>
        </tr>
      </tbody>
    </Table>
  );
}
