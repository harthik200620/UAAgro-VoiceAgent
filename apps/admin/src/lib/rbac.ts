/**
 * Roles and permissions in the UI (§10, §15.1, §17).
 *
 * This is the *second* line, and it is worth being explicit that it is not the
 * first. Authorisation is enforced in Postgres by RLS and in the API by
 * permission dependencies; what lives here decides which controls a user is
 * shown. A panel that renders a button the API will refuse teaches its users
 * that the system is unreliable, and one that hides a button the API would
 * allow makes them ask someone else to do their job.
 *
 * Neither of those is a security boundary. Hiding a control does not protect
 * anything -- the request can still be made by hand -- so nothing here is
 * relied on for safety, and every capability below has a matching check on the
 * server.
 */

export const ROLES = [
  "read_only",
  "auditor",
  "agronomist",
  "centre_manager",
  "ops_manager",
  "super_admin",
] as const;

export type Role = (typeof ROLES)[number];

/**
 * Ordering for "at least this role" questions.
 *
 * Deliberately not used for anything except comparisons that are genuinely
 * hierarchical. An agronomist outranks a centre manager on advisory approval
 * and does not outrank them on inventory, so most checks below are capability
 * grants rather than rank comparisons -- a single ladder would give the
 * agronomist the ability to edit prices, which nobody intended.
 */
const RANK: Record<Role, number> = {
  read_only: 0,
  auditor: 1,
  agronomist: 2,
  centre_manager: 3,
  ops_manager: 4,
  super_admin: 5,
};

export type Capability =
  | "calls.view"
  | "calls.listen"
  | "calls.intervene"
  | "farmers.view"
  | "farmers.edit"
  | "farmers.decrypt_phone"
  | "catalogue.view"
  | "catalogue.edit"
  | "inventory.edit"
  | "advisory.view"
  | "advisory.edit"
  | "advisory.approve"
  | "knowledge.upload"
  | "knowledge.publish"
  | "flows.edit"
  | "flows.publish"
  | "campaigns.create"
  | "campaigns.approve"
  | "campaigns.control"
  | "users.manage"
  | "audit.view"
  | "settings.edit";

/**
 * Who may do what.
 *
 * Written as explicit grants per role rather than inherited from the rank
 * above, because the interesting cases are the ones that break the ladder:
 *
 * - An **auditor** reads the audit log and cannot change anything, including
 *   things a read_only user also cannot change. Rank would give them less than
 *   they need or more.
 * - An **agronomist** approves crop recommendations -- the §9 safety control --
 *   and has no business editing inventory prices.
 * - **`campaigns.approve`** is deliberately absent from `campaigns.create`'s
 *   holders where possible: §13.1 requires four-eyes, and the *creator* cannot
 *   approve their own campaign regardless of role. That check is on the server,
 *   because it depends on who created the row and this table cannot see that.
 * - **`farmers.decrypt_phone`** is a privileged audited operation (§17), so it
 *   sits with ops_manager and above and nowhere else.
 */
const GRANTS: Record<Role, readonly Capability[]> = {
  read_only: ["calls.view", "farmers.view", "catalogue.view", "advisory.view"],
  auditor: [
    "calls.view",
    "farmers.view",
    "catalogue.view",
    "advisory.view",
    "audit.view",
  ],
  agronomist: [
    "calls.view",
    "farmers.view",
    "catalogue.view",
    "advisory.view",
    "advisory.edit",
    // The §9 safety control. Nobody else grants this.
    "advisory.approve",
    "knowledge.upload",
    "knowledge.publish",
  ],
  centre_manager: [
    "calls.view",
    "calls.listen",
    "calls.intervene",
    "farmers.view",
    "farmers.edit",
    "catalogue.view",
    // The daily-use screen for a centre (§15.1 Catalogue).
    "inventory.edit",
    "advisory.view",
  ],
  ops_manager: [
    "calls.view",
    "calls.listen",
    "calls.intervene",
    "farmers.view",
    "farmers.edit",
    "farmers.decrypt_phone",
    "catalogue.view",
    "catalogue.edit",
    "inventory.edit",
    "advisory.view",
    "knowledge.upload",
    "flows.edit",
    "flows.publish",
    "campaigns.create",
    "campaigns.approve",
    "campaigns.control",
    "audit.view",
  ],
  super_admin: [
    "calls.view",
    "calls.listen",
    "calls.intervene",
    "farmers.view",
    "farmers.edit",
    "farmers.decrypt_phone",
    "catalogue.view",
    "catalogue.edit",
    "inventory.edit",
    "advisory.view",
    "advisory.edit",
    "knowledge.upload",
    "knowledge.publish",
    "flows.edit",
    "flows.publish",
    "campaigns.create",
    "campaigns.approve",
    "campaigns.control",
    "users.manage",
    "audit.view",
    "settings.edit",
  ],
};

export type Session = {
  userId: string;
  role: Role;
  /** Centre ids this user may see. Empty means org-wide (§10 RLS). */
  centreIds: readonly string[];
  name: string;
  locale: "hi" | "en";
  /**
   * The API access token, held server-side only.
   *
   * The control plane authenticates with `Authorization: Bearer` and reads no
   * cookie, so this is what every call from the panel carries. It never
   * reaches the browser: the cookie holding it is httpOnly, and this field is
   * only ever read inside a Server Component or Server Action.
   */
  accessToken: string;
};

export function can(session: Session | null, capability: Capability): boolean {
  if (!session) return false;
  return GRANTS[session.role].includes(capability);
}

export function atLeast(session: Session | null, role: Role): boolean {
  if (!session) return false;
  return RANK[session.role] >= RANK[role];
}

/**
 * Whether this session may see rows for a given centre.
 *
 * Mirrors the RLS policy so the UI does not offer a filter that returns
 * nothing. An empty `centreIds` means org-wide, matching the GUC convention on
 * the database side -- and the *reason* it means org-wide there is that
 * org-wide roles are listed explicitly in the policy, not that an empty list is
 * permissive. An empty list for a centre_manager returns no rows, which is the
 * safe direction.
 */
export function canSeeCentre(session: Session | null, centreId: string): boolean {
  if (!session) return false;
  if (session.centreIds.length === 0) return atLeast(session, "ops_manager");
  return session.centreIds.includes(centreId);
}

/**
 * §13.1's four-eyes rule, as far as the UI can express it.
 *
 * The server decides -- it knows who created the row and this cannot be
 * spoofed there. The panel uses this only to disable the button and say why,
 * so a reviewer is not left clicking an Approve control that silently fails.
 */
export function canApproveCampaign(
  session: Session | null,
  createdByUserId: string,
): boolean {
  if (!can(session, "campaigns.approve")) return false;
  return session!.userId !== createdByUserId;
}

export const capabilitiesFor = (role: Role): readonly Capability[] => GRANTS[role];
