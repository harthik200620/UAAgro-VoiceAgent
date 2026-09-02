"use client";

import { clsx } from "clsx";
import { useTranslations } from "next-intl";
import { useActionState, useState } from "react";

import { authenticate, type LoginState } from "@/app/actions/auth";
import { Field } from "@/components/ui/field";
import { Icon } from "@/components/ui/icon";

/**
 * The panel's front door.
 *
 * Two steps, because TOTP is mandatory: credentials, then a code. On a first
 * sign-in the API returns an enrolment secret instead of a challenge, and
 * this shows it so the operator can add it to an authenticator.
 *
 * One `useActionState` for both steps. The server action dispatches on the
 * step it is handed, so the state machine has a single source of truth -- a
 * hook per step means the second one is initialised at first render and still
 * says "credentials" when the code is submitted.
 *
 * The enrolment secret is shown as text rather than as a QR image. Drawing a
 * QR would mean a client-side library or a request to a QR service, and
 * sending an MFA secret to a third party to be rendered is exactly what MFA
 * exists to prevent. Every authenticator accepts a typed key.
 */
export function LoginForm() {
  const t = useTranslations("login");
  const [state, action, pending] = useActionState<LoginState, FormData>(authenticate, {
    step: "credentials",
  });

  if (state.step === "credentials") {
    return (
      <form action={action} className="flex w-[420px] max-w-full flex-col gap-5.5">
        <div>
          <h1 className="font-serif text-title-lg font-normal">{t("title")}</h1>
          <p className="mt-2 text-base text-muted">{t("intro")}</p>
        </div>

        <Field label={t("email")}>
          <input type="email" name="email" autoComplete="username" required className={loginInput} />
        </Field>
        <Field label={t("password")}>
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            className={loginInput}
          />
        </Field>

        <button type="submit" disabled={pending} className={submitClass}>
          {t("continue")}
        </button>
        {state.error ? <Problem>{state.error}</Problem> : null}

        <p className="flex items-center gap-2 text-small text-muted">
          <Icon name="shield" size={14} />
          {t("policy")}
        </p>

        <div className="flex flex-col gap-3 border-t border-line pt-5.5">
          <div className="flex items-baseline justify-between">
            <span className="font-semibold">{t("thenCode")}</span>
            <span className="text-label text-faint">{t("stepTwo")}</span>
          </div>
          <CodeBoxes digits="" active={false} />
          <p className="text-small text-muted">{t("codeHint")}</p>
        </div>
      </form>
    );
  }

  const enrolling = state.step === "enrol";

  return (
    <form action={action} className="flex w-[420px] max-w-full flex-col gap-5.5">
      <div>
        <h1 className="font-serif text-title-lg font-normal">
          {enrolling ? t("enrolTitle") : t("mfaTitle")}
        </h1>
        <p className="mt-2 text-base text-muted">{enrolling ? t("enrolHint") : t("mfaHint")}</p>
      </div>

      {enrolling ? (
        <div className="flex flex-col gap-[5px]">
          <span className="text-label text-muted">{t("secretLabel")}</span>
          <code className="block break-all rounded-panel border border-line bg-inset px-3.5 py-3 font-mono text-ui">
            {state.secret}
          </code>
        </div>
      ) : null}

      <CodeInput label={t("code")} />

      <button type="submit" disabled={pending} className={submitClass}>
        {t("verify")}
      </button>
      {state.error ? <Problem>{state.error}</Problem> : null}
      <p className="text-small text-muted">{t("codeHint")}</p>
    </form>
  );
}

const loginInput =
  "w-full rounded-panel border border-line bg-surface px-3.5 py-3 text-prose text-ink placeholder:text-faint focus:border-ink focus:outline-none";

const submitClass =
  "rounded-panel bg-ink px-3.5 py-3.5 text-prose font-semibold text-paper transition-colors hover:bg-ink/90 disabled:cursor-not-allowed disabled:opacity-50";

function Problem({ children }: { children: string }) {
  return (
    <p role="alert" className="text-ui text-red-text">
      {children}
    </p>
  );
}

/**
 * Six boxes over one real input. The input is transparent and covers the
 * boxes, so typing, pasting and autofill all work as they would in a plain
 * field; the boxes only draw what it holds and outline the next slot.
 */
function CodeInput({ label }: { label: string }) {
  const [value, setValue] = useState("");
  const digits = value.replace(/\D/g, "").slice(0, 6);

  return (
    <div className="relative">
      <input
        name="code"
        value={digits}
        onChange={(event) => setValue(event.target.value)}
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]{6}"
        maxLength={6}
        required
        autoFocus
        aria-label={label}
        className="absolute inset-0 h-full w-full cursor-text opacity-0"
      />
      <CodeBoxes digits={digits} active />
    </div>
  );
}

function CodeBoxes({ digits, active }: { digits: string; active: boolean }) {
  return (
    <div className="flex gap-2.5" aria-hidden="true">
      {Array.from({ length: 6 }, (_, index) => (
        <div
          key={index}
          className={clsx(
            "flex h-14 w-14 items-center justify-center rounded-panel border bg-surface font-mono text-digit",
            active && index === digits.length ? "border-ink" : "border-line",
          )}
        >
          {digits[index] ?? ""}
        </div>
      ))}
    </div>
  );
}
