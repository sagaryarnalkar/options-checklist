const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, AlignmentType, LevelFormat, BorderStyle, WidthType,
  ShadingType, PageOrientation, PageBreak,
} = require('docx');

// --- helpers ---
const border = { style: BorderStyle.SINGLE, size: 4, color: "BBBBBB" };
const borders = { top: border, bottom: border, left: border, right: border };

function p(text, opts = {}) {
  return new Paragraph({
    children: [new TextRun({ text, bold: opts.bold, italics: opts.italics, size: opts.size })],
    spacing: { before: opts.before ?? 60, after: opts.after ?? 60 },
    alignment: opts.alignment,
  });
}

function h(text, level) {
  const headingMap = { 1: HeadingLevel.HEADING_1, 2: HeadingLevel.HEADING_2, 3: HeadingLevel.HEADING_3 };
  return new Paragraph({
    heading: headingMap[level],
    children: [new TextRun({ text })],
  });
}

function bullet(text, level = 0, bold = false) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    children: [new TextRun({ text, bold })],
    spacing: { before: 40, after: 40 },
  });
}

function check(text) {
  return new Paragraph({
    children: [new TextRun({ text: "\u2610  " + text })],
    spacing: { before: 40, after: 40 },
    indent: { left: 360 },
  });
}

function cell(text, opts = {}) {
  return new TableCell({
    borders,
    width: { size: opts.width, type: WidthType.DXA },
    shading: opts.fill ? { fill: opts.fill, type: ShadingType.CLEAR } : undefined,
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [new Paragraph({ children: [new TextRun({ text, bold: opts.bold, size: opts.size ?? 20 })] })],
  });
}

function table(headers, rows, colWidths) {
  const totalWidth = colWidths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: totalWidth, type: WidthType.DXA },
    columnWidths: colWidths,
    rows: [
      new TableRow({
        tableHeader: true,
        children: headers.map((t, i) => cell(t, { width: colWidths[i], fill: "D5E8F0", bold: true })),
      }),
      ...rows.map(r => new TableRow({
        children: r.map((t, i) => cell(t, { width: colWidths[i] })),
      })),
    ],
  });
}

// --- content ---
const children = [];

// Title
children.push(new Paragraph({
  heading: HeadingLevel.TITLE,
  alignment: AlignmentType.CENTER,
  children: [new TextRun({ text: "NIFTY Options — 3:15 PM Daily Checklist" })],
}));
children.push(p("Companion to: Options Strategy Handbook (7 strategies).", { italics: true, alignment: AlignmentType.CENTER, after: 240 }));

// ====== PRE-FLIGHT ======
children.push(h("0. Pre-Flight (before 3:15 PM)", 1));
children.push(p("Keep these charts and data points ready so you can act within the 3:15–3:20 window without scrambling.", { after: 120 }));
children.push(check("Daily NIFTY chart open with Bollinger Bands (21) — for Golden Goose"));
children.push(check("Daily NIFTY chart with EMA 53 — for Nidhi Kalash"));
children.push(check("Chaikin Money Flow (CMF) indicator loaded on daily chart — for Panther"));
children.push(check("2-hour NIFTY chart with VWMA (21) — for Ocean Treasure"));
children.push(check("Note: current spot, India VIX, today's date"));
children.push(check("Compute: days-to-monthly-expiry, days-to-weekly-expiry, is it last Friday?"));

// ====== STEP 1 ======
children.push(h("1. Calendar Check — What kind of day is it?", 1));
children.push(p("Any of these conditions triggers a scheduled action today. Tick whichever apply.", { after: 120 }));

children.push(table(
  ["Condition", "Action Today"],
  [
    ["Wednesday before weekly expiry", "Enter Expiry Double Butterfly at ~3:15 PM"],
    ["Thursday (weekly expiry day) & EDB open", "Close EDB at ~3:25 PM"],
    ["Last Friday of the month", "Enter Batman at ~3:15 PM AND No Brainer NIFTY at ~3:16 PM (both next-month)"],
    ["T-7 from monthly expiry, Golden Goose open", "Close and rollover Golden Goose"],
    ["T-8 from monthly expiry, Panther open", "Close and rollover Panther"],
    ["T-8 or T-9 from monthly expiry, Nidhi Kalash open", "Close and rollover Nidhi Kalash"],
    ["T-4 from current-month expiry, Ocean Treasure open", "Roll the hedge put only (do not touch 3M short put)"],
  ],
  [4320, 5040],
));

// ====== STEP 2 ======
children.push(h("2. Open Position Review (exit / roll triggers)", 1));

children.push(h("Golden Goose — Bull Put / Bear Call monthly credit spread", 3));
children.push(check("Still within wing and credit rules? (monitoring only — no saved mid-cycle SL)"));
children.push(check("Is today T-7 from monthly expiry? → close and rollover per fresh signal"));

children.push(h("Panther — CMF monthly credit spread", 3));
children.push(check("Monitor only (no saved mid-cycle SL)"));
children.push(check("Is today T-8 from monthly expiry? → close and rollover per fresh CMF signal"));

children.push(h("Nidhi Kalash — EMA-53 monthly debit spread", 3));
children.push(check("Monitor only (no saved mid-cycle SL)"));
children.push(check("Is today T-8/T-9 from monthly expiry? → close and rollover per fresh EMA-53 signal"));

children.push(h("Batman — Last-Friday asymmetrical call structure", 3));
children.push(check("MTM ≥ +2% of position cost? → book profit now"));
children.push(check("Otherwise hold — no saved SL, no saved adjustment"));

children.push(h("No Brainer NIFTY — Last-Friday call structure", 3));
children.push(check("MTM ≥ +2.5% of deployed capital? → close all legs"));
children.push(check("MTM ≤ −3% of deployed capital? → close all legs"));
children.push(check("Neither hit & expiry reached? → close at expiry"));

children.push(h("Ocean Treasure — IBBM VWMA", 3));
children.push(check("Is today T-4 from current-month expiry? → roll hedge put only"));
children.push(check("Bullish thesis still intact? → keep 3-month short put untouched"));

children.push(h("Expiry Double Butterfly", 3));
children.push(check("Is today Thursday (weekly expiry)? → close at ~3:25 PM"));

// ====== STEP 3 ======
children.push(h("3. Fresh-Entry Signal Scan (daily)", 1));
children.push(p("Only enter a strategy if the signal fires AND the structural rules validate. Do not enter on signal alone.", { after: 120 }));

// Golden Goose
children.push(h("Golden Goose — Bollinger (21) centerline cross, daily", 2));
children.push(bullet("Close crossed BB-21 centerline upward (below → above)? → Bull Put Credit Spread"));
children.push(bullet("Close crossed BB-21 centerline downward (above → below)? → Bear Call Credit Spread"));
children.push(p("Structural validation before placing:", { bold: true, before: 120 }));
children.push(check("100-point strikes only"));
children.push(check("Sold leg OTM relative to trade direction"));
children.push(check("Wing width ≤ 2% of sold strike"));
children.push(check("Net credit ≈ ₹90 to ₹130"));
children.push(check("Monthly expiry; if today > 15th of month, use next month"));

// Panther
children.push(h("Panther — CMF zero-line cross, daily", 2));
children.push(bullet("CMF crossed negative → positive? → Bull Put Credit Spread"));
children.push(bullet("CMF crossed positive → negative? → Bear Call Credit Spread"));
children.push(p("Structural validation:", { bold: true, before: 120 }));
children.push(check("100-point strikes"));
children.push(check("Wing width 200–400 points"));
children.push(check("Net credit ≥ ~200 points"));
children.push(check("Monthly expiry"));

// Nidhi Kalash
children.push(h("Nidhi Kalash — EMA(53) cross, daily (check at 3:20 PM)", 2));
children.push(bullet("Price crossed EMA-53 upward? → Bull Call Debit Spread"));
children.push(bullet("Price crossed EMA-53 downward? → Bear Put Debit Spread"));
children.push(p("Structural validation:", { bold: true, before: 120 }));
children.push(check("100-point strikes"));
children.push(check("Main leg ≥ 0.5% away from CMP"));
children.push(check("Premium gap (debit) ≈ 90–130 points"));
children.push(check("Strike gap within 2.5%"));
children.push(check("Monthly expiry; if VIX is low, next month is allowed"));

// Ocean Treasure
children.push(h("Ocean Treasure — 2-hour VWMA (21) cross", 2));
children.push(bullet("2-hour close crossed VWMA-21 from below to above? → build bullish income structure"));
children.push(p("Structural validation:", { bold: true, before: 120 }));
children.push(check("Sell 3-month OTM put, premium ≈ ₹200–₹220"));
children.push(check("Buy current-month OTM put, premium ≈ ₹20–₹50"));
children.push(check("Hedge strike within 700 points of short strike"));

// ====== STEP 4 ======
children.push(h("4. Scheduled Cycle Builds (not every day)", 1));

children.push(h("Last Friday of month @ 3:15 — Batman (next-month expiry)", 2));
children.push(bullet("Buy 1 × (spot + 300) CE"));
children.push(bullet("Sell 2 × (spot + 600) CE"));
children.push(bullet("Buy 1 × (spot + 1600) CE"));
children.push(p("Example at spot 24,800: Buy 25,100 CE, Sell 2× 25,400 CE, Buy 26,400 CE.", { italics: true }));

children.push(h("Last Friday of month @ 3:16 — No Brainer NIFTY (next-month expiry)", 2));
children.push(bullet("Buy 1 × (spot + 300) CE"));
children.push(bullet("Sell 2 × (spot + 600) CE"));
children.push(bullet("Buy 1 × (spot + 1600) CE"));
children.push(check("Ensure margin for 2 short calls"));
children.push(check("Set MTM alerts at +2.5% and −3% of deployed capital"));
children.push(p("Example at deployed capital ₹4,00,000: target ₹10,000; stop ₹12,000.", { italics: true }));

children.push(h("Wednesday before weekly expiry @ 3:15 — Expiry Double Butterfly", 2));
children.push(bullet("Let S = current spot; let X = 0.5% of S, rounded to nearest 50 or 100 per chain"));
children.push(bullet("Call fly: Buy S+0.5X CE / Sell 2× S+1.5X CE / Buy S+2.5X CE"));
children.push(bullet("Put fly: Buy S−0.5X PE / Sell 2× S−1.5X PE / Buy S−2.5X PE"));
children.push(bullet("Exit Thursday at ~3:25 PM (no stop-loss, hold through expiry)"));

// ====== STEP 5: Quick grid ======
children.push(new Paragraph({ children: [new PageBreak()] }));
children.push(h("5. 3:15 PM Quick Decision Grid", 1));
children.push(p("Run through this in order every day:", { after: 120 }));
children.push(bullet("1. Calendar: any special-day trigger? (EDB Wed, Batman/NoBrainer last Fri, rollover T-7/T-8/T-9/T-4)", 0, true));
children.push(bullet("2. Open positions: any MTM exit or roll condition hit? (Batman +2%, NoBrainer ±2.5/−3%, OT hedge T-4)", 0, true));
children.push(bullet("3. Signals: scan BB-21, CMF, EMA-53 (3:20), 2h VWMA-21", 0, true));
children.push(bullet("4. If a signal fires: validate strike / credit-debit / wing rules BEFORE placing", 0, true));
children.push(bullet("5. Log the trade: signal, strikes, net credit/debit, rationale, and any rule-deviation", 0, true));

// ====== Rule reference table ======
children.push(h("6. Rule Quick-Reference Table", 1));
children.push(table(
  ["Strategy", "Signal / Trigger", "Structure", "Expiry", "Rollover"],
  [
    ["Golden Goose", "BB-21 centerline cross (daily)", "100-pt credit spread; wing ≤ 2%; credit ₹90–130", "Monthly; >15th → next mo.", "T-7"],
    ["Panther", "CMF zero-line cross (daily)", "100-pt credit spread; wing 200–400; credit ≥ 200", "Monthly", "T-8"],
    ["Nidhi Kalash", "EMA-53 cross (daily, 3:20)", "100-pt debit spread; gap 90–130; ≥0.5% from CMP", "Monthly; VIX low → next mo.", "T-8 / T-9"],
    ["Batman", "Last Friday schedule", "Buy (+300) / Sell 2× (+600) / Buy (+1600) CE", "Next month", "Rebuild next cycle"],
    ["No Brainer NIFTY", "Last Friday schedule", "Buy (+300) / Sell 2× (+600) / Buy (+1600) CE", "Next month", "Rebuild next cycle"],
    ["Expiry Double Butterfly", "Wed before weekly expiry", "Call fly + Put fly around 0.5% wings", "Current weekly", "None (1-day)"],
    ["Ocean Treasure", "2h VWMA-21 bullish cross", "Sell 3M PUT (₹200–220) + Buy 1M hedge (₹20–50)", "3M short + 1M hedge", "Hedge T-4 monthly"],
  ],
  [1440, 1680, 2700, 1800, 1260],
));

// ====== Guard rails ======
children.push(h("7. Non-Negotiable Validation Before Any Entry", 1));
children.push(check("Strike multiples correct (100-point for GG / Panther / NK; 0.5% wings for EDB)"));
children.push(check("Credit/debit falls inside the target band for that strategy"));
children.push(check("Wing / premium-gap / distance rule satisfied"));
children.push(check("Correct expiry month selected per rule"));
children.push(check("Position sizing fits capital plan (especially No Brainer — % based)"));
children.push(check("You are NOT entering outside the saved signal window"));

// ====== Footer note ======
children.push(p("", {}));
children.push(p("Notes:", { bold: true, before: 240 }));
children.push(bullet("Batman, Ocean Treasure final-exit logic in the source handbook is framework-level (no mechanical SL)."));
children.push(bullet("Golden Goose, Panther, Nidhi Kalash have no saved mid-cycle SL — monitoring is structural (roll at T-window)."));
children.push(bullet("Examples in the source handbook are illustrative; re-derive strikes from CURRENT spot on each entry day."));

// --- build document ---
const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial" },
        paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 0 } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: "Arial", color: "1F4E79" },
        paragraph: { spacing: { before: 320, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Arial", color: "2E75B6" },
        paragraph: { spacing: { before: 220, after: 100 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, font: "Arial", color: "333333" },
        paragraph: { spacing: { before: 160, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets",
        levels: [
          { level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 540, hanging: 270 } } } },
          { level: 1, format: LevelFormat.BULLET, text: "\u25E6", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 900, hanging: 270 } } } },
        ] },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
      },
    },
    children,
  }],
});

Packer.toBuffer(doc).then(buf => {
  const out = "/Users/sagary/Downloads/options_315pm_daily_checklist.docx";
  fs.writeFileSync(out, buf);
  console.log("wrote", out, buf.length, "bytes");
});
