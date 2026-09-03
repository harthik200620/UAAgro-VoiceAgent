import { describe, expect, it } from "vitest";

import { ROLES, can, capabilitiesFor, type Role, type Session } from "@/lib/rbac";

const session = (role: Role): Session => ({
  userId: "u1",
  role,
  centreIds: [],
  name: "Test",
  // Never used by `can()` -- these tests are about the grant table -- but the
  // type requires it, because a session without a token cannot call the API.
  accessToken: "test-token",
});

describe("roles and capabilities", () => {
  it("gives the ops manager everything but the connection and the client's sources", () => {
    const ops = capabilitiesFor("ops_manager");
    const admin = capabilitiesFor("super_admin");
    expect(admin.filter((capability) => !ops.includes(capability))).toEqual([
      "data.connection",
      "data.sources",
    ]);
  });

  it("keeps the database connection and the sources to the super_admin", () => {
    // A DSN and a MySQL password are the only secrets the panel ever handles.
    for (const capability of ["data.connection", "data.sources"] as const) {
      const holders = ROLES.filter((role) => capabilitiesFor(role).includes(capability));
      expect(holders, capability).toEqual(["super_admin"]);
    }
  });

  it("lets a centre manager run their centre and nothing else", () => {
    const manager = session("centre_manager");
    for (const capability of [
      "calls.view",
      "calls.listen",
      "inventory.edit",
      "campaigns.view",
      "centres.view",
      "knowledge.view",
      "knowledge.ask",
    ] as const) {
      expect(can(manager, capability), capability).toBe(true);
    }
    for (const capability of [
      "calls.intervene",
      "campaigns.create",
      "campaigns.dial",
      "campaigns.control",
      "flows.edit",
      "knowledge.upload",
      "centres.edit",
      "data.view",
    ] as const) {
      expect(can(manager, capability), capability).toBe(false);
    }
  });

  it("lets an agronomist try questions without uploading, and see no calls", () => {
    const agronomist = session("agronomist");
    expect(can(agronomist, "knowledge.view")).toBe(true);
    expect(can(agronomist, "knowledge.ask")).toBe(true);
    expect(can(agronomist, "knowledge.upload")).toBe(false);
    expect(can(agronomist, "calls.view")).toBe(false);
  });

  it("gives read_only and auditor nothing the API would refuse them", () => {
    // Every panel route needs at least an agronomist, so a grant here would
    // only ever produce a 403.
    expect(capabilitiesFor("read_only")).toEqual([]);
    expect(capabilitiesFor("auditor")).toEqual([]);
  });

  it("lets only an ops manager or above call a list right now", () => {
    const holders = ROLES.filter((role) => capabilitiesFor(role).includes("campaigns.dial"));
    expect(holders).toEqual(["ops_manager", "super_admin"]);
  });

  it("grants nothing without a session", () => {
    // A signed-out user must not fall through to a permissive default.
    expect(can(null, "calls.view")).toBe(false);
  });
});
