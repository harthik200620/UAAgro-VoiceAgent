"use server";

import { getTranslations } from "next-intl/server";

import type { ConnectionTest } from "@/lib/contract";
import { failure, testConnection } from "@/server/api";
import { guard } from "@/server/guard";

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
  const guarded = await guard("data.connection");
  if (!guarded.ok) return { status: "error", message: guarded.message };
  const { session } = guarded;
  const dsn = String(formData.get("dsn") ?? "").trim();
  if (!dsn) return { status: "error", message: t("dsnRequired") };
  try {
    return { status: "done", result: await testConnection(session, dsn) };
  } catch (error) {
    return { status: "error", message: failure(error, t("testFailed")).message };
  }
}
