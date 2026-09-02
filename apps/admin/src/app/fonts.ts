import {
  IBM_Plex_Mono,
  IBM_Plex_Sans,
  IBM_Plex_Sans_Devanagari,
  Instrument_Serif,
  Tiro_Devanagari_Hindi,
} from "next/font/google";

/**
 * The five faces of the design, self-hosted by next/font: no request leaves
 * for Google at runtime and text never flashes through a fallback face.
 */
const sans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-sans",
  display: "swap",
});

const devanagari = IBM_Plex_Sans_Devanagari({
  subsets: ["devanagari", "latin"],
  weight: ["400", "500", "600"],
  variable: "--font-devanagari",
  display: "swap",
});

const mono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-mono",
  display: "swap",
});

const serif = Instrument_Serif({
  subsets: ["latin"],
  weight: "400",
  variable: "--font-serif",
  display: "swap",
});

// Only the login hero, "नमस्ते।", uses it.
const hero = Tiro_Devanagari_Hindi({
  subsets: ["devanagari"],
  weight: "400",
  variable: "--font-hero",
  display: "swap",
});

export const fontVariables = [sans, devanagari, mono, serif, hero]
  .map((font) => font.variable)
  .join(" ");
