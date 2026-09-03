import { describe, expect, it } from "vitest";

import type { AttentionItem, ByHourRow } from "@/lib/contract";
import { movedRoute, resolvePanelHref } from "@/lib/nav";
import {
  byHourTotals,
  chartScale,
  funnelSteps,
  labelEvery,
  labelledBars,
  niceCeiling,
  percent,
  sortAttention,
} from "@/lib/overview";

const item = (overrides: Partial<AttentionItem>): AttentionItem => ({
  kind: "ticket",
  severity: "low",
  title: "",
  detail: null,
  href: null,
  at: null,
  count: 1,
  ...overrides,
});

describe("the attention list", () => {
  it("reads the most urgent first and keeps the API's order within a severity", () => {
    const sorted = sortAttention([
      item({ kind: "knowledge_pending", severity: "low" }),
      item({ kind: "stock_out", severity: "medium", detail: "urea" }),
      item({ kind: "ticket", severity: "high" }),
      item({ kind: "stock_out", severity: "medium", detail: "dap" }),
      item({ kind: "worker", severity: "high" }),
    ]);
    expect(sorted.map((entry) => `${entry.severity}:${entry.kind}:${entry.detail ?? ""}`)).toEqual([
      "high:ticket:",
      "high:worker:",
      "medium:stock_out:urea",
      "medium:stock_out:dap",
      "low:knowledge_pending:",
    ]);
  });

  it("does not change the list it was given", () => {
    const items = [item({ severity: "low" }), item({ severity: "high" })];
    sortAttention(items);
    expect(items[0]?.severity).toBe("low");
  });
});

describe("attention links", () => {
  it("moves the old inbound routes to the new sections", () => {
    expect(resolvePanelHref("/inbound/knowledge")).toBe("/knowledge");
    expect(resolvePanelHref("/inbound/centres")).toBe("/centres");
    expect(resolvePanelHref("/inbound/greeting")).toBe("/flows?type=inbound");
    expect(resolvePanelHref("/outbound/knowledge")).toBe("/knowledge");
    expect(movedRoute("/inbound/")).toBe("/knowledge");
  });

  it("keeps a current route and its query", () => {
    expect(resolvePanelHref("/calls/abc-123")).toBe("/calls/abc-123");
    expect(resolvePanelHref("/calls?outcome=failed")).toBe("/calls?outcome=failed");
  });

  it("refuses anything that is not a path inside the panel", () => {
    expect(resolvePanelHref(null)).toBeNull();
    expect(resolvePanelHref("https://evil.example.com/calls")).toBeNull();
    expect(resolvePanelHref("//evil.example.com")).toBeNull();
    expect(resolvePanelHref("calls/abc")).toBeNull();
  });
});

const rows: ByHourRow[] = [
  { hour: "09:00", inbound: 3, outbound: 0 },
  { hour: "10:00", inbound: 12, outbound: 4 },
  { hour: "11:00", inbound: 7, outbound: 9 },
];

describe("the by-hour chart", () => {
  it("scales to a clean ceiling with four gridlines", () => {
    expect(chartScale(rows)).toEqual({ max: 20, ticks: [5, 10, 15, 20] });
    expect(chartScale([])).toEqual({ max: 1, ticks: [0.25, 0.5, 0.75, 1] });
  });

  it("rounds ceilings the way an axis reads", () => {
    expect(niceCeiling(7)).toBe(10);
    expect(niceCeiling(23)).toBe(25);
    expect(niceCeiling(140)).toBe(200);
    expect(niceCeiling(1000)).toBe(1000);
    expect(niceCeiling(0)).toBe(1);
  });

  it("labels the peak of each series and nothing else", () => {
    expect(labelledBars(rows)).toEqual({ inbound: 1, outbound: 2 });
    expect(labelledBars([{ hour: "09:00", inbound: 0, outbound: 0 }])).toEqual({ inbound: null, outbound: null });
  });

  it("thins the x-axis labels as the columns multiply", () => {
    expect(labelEvery(7)).toBe(1);
    expect(labelEvery(24)).toBe(3);
    expect(labelEvery(30)).toBe(5);
  });

  it("gives the table view the same totals as the legend", () => {
    expect(byHourTotals(rows)).toEqual({ inbound: 22, outbound: 13 });
  });
});

describe("the outbound funnel", () => {
  it("narrows each stage against the numbers dialled", () => {
    const steps = funnelSteps({
      campaignsRunning: 1,
      contactsDialled: 200,
      reached: 120,
      pressed1: 30,
      pressed2: 18,
      optedOut: 4,
    });
    expect(steps.map((step) => step.key)).toEqual(["dialled", "reached", "pressed1", "pressed2", "optedOut"]);
    expect(steps.map((step) => step.share)).toEqual([1, 0.6, 0.15, 0.09, 0.02]);
  });

  it("is all zero rather than a division by zero before the first call", () => {
    const steps = funnelSteps({
      campaignsRunning: 0,
      contactsDialled: 0,
      reached: 0,
      pressed1: 0,
      pressed2: 0,
      optedOut: 0,
    });
    expect(steps.every((step) => step.share === 0)).toBe(true);
  });
});

describe("the stat cards", () => {
  it("shows a share only when there is something to divide by", () => {
    expect(percent(3, 12)).toBe(25);
    expect(percent(0, 0)).toBeNull();
  });
});
