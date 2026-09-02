import { describe, expect, it } from "vitest";

import { inboundTabsFor, navItemsFor } from "@/lib/nav";
import type { Role, Session } from "@/lib/rbac";

const session = (role: Role): Session => ({
  userId: "u1",
  role,
  centreIds: [],
  name: "Test",
  accessToken: "test-token",
});

const keys = (role: Role) => navItemsFor(session(role)).map((item) => item.key);
const tabs = (role: Role) => inboundTabsFor(session(role)).map((tab) => tab.key);

describe("the sidebar", () => {
  it("shows the ops manager and the super_admin all six sections in order", () => {
    const all = ["live", "calls", "outbound", "inbound", "flows", "data"];
    expect(keys("ops_manager")).toEqual(all);
    expect(keys("super_admin")).toEqual(all);
  });

  it("hides Flows and Data from a centre manager", () => {
    expect(keys("centre_manager")).toEqual(["live", "calls", "outbound", "inbound"]);
  });

  it("gives a read-only user the four viewing sections", () => {
    expect(keys("read_only")).toEqual(["live", "calls", "outbound", "inbound"]);
    expect(keys("auditor")).toEqual(keys("read_only"));
  });

  it("shows nothing without a session", () => {
    expect(navItemsFor(null)).toEqual([]);
    expect(inboundTabsFor(null)).toEqual([]);
  });

  it("keeps Greeting to those who may edit scripts", () => {
    expect(tabs("ops_manager")).toEqual(["knowledge", "centres", "greeting"]);
    expect(tabs("centre_manager")).toEqual(["knowledge", "centres"]);
    expect(tabs("read_only")).toEqual(["centres"]);
  });

  it("points Live at the root and every other section at its own path", () => {
    const hrefs = Object.fromEntries(navItemsFor(session("super_admin")).map((item) => [item.key, item.href]));
    expect(hrefs).toEqual({
      live: "/",
      calls: "/calls",
      outbound: "/outbound",
      inbound: "/inbound",
      flows: "/flows",
      data: "/data",
    });
  });
});
