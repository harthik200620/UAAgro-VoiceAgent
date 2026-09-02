/**
 * The pasted contact list: one contact per line, `number` or `name, number`.
 *
 * Client-safe on purpose. A CSV is turned into the same text in the browser,
 * so the operator sees exactly the list that will be sent and the server
 * receives one shape whichever way the numbers arrived.
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

/**
 * CSV rows to `name, number` lines. Header rows and rows without a number
 * are dropped; the name is the first cell that is not the number.
 */
export function csvToContactLines(csv: string): string[] {
  const lines: string[] = [];
  for (const row of csv.split(/\r?\n/)) {
    const parts = cells(row);
    const number = parts.find(looksLikePhone);
    if (!number) continue;
    const name = parts.find((cell) => cell !== number && !looksLikePhone(cell));
    lines.push(name ? `${name}, ${number}` : number);
  }
  return lines;
}
