import { describe, expect, it } from "vitest";

import { chosenLocale, localeRedirect, returnPath } from "@/lib/locale-choice";

describe("the panel's language", () => {
  it("is English for a browser that has not chosen", () => {
    expect(chosenLocale(undefined)).toBe("en");
    expect(chosenLocale("")).toBe("en");
    expect(chosenLocale("fr")).toBe("en");
  });

  it("sends the bare address to English", () => {
    expect(localeRedirect("/", undefined)).toBe("/en");
  });

  it("sends the bare address to Hindi once Hindi has been chosen", () => {
    expect(localeRedirect("/", "hi")).toBe("/hi");
  });

  it("opens an old Hindi bookmark in English", () => {
    // The reason this exists: an address typed months ago must not outrank
    // the default, because nobody can see that it is doing so.
    expect(localeRedirect("/hi/login", undefined)).toBe("/en/login");
    expect(localeRedirect("/hi/calls/abc-123", undefined)).toBe("/en/calls/abc-123");
    expect(localeRedirect("/hi", undefined)).toBe("/en");
  });

  it("keeps a Hindi reader in Hindi, whichever link they follow", () => {
    expect(localeRedirect("/hi/calls", "hi")).toBeNull();
    expect(localeRedirect("/en/calls", "hi")).toBe("/hi/calls");
  });

  it("leaves a page alone when the address already matches the setting", () => {
    expect(localeRedirect("/en/outbound", undefined)).toBeNull();
    expect(localeRedirect("/en/outbound", "en")).toBeNull();
  });

  it("does not invent a prefix for a path that has none", () => {
    expect(localeRedirect("/api/events/live", undefined)).toBeNull();
  });
});

describe("returning from the language switch", () => {
  it("comes back to the same page in the new language", () => {
    expect(returnPath("/calls/abc-123", "hi")).toBe("/hi/calls/abc-123");
    expect(returnPath("/", "hi")).toBe("/hi");
  });

  it("does not double the prefix when one is already there", () => {
    expect(returnPath("/hi/calls", "en")).toBe("/en/calls");
  });

  it("refuses to send anyone off this panel", () => {
    // The path arrives as form input, so it is somewhere an attacker could
    // put a link to their own site.
    expect(returnPath("//evil.example.com", "en")).toBe("/en");
    expect(returnPath("https://evil.example.com", "en")).toBe("/en");
    expect(returnPath("\\\\evil.example.com", "en")).toBe("/en");
    expect(returnPath(null, "en")).toBe("/en");
  });
});
