import { describe, expect, it } from "vitest";

import {
  ROLES,
  can,
  canApproveCampaign,
  canSeeCentre,
  capabilitiesFor,
  type Role,
  type Session,
} from "@/lib/rbac";

const session = (role: Role, overrides: Partial<Session> = {}): Session => ({
  userId: "u1",
  role,
  centreIds: [],
  name: "Test",
  locale: "hi",
  // Never used by `can()` -- these tests are about the grant table -- but the
  // type requires it, because a session without a token cannot call the API.
  accessToken: "test-token",
  ...overrides,
});

describe("roles and capabilities (§10, §15.1)", () => {
  it("gives only the agronomist advisory approval", () => {
    // §9's safety control. Ops and super_admin can run the business; they do
    // not sign off a pesticide dose, and a rank ladder would have given it to
    // them automatically.
    const approvers = ROLES.filter((role) =>
      capabilitiesFor(role).includes("advisory.approve"),
    );
    expect(approvers).toEqual(["agronomist"]);
  });

  it("does not let an agronomist edit prices", () => {
    // The ladder breaks here, which is why grants are explicit. An agronomist
    // outranks a centre manager on advisory and has no business in inventory.
    expect(can(session("agronomist"), "inventory.edit")).toBe(false);
    expect(can(session("centre_manager"), "inventory.edit")).toBe(true);
  });

  it("lets an auditor read the audit log and change nothing", () => {
    const auditor = session("auditor");
    expect(can(auditor, "audit.view")).toBe(true);
    for (const capability of [
      "catalogue.edit",
      "inventory.edit",
      "advisory.edit",
      "flows.publish",
      "campaigns.create",
      "settings.edit",
    ] as const) {
      expect(can(auditor, capability)).toBe(false);
    }
  });

  it("restricts phone decryption to ops and above", () => {
    // §17: a privileged, audited operation.
    const holders = ROLES.filter((role) =>
      capabilitiesFor(role).includes("farmers.decrypt_phone"),
    );
    expect(holders).toEqual(["ops_manager", "super_admin"]);
  });

  it("grants nothing without a session", () => {
    // A signed-out user must not fall through to a permissive default.
    expect(can(null, "calls.view")).toBe(false);
    expect(canSeeCentre(null, "any")).toBe(false);
  });

  it("refuses centre rows outside a manager's scope", () => {
    const manager = session("centre_manager", { centreIds: ["c1"] });
    expect(canSeeCentre(manager, "c1")).toBe(true);
    expect(canSeeCentre(manager, "c2")).toBe(false);
  });

  it("treats an empty centre list as no access for a centre manager", () => {
    // Empty means org-wide only for org-wide roles, matching the RLS policy.
    // For a centre manager it must mean nothing, which is the safe direction.
    expect(canSeeCentre(session("centre_manager"), "c1")).toBe(false);
    expect(canSeeCentre(session("ops_manager"), "c1")).toBe(true);
  });

  it("stops a creator approving their own campaign", () => {
    // §13.1's four-eyes rule. Enforced on the server; this keeps a reviewer
    // from clicking a control that would silently fail.
    const ops = session("ops_manager", { userId: "creator" });
    expect(canApproveCampaign(ops, "creator")).toBe(false);
    expect(canApproveCampaign(ops, "someone-else")).toBe(true);
  });

  it("does not let a non-approver approve someone else's campaign", () => {
    expect(canApproveCampaign(session("centre_manager"), "other")).toBe(false);
  });

  it("keeps read_only genuinely read-only", () => {
    const readOnly = session("read_only");
    for (const capability of capabilitiesFor("read_only")) {
      expect(capability.endsWith(".view")).toBe(true);
    }
    expect(can(readOnly, "calls.intervene")).toBe(false);
  });
});
