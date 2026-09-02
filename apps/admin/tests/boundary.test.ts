import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * §1 N6 and §17: nothing secret reaches the browser bundle.
 *
 * These are source-level checks rather than bundle analysis, and that is a
 * deliberate trade. A bundle check is stronger but only runs after a build and
 * only catches what the build happened to include; these run in milliseconds
 * and catch the mistake at the moment it is made -- which is when someone adds
 * `"use client"` to a file that reaches the API, or writes NEXT_PUBLIC_ in
 * front of a key because the value would not otherwise be visible.
 *
 * Both are worth having. This is the one that fires first.
 */

const SRC = path.resolve(__dirname, "../src");

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) return walk(full);
    return /\.tsx?$/.test(entry) ? [full] : [];
  });
}

const FILES = walk(SRC).map((file) => ({
  path: path.relative(SRC, file).replaceAll("\\", "/"),
  source: readFileSync(file, "utf8"),
}));

const isClient = (source: string) =>
  /^\s*["']use client["']/m.test(source.split("\n").slice(0, 5).join("\n"));

describe("the server-only boundary", () => {
  it("finds source to check", () => {
    // A walk that silently returned nothing would make every test below pass.
    expect(FILES.length).toBeGreaterThan(5);
  });

  it("marks every module that reads configuration as server-only", () => {
    // `server-only` throws at build time when reached from a Client Component,
    // which turns an accidental import into a failed build rather than a
    // vendor key in every browser.
    for (const file of FILES) {
      if (!/process\.env/.test(file.source)) continue;
      if (file.path === "middleware.ts" || file.path.startsWith("i18n/")) continue;
      expect(file.source, `${file.path} reads process.env`).toMatch(
        /import ["']server-only["']/,
      );
    }
  });

  it("never exposes a secret through NEXT_PUBLIC_", () => {
    // Next inlines anything prefixed NEXT_PUBLIC_ directly into client
    // JavaScript. One well-meaning entry ships the key to everyone.
    for (const file of FILES) {
      const matches = file.source.match(/NEXT_PUBLIC_[A-Z0-9_]+/g) ?? [];
      for (const name of matches) {
        expect(
          /KEY|SECRET|TOKEN|PASSWORD|DSN|CREDENTIAL/.test(name),
          `${file.path} exposes ${name} to the browser`,
        ).toBe(false);
      }
    }
  });

  it("keeps client components away from the control-plane client", () => {
    for (const file of FILES) {
      if (!isClient(file.source)) continue;
      expect(
        file.source,
        `${file.path} is a client component importing the API client`,
      ).not.toMatch(/from ["']@\/server\//);
    }
  });

  it("keeps prompts and model configuration out of client components", () => {
    // §1 N6: prompts, flows, catalogue logic and crop recommendations never
    // reach a client bundle. A prompt in the browser is a prompt a competitor
    // can read and a caller can learn to steer.
    for (const file of FILES) {
      if (!isClient(file.source)) continue;
      for (const forbidden of [
        "systemPrompt",
        "system_prompt",
        "INBOUND_SYSTEM_PROMPT",
        "llm_primary_model",
        "toolAllowlist",
      ]) {
        expect(
          file.source.includes(forbidden),
          `${file.path} carries ${forbidden} into the browser`,
        ).toBe(false);
      }
    }
  });

  it("never renders a full phone number", () => {
    // §17 and §23-6: the panel receives `phone_last4` and nothing else, so
    // there is no field it could render. This catches a component that starts
    // reading one after an API change.
    for (const file of FILES) {
      for (const forbidden of ["phone_enc", "phoneEnc", "fullPhone", "phone_plain"]) {
        expect(
          file.source.includes(forbidden),
          `${file.path} references ${forbidden}`,
        ).toBe(false);
      }
    }
  });
});
