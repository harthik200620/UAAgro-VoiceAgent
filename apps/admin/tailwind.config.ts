import type { Config } from "tailwindcss";

/**
 * Design tokens, lifted verbatim from docs/design/admin-panel/*.dc.html.
 *
 * Colours are named for what they mean on the page (paper, ink, amber = a
 * call in progress) rather than for their hue, so a status can never be
 * carried by a colour that also decorates something else. Text sizes keep the
 * mockups' exact pixel values: the panel is read at arm's length in a control
 * room, and "close enough" drifts a table out of alignment.
 */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "#F7F5EF",
        surface: "#FFFFFF",
        inset: "#F1EEE6",
        line: "#E6E1D6",
        ink: "#17160F",
        muted: "#6B6759",
        faint: "#9B9689",
        // The logo tile and the login panel only.
        brand: "#1E5B3A",
        amber: { DEFAULT: "#F2B33D", text: "#7A4E00", bg: "#FBEFD2" },
        green: { DEFAULT: "#3C9D5D", text: "#1C5E33", bg: "#DDF0E3" },
        red: { DEFAULT: "#D2492E", text: "#8E2B17", bg: "#F8DFD9" },
        grey: "#D9D4C8",
        // The "speed of reply" ramp: one hue in five validated steps. Never
        // reused for a status, so a latency bar cannot read as a warning.
        latency: {
          line: "#86b6ef",
          turn: "#5598e7",
          stt: "#2a78d6",
          think: "#1c5cab",
          voice: "#0d366b",
        },
      },
      fontFamily: {
        sans: ["var(--font-sans)", "var(--font-devanagari)", "system-ui", "sans-serif"],
        hindi: ["var(--font-devanagari)", "var(--font-sans)", "system-ui", "sans-serif"],
        serif: ["var(--font-serif)", "Georgia", "Times New Roman", "serif"],
        mono: ["var(--font-mono)", "ui-monospace", "Consolas", "monospace"],
        hero: ["var(--font-hero)", "Noto Serif Devanagari", "serif"],
      },
      fontSize: {
        micro: ["10px", { lineHeight: "1.4" }],
        tiny: ["11px", { lineHeight: "1.4" }],
        meta: ["11.5px", { lineHeight: "1.4" }],
        label: ["12px", { lineHeight: "1.4" }],
        small: ["12.5px", { lineHeight: "1.45" }],
        body: ["13px", { lineHeight: "1.5" }],
        ui: ["13.5px", { lineHeight: "1.5" }],
        base: ["14px", { lineHeight: "1.5" }],
        name: ["14.5px", { lineHeight: "1.4" }],
        prose: ["15px", { lineHeight: "1.65" }],
        lead: ["15.5px", { lineHeight: "1.65" }],
        md: ["16px", { lineHeight: "1.4" }],
        lg: ["18px", { lineHeight: "1.3" }],
        xl: ["20px", { lineHeight: "1.3" }],
        digit: ["22px", { lineHeight: "1" }],
        "num-sm": ["26px", { lineHeight: "1" }],
        num: ["28px", { lineHeight: "1" }],
        "hero-sub": ["30px", { lineHeight: "1.25" }],
        "title-sm": ["32px", { lineHeight: "1.1", letterSpacing: "-0.01em" }],
        title: ["38px", { lineHeight: "1.05", letterSpacing: "-0.01em" }],
        "title-lg": ["40px", { lineHeight: "1.05", letterSpacing: "-0.01em" }],
        "num-lg": ["44px", { lineHeight: "1", letterSpacing: "-0.02em" }],
        "num-xl": ["46px", { lineHeight: "1", letterSpacing: "-0.02em" }],
        hero: ["112px", { lineHeight: "1.1", letterSpacing: "-0.01em" }],
      },
      spacing: {
        4.5: "18px",
        5.5: "22px",
        6.5: "26px",
        7.5: "30px",
        8.5: "34px",
        11.5: "46px",
        13: "52px",
      },
      borderRadius: { tag: "6px", btn: "8px", panel: "10px", card: "12px" },
      boxShadow: { card: "0 1px 2px rgba(23,22,15,.04)" },
      keyframes: {
        live: {
          "0%": { boxShadow: "0 0 0 0 rgba(242,179,61,.55)" },
          "70%": { boxShadow: "0 0 0 9px rgba(242,179,61,0)" },
          "100%": { boxShadow: "0 0 0 0 rgba(242,179,61,0)" },
        },
        dots: {
          "0%, 20%": { opacity: "0.2" },
          "50%": { opacity: "1" },
          "100%": { opacity: "0.2" },
        },
      },
      animation: {
        live: "live 1.6s ease-out infinite",
        dots: "dots 1.2s infinite",
      },
    },
  },
  plugins: [],
};

export default config;
