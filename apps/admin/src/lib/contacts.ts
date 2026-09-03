/**
 * The pasted contact list: one contact per line, `number` or `name, number`.
 *
 * Client-safe on purpose, and deliberately only a counter: reading a file
 * belongs to the control plane (`services/contact_import.py`), because a
 * workbook is a zip of XML and because "which cell is the phone number"
 * should be decided once, in the language that also validates it, with tests.
 */

const PHONE_SHAPE = /^\+?[\d\s\-()]{8,18}$/;

function looksLikePhone(cell: string): boolean {
  const digits = cell.replace(/\D/g, "");
  return PHONE_SHAPE.test(cell.trim()) && digits.length >= 10 && digits.length <= 13;
}

function cells(line: string): string[] {
  return line
    .split(/[,;\t]/)
    .map((cell) => cell.trim().replace(/^"|"$/g, "").trim())
    .filter(Boolean);
}

/** How many lines carry something that reads as a phone number. */
export function countContacts(text: string): number {
  return text.split(/\r?\n/).filter((line) => cells(line).some(looksLikePhone)).length;
}
