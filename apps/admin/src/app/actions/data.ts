"use server";

import { randomUUID } from "node:crypto";

import { getTranslations } from "next-intl/server";
import { revalidatePath } from "next/cache";

import type {
  ConnectionReport,
  ConnectionTest,
  DataSource,
  DataSourceInput,
  DataSourcePatch,
  SourceConnectionInput,
  SourceMapping,
  SourceSchedule,
  SyncRun,
  TableColumns,
} from "@/lib/contract";
import { SOURCE_TABLES, validateMapping } from "@/lib/data-sources";
import {
  createDataSource,
  deleteDataSource,
  failure,
  getSourceRuns,
  getSourceTableColumns,
  syncDataSource,
  testConnection,
  testDataSource,
  testSourceConnection,
  updateDataSource,
  type ActionResult,
} from "@/server/api";
import { guard } from "@/server/guard";

/**
 * The Data screen's two kinds of secret, handled the same way: a connection
 * string or a MySQL password goes from the form to the API and nowhere else.
 * It is not in any returned state, not logged, not stored by the panel. The
 * API keeps a source's password encrypted and only ever says whether one is
 * set.
 */

const DATA_PAGE = "/[locale]/(panel)/data";

export type ConnectionTestState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "done"; result: ConnectionTest };

/** "Test" for the platform's own database. The running connection changes only through the deployment. */
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

/* The client's MySQL database */

export type SourceFormState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "saved"; source: DataSource };

type SourceFields = {
  id: string;
  name: string;
  host: string;
  port: number | null;
  database: string;
  user: string;
  password: string;
  tls: boolean;
  schedule: SourceSchedule;
};

function readFields(formData: FormData): SourceFields {
  const text = (key: string) => String(formData.get(key) ?? "").trim();
  const port = Number(text("port"));
  const schedule = text("schedule");
  return {
    id: text("id"),
    name: text("name"),
    host: text("host"),
    port: Number.isInteger(port) && port > 0 && port < 65536 ? port : null,
    database: text("database"),
    user: text("user"),
    // Not trimmed: a password is whatever was typed.
    password: String(formData.get("password") ?? ""),
    tls: formData.get("tls") === "on",
    schedule: schedule === "hourly" || schedule === "daily" ? schedule : "manual",
  };
}

/** Add a source, or change one. On a change the password is replaced only when a new one was typed. */
export async function saveSource(_previous: SourceFormState, formData: FormData): Promise<SourceFormState> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.sources");
  if (!guarded.ok) return { status: "error", message: guarded.message };
  const { session } = guarded;

  const fields = readFields(formData);
  if (!fields.name || !fields.host || !fields.database || !fields.user) {
    return { status: "error", message: t("sourceIncomplete") };
  }
  if (!fields.id && !fields.password) return { status: "error", message: t("sourcePasswordRequired") };

  try {
    let source: DataSource;
    if (fields.id) {
      const patch: DataSourcePatch = {
        name: fields.name,
        host: fields.host,
        database: fields.database,
        user: fields.user,
        tls: fields.tls,
        schedule: fields.schedule,
        ...(fields.port === null ? {} : { port: fields.port }),
        ...(fields.password ? { password: fields.password } : {}),
      };
      source = await updateDataSource(session, fields.id, patch);
    } else {
      const input: DataSourceInput = {
        name: fields.name,
        host: fields.host,
        database: fields.database,
        user: fields.user,
        password: fields.password,
        tls: fields.tls,
        schedule: fields.schedule,
        ...(fields.port === null ? {} : { port: fields.port }),
      };
      source = await createDataSource(session, input, randomUUID());
    }
    revalidatePath(DATA_PAGE, "page");
    return { status: "saved", source };
  } catch (error) {
    return { status: "error", message: failure(error, t("changeFailed")).message };
  }
}

export type SourceTestState =
  | { status: "idle" }
  | { status: "error"; message: string }
  | { status: "done"; report: ConnectionReport };

/**
 * "Test connection" from the form. With a new password typed the test uses
 * exactly what the form holds and stores nothing; on a saved source with the
 * password field left blank, the API tests with the password it keeps.
 */
export async function testSourceForm(_previous: SourceTestState, formData: FormData): Promise<SourceTestState> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.sources");
  if (!guarded.ok) return { status: "error", message: guarded.message };
  const { session } = guarded;

  const fields = readFields(formData);
  try {
    if (fields.id && !fields.password) {
      return { status: "done", report: await testDataSource(session, fields.id) };
    }
    if (!fields.host || !fields.database || !fields.user || !fields.password) {
      return { status: "error", message: t("sourceIncomplete") };
    }
    const input: SourceConnectionInput = {
      host: fields.host,
      database: fields.database,
      user: fields.user,
      password: fields.password,
      tls: fields.tls,
      ...(fields.port === null ? {} : { port: fields.port }),
    };
    return { status: "done", report: await testSourceConnection(session, input) };
  } catch (error) {
    return { status: "error", message: failure(error, t("testFailed")).message };
  }
}

export async function testSavedSource(sourceId: string): Promise<ActionResult<ConnectionReport>> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.sources");
  if (!guarded.ok) return guarded;
  try {
    return { ok: true, value: await testDataSource(guarded.session, sourceId) };
  } catch (error) {
    return failure(error, t("testFailed"));
  }
}

export async function removeSource(sourceId: string): Promise<ActionResult<null>> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.sources");
  if (!guarded.ok) return guarded;
  try {
    await deleteDataSource(guarded.session, sourceId);
    revalidatePath(DATA_PAGE, "page");
    return { ok: true, value: null };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

/** One of their tables: columns and the first rows, for the mapping screen. */
export async function loadSourceColumns(sourceId: string, table: string): Promise<ActionResult<TableColumns>> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.sources");
  if (!guarded.ok) return guarded;
  const name = table.trim();
  if (!name) return { ok: false, message: t("tableRequired"), code: "validation_error" };
  try {
    return { ok: true, value: await getSourceTableColumns(guarded.session, sourceId, name) };
  } catch (error) {
    return failure(error, t("loadFailed"));
  }
}

/** The mapping is refused here before the API sees it when a required field is still unmapped. */
export async function saveSourceMapping(
  sourceId: string,
  mapping: SourceMapping,
): Promise<ActionResult<DataSource>> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.sources");
  if (!guarded.ok) return guarded;
  const problems = validateMapping(mapping);
  if (SOURCE_TABLES.some((table) => problems[table].length > 0)) {
    return { ok: false, message: t("mappingIncomplete"), code: "validation_error" };
  }
  try {
    const source = await updateDataSource(guarded.session, sourceId, { mapping });
    revalidatePath(DATA_PAGE, "page");
    return { ok: true, value: source };
  } catch (error) {
    return failure(error, t("changeFailed"));
  }
}

/** 202 from the API: the run continues in the background and the runs list is polled. */
export async function syncSourceNow(sourceId: string): Promise<ActionResult<SyncRun>> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.view");
  if (!guarded.ok) return guarded;
  try {
    const run = await syncDataSource(guarded.session, sourceId, randomUUID());
    revalidatePath(DATA_PAGE, "page");
    return { ok: true, value: run };
  } catch (error) {
    return failure(error, t("syncFailed"));
  }
}

export async function loadSourceRuns(sourceId: string): Promise<ActionResult<SyncRun[]>> {
  const t = await getTranslations("actions");
  const guarded = await guard("data.view");
  if (!guarded.ok) return guarded;
  try {
    return { ok: true, value: await getSourceRuns(guarded.session, sourceId) };
  } catch (error) {
    return failure(error, t("loadFailed"));
  }
}
