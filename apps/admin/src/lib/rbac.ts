/**
 * Roles and capabilities in the UI.
 *
 * This is the *second* line, and it is worth being explicit that it is not the
 * first. Authorisation is enforced in Postgres by RLS and in the API by
 * permission dependencies; what lives here decides which controls a user is
 * shown. A panel that renders a button the API will refuse teaches its users
 * that the system is unreliable, and one that hides a button the API would
 * allow makes them ask someone else to do their job.
 *
 * Neither of those is a security boundary. Hiding a control protects nothing
 * -- the request can still be made by hand -- so nothing here is relied on
 * for safety, and every capability below has a matching check on the server.
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

export type Capability =
  | "calls.view"
  | "calls.listen"
  | "calls.intervene"
  | "campaigns.view"
  | "campaigns.create"
  | "campaigns.approve"
  | "campaigns.control"
  | "flows.edit"
  | "flows.publish"
  | "knowledge.view"
  | "knowledge.ask"
  | "knowledge.upload"
  | "centres.view"
  | "centres.edit"
  | "inventory.edit"
  | "data.view"
  | "data.connection"
  | "users.manage";

const EVERYTHING: readonly Capability[] = [
  "calls.view",
  "calls.listen",
  "calls.intervene",
  "campaigns.view",
  "campaigns.create",
  "campaigns.approve",
  "campaigns.control",
  "flows.edit",
  "flows.publish",
  "knowledge.view",
  "knowledge.ask",
  "knowledge.upload",
  "centres.view",
  "centres.edit",
  "inventory.edit",
  "data.view",
  "data.connection",
  "users.manage",
];

/** What a signed-in user may look at without being able to change anything. */
const VIEW_ONLY: readonly Capability[] = ["calls.view", "campaigns.view", "centres.view"];

/**
 * Who may do what.
 *
 * Explicit grants per role rather than a rank ladder, because the interesting
 * cases break the ladder: a centre manager edits their own stock and nobody's
 * script; an agronomist may try questions against the knowledge base but not
 * upload to it; the database connection is the super_admin's alone, since a
 * DSN is the one secret the panel ever handles.
 */
const GRANTS: Record<Role, readonly Capability[]> = {
  read_only: VIEW_ONLY,
  auditor: VIEW_ONLY,
  agronomist: [...VIEW_ONLY, "knowledge.view", "knowledge.ask"],
  centre_manager: [
    ...VIEW_ONLY,
    "calls.listen",
    "inventory.edit",
    "knowledge.view",
    "knowledge.ask",
  ],
  ops_manager: EVERYTHING.filter(
    (capability) => capability !== "data.connection" && capability !== "users.manage",
  ),
  super_admin: EVERYTHING,
};

export type Session = {
  userId: string;
  role: Role;
  /** Centre ids this user may see. Empty means org-wide (RLS). */
  centreIds: readonly string[];
  name: string;
  /**
   * The API access token, held server-side only.
   *
   * The control plane authenticates with `Authorization: Bearer` and reads no
   * cookie, so this is what every call from the panel carries. It never
   * reaches the browser: the cookie holding it is httpOnly, and this field is
   * only ever read inside a Server Component, a Server Action or a Route
   * Handler.
   */
  accessToken: string;
};

export function can(session: Session | null, capability: Capability): boolean {
  if (!session) return false;
  return GRANTS[session.role].includes(capability);
}

export const capabilitiesFor = (role: Role): readonly Capability[] => GRANTS[role];
