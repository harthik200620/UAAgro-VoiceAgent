import type { SourceMapping, SourceTable, TableMap } from "./contract";

/**
 * The mapping screen's vocabulary: our fields per table, exactly as the
 * contract lists them, with the required ones marked. The API validates the
 * same list; this copy is what lets the screen refuse an incomplete mapping
 * before a round trip.
 */

export const SOURCE_TABLES: readonly SourceTable[] = ["stores", "products", "stock"];

export type SourceField = { name: string; required: boolean };

const field = (name: string, required = false): SourceField => ({ name, required });

export const SOURCE_FIELDS: Record<SourceTable, readonly SourceField[]> = {
  stores: [
    field("code", true),
    field("name", true),
    field("name_hi"),
    field("district", true),
    field("block"),
    field("address"),
    field("pincode"),
    field("latitude"),
    field("longitude"),
    field("phone"),
    field("manager_name"),
    field("manager_phone"),
    field("open_time"),
    field("close_time"),
  ],
  products: [
    field("sku", true),
    field("name", true),
    field("name_hi"),
    field("category", true),
    field("brand"),
    field("pack_size", true),
    field("mrp", true),
    field("description"),
  ],
  stock: [
    field("store_code", true),
    field("sku", true),
    field("qty", true),
    field("price"),
    field("is_available"),
  ],
};

/** The required fields of `table` that `columns` leaves unmapped. */
export function missingRequired(table: SourceTable, columns: Record<string, string>): string[] {
  return SOURCE_FIELDS[table]
    .filter((item) => item.required && !columns[item.name])
    .map((item) => item.name);
}

export type MappingProblems = Record<SourceTable, string[]>;

/**
 * What stops a mapping from being saved: for every table that names a source
 * table, the required fields still unmapped. A table left null is simply not
 * synced, which is allowed -- a client may keep stock elsewhere.
 */
export function validateMapping(mapping: SourceMapping): MappingProblems {
  const problems = { stores: [], products: [], stock: [] } as MappingProblems;
  for (const table of SOURCE_TABLES) {
    const map = mapping[table];
    if (!map) continue;
    problems[table] = map.table ? missingRequired(table, map.columns) : ["table"];
  }
  return problems;
}

export function isMappingComplete(mapping: SourceMapping): boolean {
  const problems = validateMapping(mapping);
  return SOURCE_TABLES.every((table) => problems[table].length === 0);
}

/** A mapping with nothing chosen yet, for a source just added. */
export const EMPTY_MAPPING: SourceMapping = { stores: null, products: null, stock: null };

/** One table's map with a column set or cleared. */
export function withColumn(map: TableMap, ourField: string, theirColumn: string): TableMap {
  const columns = { ...map.columns };
  if (theirColumn) columns[ourField] = theirColumn;
  else delete columns[ourField];
  return { ...map, columns };
}
