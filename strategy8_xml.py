#!/usr/bin/env python3
"""Inject Strategy 8 (Golden Goose LEAPS) into the handbook XML."""

import re
from pathlib import Path

DOC = Path("/Users/sagary/Claude Work Folder/OptionsStrats/handbook_unpacked/word/document.xml")

# Anchor: the paragraph that immediately precedes the page break to "Cross-Strategy Operating Notes"
ANCHOR = """      <w:r>
        <w:t>• Only the protection leg is refreshed.</w:t>
      </w:r>
    </w:p>
    <w:p>
      <w:r>
        <w:br w:type="page"/>
      </w:r>
    </w:p>
    <w:p>
      <w:pPr>
        <w:pStyle w:val="H1Custom"/>
      </w:pPr>
      <w:r>
        <w:t>Cross-Strategy Operating Notes</w:t>"""

def h1(text):
    return f"""    <w:p>
      <w:pPr>
        <w:pStyle w:val="H1Custom"/>
      </w:pPr>
      <w:r>
        <w:t>{text}</w:t>
      </w:r>
    </w:p>"""

def h2(text):
    return f"""    <w:p>
      <w:pPr>
        <w:pStyle w:val="H2Custom"/>
      </w:pPr>
      <w:r>
        <w:t>{text}</w:t>
      </w:r>
    </w:p>"""

def para(text):
    return f"""    <w:p>
      <w:pPr/>
      <w:r>
        <w:t xml:space="preserve">{text}</w:t>
      </w:r>
    </w:p>"""

PAGE_BREAK = """    <w:p>
      <w:r>
        <w:br w:type="page"/>
      </w:r>
    </w:p>"""

def summary_table(objective, style):
    return f"""    <w:tbl>
      <w:tblPr>
        <w:tblStyle w:val="TableGrid"/>
        <w:tblW w:type="auto" w:w="0"/>
        <w:jc w:val="center"/>
        <w:tblLook w:firstColumn="1" w:firstRow="1" w:lastColumn="0" w:lastRow="0" w:noHBand="0" w:noVBand="1" w:val="04A0"/>
      </w:tblPr>
      <w:tblGrid>
        <w:gridCol w:w="5043"/>
        <w:gridCol w:w="5043"/>
      </w:tblGrid>
      <w:tr>
        <w:tc>
          <w:tcPr>
            <w:tcW w:type="dxa" w:w="5043"/>
            <w:tcMar><w:top w:w="90" w:type="dxa"/><w:start w:w="90" w:type="dxa"/><w:bottom w:w="90" w:type="dxa"/><w:end w:w="90" w:type="dxa"/></w:tcMar>
            <w:vAlign w:val="center"/>
            <w:shd w:fill="EAF3FB"/>
          </w:tcPr>
          <w:p><w:r><w:rPr><w:b/><w:sz w:val="18"/></w:rPr><w:t>Objective</w:t></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:tcPr>
            <w:tcW w:type="dxa" w:w="5043"/>
            <w:tcMar><w:top w:w="90" w:type="dxa"/><w:start w:w="90" w:type="dxa"/><w:bottom w:w="90" w:type="dxa"/><w:end w:w="90" w:type="dxa"/></w:tcMar>
            <w:vAlign w:val="center"/>
          </w:tcPr>
          <w:p><w:r><w:rPr><w:sz w:val="18"/></w:rPr><w:t xml:space="preserve">{objective}</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc>
          <w:tcPr>
            <w:tcW w:type="dxa" w:w="5043"/>
            <w:tcMar><w:top w:w="90" w:type="dxa"/><w:start w:w="90" w:type="dxa"/><w:bottom w:w="90" w:type="dxa"/><w:end w:w="90" w:type="dxa"/></w:tcMar>
            <w:vAlign w:val="center"/>
            <w:shd w:fill="EAF3FB"/>
          </w:tcPr>
          <w:p><w:r><w:rPr><w:b/><w:sz w:val="18"/></w:rPr><w:t>Operating Style</w:t></w:r></w:p>
        </w:tc>
        <w:tc>
          <w:tcPr>
            <w:tcW w:type="dxa" w:w="5043"/>
            <w:tcMar><w:top w:w="90" w:type="dxa"/><w:start w:w="90" w:type="dxa"/><w:bottom w:w="90" w:type="dxa"/><w:end w:w="90" w:type="dxa"/></w:tcMar>
            <w:vAlign w:val="center"/>
          </w:tcPr>
          <w:p><w:r><w:rPr><w:sz w:val="18"/></w:rPr><w:t xml:space="preserve">{style}</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
    </w:tbl>
    <w:p/>"""

sections = []
sections.append(PAGE_BREAK)
sections.append(h1("8. Golden Goose LEAPS — Positional Option Selling with Monthly Hedge"))
sections.append(summary_table(
    "Positional option selling on LEAPS hedged with a rolling monthly option. One 3:15 PM check per day; position flips only on BB-21 midline cross.",
    "Low-frequency positional trading (~1–2 trades per month). Hedge rolled on the 18th of each month. Short LEAPS flipped only on signal change."
))

# Objective
sections.append(h2("Objective"))
sections.append(para("Positional option selling on LEAPS hedged with a rolling monthly option. Only one 3:15 PM chart check per day. The position is held until the BB-21 midline signal flips. Approximately 1–2 trades per month on average."))

# Setup
sections.append(h2("Setup"))
sections.append(para("Chart timeframe: Daily."))
sections.append(para("Indicator: Bollinger Bands, period 21. Hide upper band, lower band, and background — keep only the middle line (median) visible. The BB-21 midline is identical to a 21-period SMA, but the creator retains the Bollinger structure because the upper and lower bands are used in a separate &#x2018;Bhalla Trading&#x2019; concept that builds on this setup."))
sections.append(para("Decision time: 3:15 PM India time, exactly once per day. No intraday or mid-day checks."))

# Signal Logic
sections.append(h2("Signal Logic"))
sections.append(para("Bearish signal: 1D candle closes below the BB-21 midline at 3:15 PM."))
sections.append(para("Trade taken: Sell a CALL LEAPS option."))
sections.append(para("Bullish signal: 1D candle closes above the BB-21 midline at 3:15 PM."))
sections.append(para("Trade taken: Sell a PUT LEAPS option."))
sections.append(para("Hold logic: a short call is held until a daily 3:15 PM close above the midline, at which point the position is flipped to a short put. The reverse applies to a short put."))
sections.append(para("No intraday trading, no mid-day re-checks, no additional entries. Strictly one decision per day."))

# Strike Selection
sections.append(h2("Strike Selection and Structure Rules"))
sections.append(para("Short leg expiry (LEAPS) is chosen by the calendar quarter in which the trade enters: Q1 (Jan–Mar) → March LEAPS; Q2 (Apr–Jun) → June LEAPS; Q3 (Jul–Sep) → September LEAPS; Q4 (Oct–Dec) → December LEAPS."))
sections.append(para("Strike must be a multiple of 500 or 1000 NIFTY points (these LEAPS strikes are the most liquid)."))
sections.append(para("Short leg must be OTM."))
sections.append(para("Target premium for the sold LEAPS: approximately &#x20B9;200–&#x20B9;350 (can stretch to &#x20B9;200–&#x20B9;450 if liquidity shifts the pricing)."))
sections.append(para("Hedge leg: buy a current-month option approximately 2% away from the sold strike (&#x2248; 500 NIFTY points). Same direction as the short — a call hedge against a short call, a put hedge against a short put."))
sections.append(para("If a fresh entry signal fires after the 15th of a calendar month, skip the current month for the hedge and take next month&#x2019;s hedge directly."))
sections.append(para("Additional timing rule: if a fresh entry signal fires between roughly 15–20 February, take the June LEAPS for the short leg rather than the March LEAPS, to avoid an overly short remaining tenor."))

# Worked Example
sections.append(h2("Worked Entry Example"))
sections.append(para("On 3 October 2024 at 3:15 PM the 1D candle closed below the BB-21 midline &#x2192; bearish signal &#x2192; sell CALL LEAPS."))
sections.append(para("Sold: 26,500 CE December 2024 LEAPS @ &#x20B9;33 (strike is a multiple of 500, OTM, premium in target band)."))
sections.append(para("Hedge: Buy 27,000 CE October 2024 monthly @ &#x20B9;12 (&#x2248; 500 points / 2% away, same direction as the short)."))
sections.append(para("On 18 October 2024 the monthly hedge was rolled to the November expiry 27,000 CE."))
sections.append(para("On 18 November 2024 the monthly hedge was rolled again to the December expiry 27,000 CE."))
sections.append(para("On 25 November 2024 at 3:15 PM the 1D candle closed above the midline &#x2192; signal flipped &#x2192; close the short call and its current hedge, and open a fresh short PUT LEAPS plus its new monthly hedge per the rules."))
sections.append(para("Trade life on the short CALL side: approximately two months."))

# Position Management
sections.append(h2("Position Management"))
sections.append(para("Position is observed once per day at 3:15 PM only. No intraday interaction with the position."))
sections.append(para("The structure is inherently hedged by the near-month long leg, so there is no separate mid-cycle stop-loss."))
sections.append(para("No-trade zones apply around major scheduled news / events: avoid new entries until after the event passes."))
sections.append(para("Discipline note: stretches of several weeks without any new signal are normal. Lower screen time is the feature, not a bug."))

# Exit Logic
sections.append(h2("Exit Logic"))
sections.append(para("Primary exit: the BB-21 midline signal flips at a 3:15 PM daily close. Both the short LEAPS and its current monthly hedge are closed; a new opposite short LEAPS plus its fresh monthly hedge are opened the same evening."))
sections.append(para("There is no expiry-based forced exit on the short LEAPS leg — the position holds until the signal flips."))
sections.append(para("No discretionary stop-loss. No fixed profit target. The signal drives every entry and every exit."))

# Rollover Logic
sections.append(h2("Rollover Logic"))
sections.append(para("The short LEAPS leg is not rolled on a fixed calendar. It is held until the BB-21 midline signal flips."))
sections.append(para("The monthly hedge is rolled on the 18th of every month. If the 18th is a market holiday, roll on the prior trading day."))
sections.append(para("Worked hedge-roll example: holding Short 26,500 CE Dec 2024 LEAPS + Long 27,000 CE October monthly. On 18 October close the October 27,000 CE hedge and buy a fresh November 27,000 CE hedge (retaining the same &#x2248; 2% / 500-point distance, current-month tenor, same direction). The short 26,500 CE December LEAPS remains untouched."))
sections.append(para("Important: only the hedge is rolled on the monthly schedule. The short LEAPS is rolled only when the BB-21 signal flips."))

# Note on overlap with existing Golden Goose
sections.append(h2("Note: Relationship to the Credit-Spread Golden Goose (Strategy 1)"))
sections.append(para("Both &#x2018;Golden Goose&#x2019; entries in this handbook share the same BB-21 daily midline signal, but the trade structure differs materially. Strategy 1 expresses the signal as a defined-width monthly credit spread with a T-7 rollover. Strategy 8 expresses the same signal as a LEAPS short plus a rolling monthly hedge, held until the signal flips. Treat them as alternative implementations of the same underlying signal; pick one per capital allocation to avoid doubling up on the same view."))

new_xml = "\n".join(sections) + "\n"

text = DOC.read_text(encoding="utf-8")
if ANCHOR not in text:
    raise SystemExit("Anchor not found in document.xml")

# Find the anchor's start position.
idx = text.index(ANCHOR)
# Find the start of the page-break <w:p> within the anchor (its location in text = idx + offset of "    <w:p>\n      <w:r>\n        <w:br")
pbreak_marker = """    <w:p>
      <w:r>
        <w:br w:type="page"/>
      </w:r>
    </w:p>
    <w:p>
      <w:pPr>
        <w:pStyle w:val="H1Custom"/>
      </w:pPr>
      <w:r>
        <w:t>Cross-Strategy Operating Notes</w:t>"""

assert pbreak_marker in text, "page-break marker not found"
new_text = text.replace(pbreak_marker, new_xml + pbreak_marker, 1)
DOC.write_text(new_text, encoding="utf-8")
print(f"OK: inserted {len(new_xml)} chars of new strategy content")
