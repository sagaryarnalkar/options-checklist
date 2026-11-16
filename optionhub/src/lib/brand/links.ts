/**
 * HeroZero brand facts for titles, the PWA manifest and site metadata.
 *
 * Pure and dependency-free (no "@/" imports): vite.config.ts and seo.ts import it directly.
 */

export const BRAND = {
  name: "HeroZero",
  product: "HeroZero",
  /** Full product name used in titles and the manifest. */
  productFull: "HeroZero Trader Terminal",
  /** Canonical home when VITE_SITE_URL is not set (the dashboard's build always sets it). */
  projectPage: "/",
  disclaimer: "Data for education and research. Not investment advice.",
} as const;
