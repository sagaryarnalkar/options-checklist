/**
 * Types for the metric explainers (the ⓘ popovers, components/metrics/MetricHelp.tsx).
 * Pure data types only: no React, no DOM.
 */

export type MetricKey =
  | "iv"
  | "ivr"
  | "ivp"
  | "hv"
  | "vrp"
  | "pop"
  | "p50"
  | "ev"
  | "em"
  | "gex"
  | "flip"
  | "callWall"
  | "putWall"
  | "absGamma"
  | "dex"
  | "vanna"
  | "charm"
  | "rr25"
  | "bf25"
  | "maxPain"
  | "pcr"
  | "pcrChg"
  | "span"
  | "elm"
  | "stt"
  | "delta"
  | "gamma"
  | "theta"
  | "vega"
  | "rho"
  | "buildup"
  | "mwpl"
  | "basis"
  | "rollover"
  | "straddle";

/** How a live value passed to <MetricHelp value={…}/> should be formatted. */
export type MetricUnit = "pct" | "inr" | "inrCompact" | "points" | "ratio" | "score" | "greek" | "text";

export interface MetricInfo {
  key: MetricKey;
  title: string;
  /** Short label, e.g. "IVR". */
  short: string;
  /** One plain-English sentence. */
  oneLiner: string;
  /** Plain text / LaTeX-lite, e.g. "IVR = (IV − IVlow) ÷ (IVhigh − IVlow) × 100". */
  formula: string;
  howToRead: string;
  /** Worked example on an Indian contract (numbers computed with @/lib/options where possible). */
  indiaExample: string;
  pitfalls: string[];
  /** Slug of the lesson that teaches this metric. */
  unit: MetricUnit;
}
