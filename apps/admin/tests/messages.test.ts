import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The two catalogues say the same things.
 *
 * A key present in English and missing in Hindi shows a Hindi reader the raw
 * key -- `overview.stats.missed` in the middle of a sentence -- and nothing
 * at build time says so. Every key must exist in both files, and no value
 * may be empty.
 */

type Catalogue = { [key: string]: string | Catalogue };

const read = (locale: string): Catalogue =>
  JSON.parse(readFileSync(path.resolve(__dirname, `../src/messages/${locale}.json`), "utf8")) as Catalogue;

function keysOf(catalogue: Catalogue, prefix = ""): string[] {
  return Object.entries(catalogue).flatMap(([key, value]) =>
    typeof value === "string" ? [`${prefix}${key}`] : keysOf(value, `${prefix}${key}.`),
  );
}

function values(catalogue: Catalogue): [string, string][] {
  return Object.entries(catalogue).flatMap(([key, value]) =>
    typeof value === "string"
      ? [[key, value] as [string, string]]
      : values(value).map(([inner, text]) => [`${key}.${inner}`, text] as [string, string]),
  );
}

describe("the message catalogues", () => {
  const en = read("en");
  const hi = read("hi");

  it("carry the same keys", () => {
    const missingInHindi = keysOf(en).filter((key) => !keysOf(hi).includes(key));
    const missingInEnglish = keysOf(hi).filter((key) => !keysOf(en).includes(key));
    expect(missingInHindi, "keys missing in hi.json").toEqual([]);
    expect(missingInEnglish, "keys missing in en.json").toEqual([]);
  });

  it("leave no sentence empty", () => {
    for (const [locale, catalogue] of [
      ["en", en],
      ["hi", hi],
    ] as const) {
      for (const [key, text] of values(catalogue)) {
        expect(text.trim(), `${locale}: ${key}`).not.toBe("");
      }
    }
  });

  it("write Hindi in Devanagari", () => {
    // Every Hindi sentence with letters in it has at least one Devanagari
    // character, so an English string pasted into hi.json is caught.
    const latinOnly = values(hi).filter(
      ([, text]) => /[A-Za-z]/.test(text) && !/[ऀ-ॿ]/.test(text),
    );
    // Product names, codes and acronyms may be Latin on their own.
    const allowed = new Set([
      "knowledge.docTypes.pdf",
      "knowledge.docTypes.docx",
      "knowledge.docTypes.csv",
      "data.sources.kind",
      "data.sources.tls",
    ]);
    expect(latinOnly.map(([key]) => key).filter((key) => !allowed.has(key))).toEqual([]);
  });
});
