import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Every Server Action refuses through one door.
 *
 * A source-level check, like the rest of `boundary.test.ts`, and for the same
 * reason: it catches the mistake when it is made rather than when an operator
 * hits it. The mistake it catches is specific and was real — an action that
 * refuses on `!session` alone tells somebody *"Your account cannot do this"*
 * when the control plane is simply down, which sends them to look at roles
 * for a problem that is a process being restarted.
 */

const ACTIONS = path.resolve(__dirname, "../src/app/actions");

const FILES = readdirSync(ACTIONS)
  .filter((name) => name.endsWith(".ts"))
  .map((name) => ({ name, source: readFileSync(path.join(ACTIONS, name), "utf8") }));

describe("the way an action refuses", () => {
  it("finds the actions to check", () => {
    expect(FILES.length).toBeGreaterThan(4);
  });

  it("never decides on the absence of a session alone", () => {
    // `!session` is true for "signed out" and for "the API did not answer",
    // and those are different sentences. `guard()` is where they are told
    // apart, so no action reaches for the raw check.
    for (const file of FILES) {
      expect(file.source, `${file.name} refuses on a bare !session`).not.toMatch(
        /if \(!session\b/,
      );
    }
  });

  it("routes every permission check through the guard", () => {
    const usingCapabilities = FILES.filter((file) => /\bguard\(/.test(file.source));
    expect(usingCapabilities.length).toBeGreaterThan(4);
    for (const file of usingCapabilities) {
      expect(file.source, `${file.name} imports guard`).toMatch(
        /from ["']@\/server\/guard["']/,
      );
    }
  });
});

describe("the sentences an operator reads", () => {
  const messages = (locale: string) =>
    JSON.parse(
      readFileSync(path.resolve(__dirname, `../src/messages/${locale}.json`), "utf8"),
    ) as { actions: Record<string, string> };

  it("has a distinct one for each of the three refusals, in both languages", () => {
    for (const locale of ["en", "hi"]) {
      const actions = messages(locale).actions;
      const three = [actions.forbidden, actions.signedOut, actions.controlPlaneDown];
      for (const sentence of three) {
        expect(sentence, `${locale} is missing one of the three`).toBeTruthy();
      }
      expect(new Set(three).size, `${locale} reuses a sentence`).toBe(3);
    }
  });

  it("does not blame the account for an outage", () => {
    const outage = (messages("en").actions.controlPlaneDown ?? "").toLowerCase();
    expect(outage).not.toContain("your account");
    // And it says nothing was changed, because nothing was.
    expect(outage).toContain("nothing was changed");
  });
});
