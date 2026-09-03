import { describe, expect, it } from "vitest";

import type { SourceMapping } from "@/lib/contract";
import {
  EMPTY_MAPPING,
  isMappingComplete,
  missingRequired,
  SOURCE_FIELDS,
  validateMapping,
  withColumn,
} from "@/lib/data-sources";

describe("the mapping screen's fields", () => {
  it("lists exactly the contract's required fields", () => {
    const required = (table: keyof typeof SOURCE_FIELDS) =>
      SOURCE_FIELDS[table].filter((field) => field.required).map((field) => field.name);
    expect(required("stores")).toEqual(["code", "name", "district"]);
    expect(required("products")).toEqual(["sku", "name", "category", "pack_size", "mrp"]);
    expect(required("stock")).toEqual(["store_code", "sku", "qty"]);
  });

  it("names the required fields still unmapped", () => {
    expect(missingRequired("stock", { store_code: "shop_id", qty: "quantity" })).toEqual(["sku"]);
    expect(missingRequired("stock", { store_code: "a", sku: "b", qty: "c" })).toEqual([]);
  });
});

describe("validating a mapping", () => {
  it("leaves a table that is not synced alone", () => {
    expect(validateMapping(EMPTY_MAPPING)).toEqual({ stores: [], products: [], stock: [] });
    expect(isMappingComplete(EMPTY_MAPPING)).toBe(true);
  });

  it("refuses a synced table without a source table or with required fields missing", () => {
    const mapping: SourceMapping = {
      stores: { table: "", columns: {} },
      products: { table: "items", columns: { sku: "item_code", name: "item_name" } },
      stock: null,
    };
    expect(validateMapping(mapping)).toEqual({
      stores: ["table"],
      products: ["category", "pack_size", "mrp"],
      stock: [],
    });
    expect(isMappingComplete(mapping)).toBe(false);
  });
});

describe("choosing a column", () => {
  it("sets and clears one field without touching the others", () => {
    const map = { table: "shops", columns: { code: "shop_code" } };
    expect(withColumn(map, "name", "shop_name").columns).toEqual({ code: "shop_code", name: "shop_name" });
    expect(withColumn(map, "code", "").columns).toEqual({});
    // The original is not changed.
    expect(map.columns).toEqual({ code: "shop_code" });
  });
});
