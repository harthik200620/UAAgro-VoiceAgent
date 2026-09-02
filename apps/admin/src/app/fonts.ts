import {
  IBM_Plex_Mono,
  IBM_Plex_Sans,
  IBM_Plex_Sans_Devanagari,
  Instrument_Serif,
  Tiro_Devanagari_Hindi,
} from "next/font/google";

/**
 * The faces of the design, self-hosted by next/font: no request leaves for
 * Google at runtime and text never flashes through a fallback face.
 *
 * Four of them are on every page. The fifth, the Devanagari display face, is
 * imported by the sign-in screen alone -- it sets one word there and nothing
 * anywhere else, and a face that large has no business being fetched by an
 * operator who is looking at a call.
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

/**
 * The sign-in hero, "नमस्ते।", and nothing else.
 *
 * Exported on its own and applied by that page, so its bytes are requested by
 * the one route that renders it.
 */
export const heroFont = Tiro_Devanagari_Hindi({
  subsets: ["devanagari"],
  weight: "400",
  variable: "--font-hero",
  display: "swap",
});

export const fontVariables = [sans, devanagari, mono, serif]
  .map((font) => font.variable)
  .join(" ");
