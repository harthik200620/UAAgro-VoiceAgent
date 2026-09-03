import "server-only";

/**
 * Server-side configuration.
 *
 * The `server-only` import at the top is the whole point of this file. It is a
 * package that throws at build time if the module is reached from a Client
 * Component, so an accidental `import { env } from "@/server/env"` in a
 * `"use client"` file fails `next build` rather than shipping a vendor key to
 * every browser that loads the panel.
 *
 * Prompts, flows, catalogue logic and crop recommendations never leave the
 * server, and no API key ever reaches a client bundle. Next.js inlines
 * anything in `next.config`'s `env` block and anything prefixed `NEXT_PUBLIC_`
 * directly into client JavaScript, so the rule here is simple and mechanical:
 * nothing in this file is ever prefixed `NEXT_PUBLIC_`, and nothing outside
 * this file reads a secret.
 *
 * Values are read lazily rather than at module load. A panel that refuses to
 * start because one variable is missing is a panel an operator cannot use to
 * find out which one.
 */

export type Env = {
  /** The FastAPI control plane. Server-side fetches only. */
  apiBaseUrl: string;
  /** Signs the session cookie. Never sent anywhere. */
  sessionSecret: string;
};

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    // Name the variable. An operator reading "configuration error" has to
    // guess; one reading "API_BASE_URL is not set" does not.
    throw new Error(
      `${name} is not set. The admin panel needs it to reach the control plane. ` +
        `See .env.example.`,
    );
  }
  return value;
}

export function env(): Env {
  return {
    apiBaseUrl: required("API_BASE_URL"),
    sessionSecret: required("ADMIN_SESSION_SECRET"),
  };
}
