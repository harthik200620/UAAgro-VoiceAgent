"use server";

import { getTranslations } from "next-intl/server";

import type { ConnectionTest } from "@/lib/contract";
import { can } from "@/lib/rbac";
import { failure, testConnection } from "@/server/api";
import { currentSession } from "@/server/session";

/**
 * "Test" on the Data screen. The DSN goes from the form to the API and
 * nowhere else: it is not in the returned state, not logged, not stored.
 * The running connection is changed through the deployment's environment
 * and a restart, never from here.
 */
export type ConnectionTestState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "done"; result: ConnectionTest };

export async function testDatabaseConnection(
  _previous: ConnectionTestState,
  formData: FormData,
): Promise<ConnectionTestState> {
  const t = await getTranslations("actions");
  const session = await currentSession();
  if (!session || !can(session, "data.connection")) {
    return { status: "error", message: t("forbidden") };
  }
  const dsn = String(formData.get("dsn") ?? "").trim();
  if (!dsn) return { status: "error", message: t("dsnRequired") };
  try {
    return { status: "done", result: await testConnection(session, dsn) };
  } catch (error) {
    return { status: "error", message: failure(error, t("testFailed")).message };
  }
}
