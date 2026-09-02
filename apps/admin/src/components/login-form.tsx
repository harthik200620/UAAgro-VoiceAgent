"use client";

import { useActionState } from "react";

import { authenticate, type LoginState } from "@/app/actions/auth";

/**
 * The panel's front door (§15, §17).
 *
 * Two steps, because §17 makes TOTP mandatory: credentials, then a code. On a
 * first sign-in the API returns an enrolment secret instead of a challenge,
 * and this shows it so the operator can add it to an authenticator.
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
export function LoginForm({
  labels,
}: {
  labels: {
    title: string;
    email: string;
    password: string;
    submit: string;
    mfaTitle: string;
    mfaHint: string;
    enrolTitle: string;
    enrolHint: string;
    secretLabel: string;
    code: string;
    verify: string;
  };
}) {
  const [state, action, pending] = useActionState<LoginState, FormData>(authenticate, {
    step: "credentials",
  });

  if (state.step === "credentials") {
    return (
      <form action={action} className="mx-auto mt-16 w-full max-w-sm">
        <h1 className="mb-4 text-lg font-semibold">{labels.title}</h1>
        <label className="mb-3 block">
          <span className="mb-1 block text-sm text-muted">{labels.email}</span>
          <input
            type="email"
            name="email"
            autoComplete="username"
            required
            className="w-full rounded border border-slate-300 px-2 py-1.5 dark:border-slate-700 dark:bg-slate-900"
          />
        </label>
        <label className="mb-4 block">
          <span className="mb-1 block text-sm text-muted">{labels.password}</span>
          <input
            type="password"
            name="password"
            autoComplete="current-password"
            required
            className="w-full rounded border border-slate-300 px-2 py-1.5 dark:border-slate-700 dark:bg-slate-900"
          />
        </label>
        <button
          type="submit"
          disabled={pending}
          className="w-full rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
        >
          {labels.submit}
        </button>
        {state.error ? <p className="mt-3 text-sm text-danger">{state.error}</p> : null}
      </form>
    );
  }

  const enrolling = state.step === "enrol";

  return (
    <form action={action} className="mx-auto mt-16 w-full max-w-sm">
      <h1 className="mb-2 text-lg font-semibold">
        {enrolling ? labels.enrolTitle : labels.mfaTitle}
      </h1>
      <p className="mb-4 text-sm text-muted">
        {enrolling ? labels.enrolHint : labels.mfaHint}
      </p>

      {enrolling ? (
        <div className="mb-4">
          <span className="mb-1 block text-sm text-muted">{labels.secretLabel}</span>
          <code className="block break-all rounded border border-slate-200 bg-slate-50 p-2 text-sm dark:border-slate-800 dark:bg-slate-900">
            {state.secret}
          </code>
        </div>
      ) : null}

      <label className="mb-4 block">
        <span className="mb-1 block text-sm text-muted">{labels.code}</span>
        <input
          type="text"
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]{6}"
          maxLength={6}
          required
          className="w-full rounded border border-slate-300 px-2 py-1.5 font-mono tracking-widest dark:border-slate-700 dark:bg-slate-900"
        />
      </label>
      <button
        type="submit"
        disabled={pending}
        className="w-full rounded border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-100 disabled:opacity-40 dark:border-slate-700 dark:hover:bg-slate-800"
      >
        {labels.verify}
      </button>
      {state.error ? <p className="mt-3 text-sm text-danger">{state.error}</p> : null}
    </form>
  );
}
