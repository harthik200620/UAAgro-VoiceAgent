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
  it("gives the ops manager everything but the connection and the user list", () => {
    const ops = capabilitiesFor("ops_manager");
    const admin = capabilitiesFor("super_admin");
    expect(admin.filter((capability) => !ops.includes(capability))).toEqual([
      "data.connection",
      "users.manage",
    ]);
  });

  it("keeps the database connection to the super_admin", () => {
    // A DSN is the one secret the panel ever handles.
    const holders = ROLES.filter((role) => capabilitiesFor(role).includes("data.connection"));
    expect(holders).toEqual(["super_admin"]);
  });

  it("lets a centre manager run their centre and nothing else", () => {
    const manager = session("centre_manager");
    for (const capability of ["calls.view", "calls.listen", "inventory.edit", "campaigns.view"] as const) {
      expect(can(manager, capability), capability).toBe(true);
    }
    for (const capability of [
      "calls.intervene",
      "campaigns.create",
      "campaigns.control",
      "flows.edit",
      "knowledge.upload",
      "centres.edit",
      "data.view",
    ] as const) {
      expect(can(manager, capability), capability).toBe(false);
    }
  });

  it("lets an agronomist try questions without uploading", () => {
    const agronomist = session("agronomist");
    expect(can(agronomist, "knowledge.ask")).toBe(true);
    expect(can(agronomist, "knowledge.upload")).toBe(false);
  });

  it("keeps read_only genuinely read-only", () => {
    for (const capability of capabilitiesFor("read_only")) {
      expect(capability.endsWith(".view")).toBe(true);
    }
    expect(can(session("read_only"), "calls.listen")).toBe(false);
  });

  it("grants nothing without a session", () => {
    // A signed-out user must not fall through to a permissive default.
    expect(can(null, "calls.view")).toBe(false);
  });

  it("grants every role at least the live view", () => {
    for (const role of ROLES) {
      expect(can(session(role), "calls.view"), role).toBe(true);
    }
  });
});
