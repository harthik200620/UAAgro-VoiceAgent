import { describe, expect, it } from "vitest";

import { navItemsFor } from "@/lib/nav";
import type { Role, Session } from "@/lib/rbac";

const session = (role: Role): Session => ({
  userId: "u1",
  role,
  centreIds: [],
  name: "Test",
  accessToken: "test-token",
});

const keys = (role: Role) => navItemsFor(session(role)).map((item) => item.key);

describe("the sidebar", () => {
  it("shows the ops manager and the super_admin all eight sections in order", () => {
    const all = ["overview", "live", "calls", "outbound", "knowledge", "centres", "flows", "data"];
    expect(keys("ops_manager")).toEqual(all);
    expect(keys("super_admin")).toEqual(all);
  });

  it("hides Flows and Data from a centre manager", () => {
    expect(keys("centre_manager")).toEqual(["overview", "live", "calls", "outbound", "knowledge", "centres"]);
  });

  it("gives an agronomist the knowledge base alone", () => {
    expect(keys("agronomist")).toEqual(["knowledge"]);
  });

  it("shows nothing to roles every route refuses", () => {
    // read_only and auditor are below every "needs" line in the contract; a
    // link that opens on a 403 is worse than no link.
    expect(keys("read_only")).toEqual([]);
    expect(keys("auditor")).toEqual([]);
    expect(navItemsFor(null)).toEqual([]);
  });

  it("points Overview at the root and every other section at its own path", () => {
    const hrefs = Object.fromEntries(navItemsFor(session("super_admin")).map((item) => [item.key, item.href]));
    expect(hrefs).toEqual({
      overview: "/",
      live: "/live",
      calls: "/calls",
      outbound: "/outbound",
      knowledge: "/knowledge",
      centres: "/centres",
      flows: "/flows",
      data: "/data",
    });
  });
});
