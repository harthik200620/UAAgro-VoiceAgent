import { FlatCompat } from "@eslint/eslintrc";

/**
 * ESLint 9 reads a flat config. `eslint-config-next` 15.5 still ships the
 * eslintrc shape, so it is bridged through FlatCompat until it moves.
 */
const compat = new FlatCompat({ baseDirectory: import.meta.dirname });

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
];

export default config;
