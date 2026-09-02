"use client";

import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { testDatabaseConnection, type ConnectionTestState } from "@/app/actions/data";
import { Button } from "@/components/ui/button";
import { Chip } from "@/components/ui/chip";
import { Field, inputClass } from "@/components/ui/field";
import { formatMs } from "@/lib/format";

/**
 * Test a connection string without changing anything. The field is a
 * password field, the value is never echoed back, and the result is the
 * API's: reachable or not, how fast, which server.
 */
export function ConnectionTestForm() {
  const t = useTranslations("data.connection.test");
  const [open, setOpen] = useState(false);
  const [state, action, pending] = useActionState<ConnectionTestState, FormData>(
    testDatabaseConnection,
    { status: "idle" },
  );

  if (!open) {
    return (
      <div className="flex justify-end">
        <Button icon="check" onClick={() => setOpen(true)}>
          {t("button")}
        </Button>
      </div>
    );
  }

  return (
    <form action={action} className="flex flex-col gap-3 border-t border-inset pt-3">
      <Field label={t("dsn")} hint={t("dsnHint")}>
        <input
          type="password"
          name="dsn"
          autoComplete="off"
          required
          placeholder="postgresql://user:password@host:5432/uaagro?sslmode=require"
          className={`${inputClass} font-mono`}
        />
      </Field>
      {state.status === "done" ? (
        <div role="status" className="flex flex-wrap items-center gap-2 text-ui">
          <Chip tone={state.result.ok ? "green" : "red"}>
            {state.result.ok ? t("reachable") : t("unreachable")}
          </Chip>
          {state.result.ok ? (
            <span className="font-mono text-small text-muted">
              {[formatMs(state.result.latencyMs), state.result.serverVersion].filter(Boolean).join(" · ")}
            </span>
          ) : (
            <span className="text-red-text">{state.result.error ?? t("unreachable")}</span>
          )}
        </div>
      ) : null}
      {state.status === "error" ? (
        <p role="alert" className="text-ui text-red-text">
          {state.message}
        </p>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={() => setOpen(false)}>
          {t("close")}
        </Button>
        <Button type="submit" variant="primary" icon="check" disabled={pending}>
          {pending ? t("testing") : t("run")}
        </Button>
      </div>
    </form>
  );
}
