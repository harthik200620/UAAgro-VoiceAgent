import type { Config } from "tailwindcss";

/**
 * §15's design bar: "dense, fast, and legible. This is an operations tool used
 * during a peak-season rush, not a marketing site."
 *
 * So the scale below is tighter than Tailwind's default and the base size is
 * larger, not smaller -- density comes from spacing, never from shrinking text
 * a centre manager reads on a phone in daylight.
 */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        // Devanagari needs a font that actually has the conjuncts. Noto is the
        // safe default; the system stack behind it keeps Latin crisp.
        sans: ["Noto Sans", "Noto Sans Devanagari", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontSize: {
        // Devanagari matras sit above and below the baseline, so Hindi needs
        // more line height than Latin at the same size or the rows collide.
        xs: ["0.8125rem", { lineHeight: "1.4" }],
        sm: ["0.875rem", { lineHeight: "1.5" }],
        base: ["0.9375rem", { lineHeight: "1.6" }],
      },
      colors: {
        // Status colours used across the call list and the compliance gate.
        // Named by meaning rather than by hue so a theme change cannot make
        // "blocked" green.
        ok: "rgb(21 128 61)",
        warn: "rgb(180 83 9)",
        danger: "rgb(185 28 28)",
        muted: "rgb(100 116 139)",
      },
    },
  },
  plugins: [],
};

export default config;
