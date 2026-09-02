/**
 * A call's language code as a word, or the code itself when it is one the
 * catalogue does not name. Kept as a plain function so both server and
 * client components can call it with their own `t`.
 */
export function languageName(
  code: string,
  t: { has: (key: string) => boolean; (key: string): string },
): string {
  if (!code) return "—";
  return t.has(code) ? t(code) : code;
}
