#!/usr/bin/env python3
"""Generate the sharpe-engine strategy + pipeline presentation (.pptx).

Comprehensive walkthrough deck — mandate, factor strategy (with academic
provenance), portfolio construction, covered-call overlay, data pipeline,
execution/risk, backtest validation, operations, roadmap — plus an appendix
of formulas, citations, methodology tables, and a glossary.

Editable by design: change CONTENT/helpers here and re-run to regenerate.

    <scratchpad>/deckvenv/bin/python presentation/build_deck.py

Output: presentation/sharpe-engine.pptx
"""
from __future__ import annotations

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

# --------------------------------------------------------------------------- #
# Design system
# --------------------------------------------------------------------------- #
INK        = RGBColor(0x0E, 0x1B, 0x2A)   # dark navy — dividers / cover
INK_PANEL  = RGBColor(0x16, 0x29, 0x3C)   # raised panel on dark
SLATE      = RGBColor(0x2A, 0x47, 0x60)   # watermark number on dividers
ACCENT     = RGBColor(0x14, 0xB8, 0xA6)   # teal accent
ACCENT_DK  = RGBColor(0x0E, 0x8C, 0x7E)
WHITE      = RGBColor(0xFF, 0xFF, 0xFF)
TEXT       = RGBColor(0x1F, 0x2A, 0x37)   # body on white
MUTED      = RGBColor(0x6B, 0x77, 0x87)   # secondary text
RULE       = RGBColor(0xE3, 0xE8, 0xEE)   # hairlines
PANEL      = RGBColor(0xF4, 0xF7, 0xF9)   # light card
PANEL_BORD = RGBColor(0xDD, 0xE4, 0xEA)
GOLD       = RGBColor(0xC6, 0x8A, 0x2E)   # caution / honesty highlight

FONT = "Arial"

EMU_W, EMU_H = Inches(13.333), Inches(7.5)

prs = Presentation()
prs.slide_width = EMU_W
prs.slide_height = EMU_H

_BLANK = prs.slide_layouts[6]
_state = {"n": 0}


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def _slide():
    return prs.slides.add_slide(_BLANK)


def rect(slide, l, t, w, h, fill=None, line=None, line_w=1.0, shape=MSO_SHAPE.RECTANGLE,
         radius=None):
    sp = slide.shapes.add_shape(shape, l, t, w, h)
    sp.shadow.inherit = False
    if fill is None:
        sp.fill.background()
    else:
        sp.fill.solid()
        sp.fill.fore_color.rgb = fill
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line
        sp.line.width = Pt(line_w)
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        try:
            sp.adjustments[0] = radius
        except Exception:
            pass
    return sp


def bg(slide, color=WHITE):
    rect(slide, 0, 0, EMU_W, EMU_H, fill=color)


def text(slide, l, t, w, h, runs, size=16, color=TEXT, bold=False, align=PP_ALIGN.LEFT,
         anchor=MSO_ANCHOR.TOP, line_spacing=1.05, font=FONT, space_after=0):
    """runs: str OR list of (text, bold) OR list of (text, bold, color)."""
    tb = slide.shapes.add_textbox(l, t, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    p = tf.paragraphs[0]
    p.alignment = align
    p.line_spacing = line_spacing
    if space_after:
        p.space_after = Pt(space_after)
    if isinstance(runs, str):
        runs = [(runs, bold, color)]
    for r in runs:
        if isinstance(r, str):
            rt, rb, rc = r, bold, color
        else:
            rt = r[0]
            rb = r[1] if len(r) > 1 else bold
            rc = r[2] if len(r) > 2 else color
        run = p.add_run()
        run.text = rt
        run.font.name = font
        run.font.size = Pt(size)
        run.font.bold = rb
        run.font.color.rgb = rc
    return tb


def bullets(slide, items, l, t, w, h, size=15, lvl1_size=13, gap=9, line_spacing=1.04):
    """items: list of dicts {lvl:0|1, runs:[(text,bold[,color])], color?, glyph?}."""
    tb = slide.shapes.add_textbox(l, t, w, h)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    first = True
    for it in items:
        lvl = it.get("lvl", 0)
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.line_spacing = line_spacing
        p.space_after = Pt(it.get("gap", gap))
        # glyph
        g = p.add_run()
        if lvl == 0:
            g.text = "▪  "
            g.font.color.rgb = it.get("glyph_color", ACCENT)
            g.font.size = Pt(size)
        else:
            g.text = "        –  "
            g.font.color.rgb = MUTED
            g.font.size = Pt(lvl1_size)
        g.font.name = FONT
        g.font.bold = lvl == 0
        # content runs
        rsize = size if lvl == 0 else lvl1_size
        base_color = it.get("color", TEXT if lvl == 0 else MUTED)
        for r in it["runs"]:
            if isinstance(r, str):
                rt, rb, rc = r, False, base_color
            else:
                rt = r[0]
                rb = r[1] if len(r) > 1 else False
                rc = r[2] if len(r) > 2 else base_color
            run = p.add_run()
            run.text = rt
            run.font.name = FONT
            run.font.size = Pt(rsize)
            run.font.bold = rb
            run.font.color.rgb = rc
    return tb


def footer(slide, label):
    _state["n"] += 1
    text(slide, Inches(0.7), Inches(7.02), Inches(6), Inches(0.3),
         "sharpe-engine   ·   strategy & pipeline", size=9, color=MUTED)
    text(slide, Inches(11.0), Inches(7.02), Inches(1.63), Inches(0.3),
         f"{_state['n']:02d}", size=9, color=MUTED, align=PP_ALIGN.RIGHT)


def content_header(slide, kicker, title, subtitle=None):
    bg(slide, WHITE)
    rect(slide, Inches(0.7), Inches(0.62), Inches(0.34), Inches(0.11), fill=ACCENT)
    text(slide, Inches(1.12), Inches(0.55), Inches(11), Inches(0.3),
         kicker.upper(), size=12, color=ACCENT_DK, bold=True)
    text(slide, Inches(0.7), Inches(0.92), Inches(12), Inches(0.7),
         title, size=27, color=INK, bold=True)
    y = 1.62
    if subtitle:
        text(slide, Inches(0.7), Inches(1.55), Inches(12), Inches(0.4),
             subtitle, size=14, color=MUTED)
        y = 2.0
    rect(slide, Inches(0.7), Inches(y), Inches(11.93), Pt(1.2), fill=RULE)
    return y + 0.18


# --------------------------------------------------------------------------- #
# Slide templates
# --------------------------------------------------------------------------- #
def cover():
    s = _slide()
    bg(s, INK)
    rect(s, 0, 0, Inches(0.22), EMU_H, fill=ACCENT)
    text(s, Inches(0.9), Inches(0.85), Inches(8), Inches(0.4),
         "PROPRIETARY SYSTEMATIC STRATEGY", size=13, color=ACCENT, bold=True)
    text(s, Inches(0.85), Inches(2.0), Inches(11.5), Inches(1.4),
         "sharpe-engine", size=58, color=WHITE, bold=True)
    text(s, Inches(0.9), Inches(3.25), Inches(11), Inches(1.0),
         "A factor-equity portfolio with a covered-call income overlay",
         size=24, color=RGBColor(0xC7, 0xD2, 0xDD))
    rect(s, Inches(0.9), Inches(4.35), Inches(3.0), Pt(2), fill=ACCENT)
    text(s, Inches(0.9), Inches(4.65), Inches(11.5), Inches(1.2),
         [("Uncorrelated, risk-adjusted USD returns · capital preservation first · "
           "paper-traded on Alpaca", False, RGBColor(0x9F, 0xAD, 0xBC))], size=15)
    text(s, Inches(0.9), Inches(6.7), Inches(11.5), Inches(0.5),
         [("Strategy & pipeline walkthrough", True, WHITE),
          ("      ·      Confidential · prepared for firm leadership", False,
           RGBColor(0x8B, 0x99, 0xA8))], size=12)


def divider(num, title, subtitle=None):
    s = _slide()
    bg(s, INK)
    text(s, Inches(0.6), Inches(1.0), Inches(7), Inches(3.2),
         f"{num:02d}", size=200, color=SLATE, bold=True)
    rect(s, Inches(0.95), Inches(4.35), Inches(2.4), Pt(3), fill=ACCENT)
    text(s, Inches(0.9), Inches(4.6), Inches(11.5), Inches(1.2),
         title, size=40, color=WHITE, bold=True)
    if subtitle:
        text(s, Inches(0.95), Inches(5.7), Inches(11), Inches(1.0),
             subtitle, size=16, color=RGBColor(0x9F, 0xAD, 0xBC))


def content(kicker, title, items, subtitle=None, body_top=None, body_w=11.93,
            body_left=0.7, size=15):
    s = _slide()
    y = content_header(s, kicker, title, subtitle)
    bullets(s, items, Inches(body_left), Inches(body_top or y),
            Inches(body_w), Inches(5.0), size=size)
    footer(s, title)
    return s


def two_col(kicker, title, left_head, left_items, right_head, right_items,
            subtitle=None):
    s = _slide()
    y = content_header(s, kicker, title, subtitle)
    top = Inches(y + 0.15)
    colw = Inches(5.75)
    # left
    text(s, Inches(0.7), top, colw, Inches(0.4), left_head, size=15, color=INK, bold=True)
    bullets(s, left_items, Inches(0.7), Inches(y + 0.7), colw, Inches(4.4), size=14)
    # divider
    rect(s, Inches(6.66), Inches(y + 0.1), Pt(1.2), Inches(4.7), fill=RULE)
    # right
    text(s, Inches(6.95), top, colw, Inches(0.4), right_head, size=15, color=INK, bold=True)
    bullets(s, right_items, Inches(6.95), Inches(y + 0.7), colw, Inches(4.4), size=14)
    footer(s, title)
    return s


def table_slide(kicker, title, headers, rows, col_w, subtitle=None, note=None,
                font_size=12, header_size=12, highlight_rows=None, body_top=None):
    s = _slide()
    y = content_header(s, kicker, title, subtitle)
    highlight_rows = highlight_rows or set()
    nrows = len(rows) + 1
    ncols = len(headers)
    total_w = Inches(sum(col_w))
    left = Inches(0.7)
    top = Inches(body_top or (y + 0.2))
    row_h = 0.46
    height = Inches(row_h * nrows)
    gfx = s.shapes.add_table(nrows, ncols, left, top, total_w, height)
    tbl = gfx.table
    tbl.first_row = False
    tbl.horz_banding = False
    for i, cw in enumerate(col_w):
        tbl.columns[i].width = Inches(cw)
    # header
    for c, head in enumerate(headers):
        cell = tbl.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = INK
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        cell.margin_left = Inches(0.08)
        cell.margin_right = Inches(0.06)
        cell.margin_top = Inches(0.02)
        cell.margin_bottom = Inches(0.02)
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT if c == 0 else PP_ALIGN.CENTER
        r = p.add_run(); r.text = head
        r.font.name = FONT; r.font.size = Pt(header_size); r.font.bold = True
        r.font.color.rgb = WHITE
    # body
    for ri, row in enumerate(rows, start=1):
        hl = (ri - 1) in highlight_rows
        for c, val in enumerate(row):
            cell = tbl.cell(ri, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = RGBColor(0xEC, 0xF6, 0xF4) if hl else (
                PANEL if ri % 2 == 0 else WHITE)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.02)
            cell.margin_bottom = Inches(0.02)
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT if c == 0 else PP_ALIGN.CENTER
            r = p.add_run(); r.text = str(val)
            r.font.name = FONT; r.font.size = Pt(font_size)
            r.font.bold = hl or c == 0
            r.font.color.rgb = INK if (hl or c == 0) else TEXT
    if note:
        text(s, left, Inches(top.inches + row_h * nrows + 0.12), total_w, Inches(0.8),
             note, size=11, color=MUTED, line_spacing=1.1)
    footer(s, title)
    return s


def stat_slide(kicker, title, stats, subtitle=None, caption=None):
    """stats: list of (big, label[, color])."""
    s = _slide()
    y = content_header(s, kicker, title, subtitle)
    n = len(stats)
    gap = 0.3
    total = 11.93
    cw = (total - gap * (n - 1)) / n
    top = y + 0.5
    for i, st in enumerate(stats):
        x = 0.7 + i * (cw + gap)
        rect(s, Inches(x), Inches(top), Inches(cw), Inches(2.1),
             fill=PANEL, line=PANEL_BORD, line_w=1, shape=MSO_SHAPE.ROUNDED_RECTANGLE,
             radius=0.06)
        rect(s, Inches(x), Inches(top), Inches(cw), Inches(0.09), fill=ACCENT)
        col = st[2] if len(st) > 2 else INK
        text(s, Inches(x), Inches(top + 0.5), Inches(cw), Inches(0.9),
             st[0], size=40, color=col, bold=True, align=PP_ALIGN.CENTER)
        text(s, Inches(x + 0.15), Inches(top + 1.45), Inches(cw - 0.3), Inches(0.6),
             st[1], size=12.5, color=MUTED, align=PP_ALIGN.CENTER, line_spacing=1.05)
    if caption:
        text(s, Inches(0.7), Inches(top + 2.45), Inches(11.93), Inches(1.0),
             caption, size=12.5, color=MUTED, line_spacing=1.2)
    footer(s, title)
    return s


def callout_slide(kicker, title, quote, attrib=None, subtitle=None, accent=ACCENT):
    s = _slide()
    y = content_header(s, kicker, title, subtitle)
    top = y + 0.6
    rect(s, Inches(0.7), Inches(top), Inches(0.12), Inches(3.0), fill=accent)
    text(s, Inches(1.1), Inches(top + 0.1), Inches(11.3), Inches(3.0),
         quote, size=22, color=INK, line_spacing=1.2)
    if attrib:
        text(s, Inches(1.1), Inches(top + 3.0), Inches(11), Inches(0.5),
             attrib, size=13, color=MUTED)
    footer(s, title)
    return s


# --------------------------------------------------------------------------- #
# Diagram helpers
# --------------------------------------------------------------------------- #
def flow_box(slide, x, y, w, h, label, sub=None, fill=INK, fg=WHITE, sub_fg=None):
    rect(slide, Inches(x), Inches(y), Inches(w), Inches(h), fill=fill,
         shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.12)
    if sub:
        text(slide, Inches(x), Inches(y + h / 2 - 0.42), Inches(w), Inches(0.5),
             label, size=14, color=fg, bold=True, align=PP_ALIGN.CENTER)
        text(slide, Inches(x + 0.06), Inches(y + h / 2 + 0.02), Inches(w - 0.12), Inches(0.5),
             sub, size=9.5, color=sub_fg or RGBColor(0xAE, 0xBC, 0xC9),
             align=PP_ALIGN.CENTER, line_spacing=1.0)
    else:
        text(slide, Inches(x), Inches(y), Inches(w), Inches(h),
             label, size=14, color=fg, bold=True, align=PP_ALIGN.CENTER,
             anchor=MSO_ANCHOR.MIDDLE)


def chevron(slide, x, y):
    text(slide, Inches(x), Inches(y), Inches(0.4), Inches(0.5),
         "▶", size=14, color=ACCENT, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)


# =========================================================================== #
# BUILD
# =========================================================================== #
cover()

# ---- Agenda --------------------------------------------------------------- #
s = _slide()
y = content_header(s, "Contents", "What this walkthrough covers")
agenda = [
    ("01", "The mandate", "What the book is for, and what it deliberately is not"),
    ("02", "Why this approach", "Factor premia over ML; the volatility-risk-premium overlay"),
    ("03", "The factor strategy", "Quality, Value, Momentum, Low-vol — formulas + the literature"),
    ("04", "Portfolio construction", "Scores → weights; the alpha-model / risk-model split"),
    ("05", "The income overlay", "How covered calls are selected, written, and managed"),
    ("06", "The data pipeline", "Sources, stores, and point-in-time integrity"),
    ("07", "Execution, risk & proof", "Order routing, the risk gate, and the backtest"),
    ("08", "Operations", "The live dashboard and deployment"),
    ("09", "Roadmap & risk", "Where we are, the go-live gate, and what's next"),
    ("—", "Appendix", "Formulas, citations, methodology tables, glossary"),
]
top = y + 0.2
for i, (num, t_, d_) in enumerate(agenda):
    row_y = top + i * 0.49
    col = 0.7 if i < 10 else 0.7
    text(s, Inches(0.7), Inches(row_y), Inches(0.6), Inches(0.4),
         num, size=15, color=ACCENT_DK, bold=True)
    text(s, Inches(1.4), Inches(row_y), Inches(3.4), Inches(0.4),
         t_, size=15, color=INK, bold=True)
    text(s, Inches(4.9), Inches(row_y), Inches(7.7), Inches(0.4),
         d_, size=13, color=MUTED)
footer(s, "Contents")

# =========================================================================== #
# § 1 — THE MANDATE
# =========================================================================== #
divider(1, "The mandate", "A proprietary book built to diversify the firm — not to chase alpha")

content("01 · The mandate", "Three objectives, in priority order", [
    {"runs": [("Preserve capital first. ", True), ("The primary mandate. Every design choice "
      "— diversification caps, the income overlay, the risk gate — serves drawdown control "
      "before it serves return.")]},
    {"runs": [("Generate uncorrelated returns. ", True), ("A systematic equity book whose "
      "return stream diversifies the firm's core brokerage business, rather than amplifying it.")]},
    {"runs": [("Be honest and defensible. ", True), ("A clean, explainable strategy with a "
      "credible academic track record — something an investment committee can understand, "
      "audit, and trust through a bad stretch.")]},
    {"runs": [("Risk-adjusted, not headline. ", True), ("The goal is a high Sharpe with shallow "
      "drawdowns — consistent income, not 50% years. Capped upside is an acceptable price.")]},
], subtitle="The book exists to diversify firm revenue with disciplined, risk-adjusted USD returns")

content("01 · The mandate", "What this is — and what it deliberately is not", [
    {"runs": [("IT IS a systematic factor portfolio.", True, ACCENT_DK)], "glyph_color": ACCENT},
    {"lvl": 1, "runs": [("Rules-based exposure to well-documented risk premia, rebalanced monthly.")]},
    {"runs": [("IT IS a volatility-risk-premium harvest.", True, ACCENT_DK)], "glyph_color": ACCENT},
    {"lvl": 1, "runs": [("Covered calls turn held stock into a recurring income stream.")]},
    {"runs": [("IT IS NOT a machine-learning return predictor.", True, GOLD)], "glyph_color": GOLD},
    {"lvl": 1, "runs": [("No black-box forecasting of next-month returns — the most crowded game in quant.")]},
    {"runs": [("IT IS NOT market timing.", True, GOLD)], "glyph_color": GOLD},
    {"lvl": 1, "runs": [("No regime filter, no macro calls — the factors themselves carry the defense.")]},
    {"runs": [("IT IS NOT concentrated stock-picking.", True, GOLD)], "glyph_color": GOLD},
    {"lvl": 1, "runs": [("~20 names, near-equal-weight, hard single-name and sector caps.")]},
], subtitle="Setting expectations up front is what makes the strategy defensible")

# Two-engines diagram
s = _slide()
y = content_header(s, "01 · The mandate", "Two engines, one book",
                   subtitle="A factor portfolio decides what to hold; the overlay earns income on it")
flow_box(s, 0.9, 2.4, 4.6, 1.9, "FACTOR EQUITY PORTFOLIO",
         "Quality · Value · Momentum · Low-vol\n~20 names, monthly rebalance", fill=INK)
flow_box(s, 7.85, 2.4, 4.6, 1.9, "COVERED-CALL OVERLAY",
         "Sell 0.30-delta calls, 30–45 DTE\nHarvest the volatility risk premium", fill=ACCENT_DK)
text(s, Inches(5.5), Inches(2.95), Inches(2.35), Inches(0.8),
     "writes calls\nagainst", size=12, color=MUTED, align=PP_ALIGN.CENTER, line_spacing=1.1)
chevron(s, 5.62, 3.15); chevron(s, 6.9, 3.15)
text(s, Inches(0.9), Inches(4.8), Inches(11.5), Inches(1.6),
     [("Return = ", True), ("factor selection (the holdings) "),
      ("+ ", True), ("premium income (the overlay) "),
      ("− ", True), ("capped upside on assigned names. "),
      ("The equity sleeve sets direction and diversification; the option sleeve trades a little "
       "upside for a steady, uncorrelated income stream and a lower-volatility path.", False, MUTED)],
     size=15, line_spacing=1.3)
footer(s, "Two engines, one book")

# =========================================================================== #
# § 2 — WHY THIS APPROACH
# =========================================================================== #
divider(2, "Why this approach", "The intellectual case for factors and for selling volatility")

content("02 · Why this approach", "Why factor premia, not machine learning", [
    {"runs": [("ML price prediction is the most arbitraged space in finance. ", True),
      ("A model trained on standard momentum/vol features competes with Renaissance, Two Sigma, "
       "and thousands of funds. The half-life of ML price signals keeps compressing as capital crowds in.")]},
    {"runs": [("Factor premia are persistent risk premia, not fleeting mispricings. ", True),
      ("Fifty years of academic evidence across dozens of markets and decades. They pay because "
       "they compensate systematic risk — which makes them durable, not arbitraged away.")]},
    {"runs": [("Defensible to an investment committee. ", True),
      ("A clean factor book is honest about what it is and has a credible track record. A black box "
       "is far harder to defend when it goes through an inevitable bad stretch.")]},
    {"runs": [("Fits the mandate. ", True),
      ("The goal is capital preservation and uncorrelated returns — not alpha generation. "
       "Factor premia + an income overlay is exactly that shape of return.")]},
], subtitle="Decision D1 — explicit, formula-based factor scores over a return-forecasting model")

content("02 · Why this approach", "Why a covered-call income overlay", [
    {"runs": [("The volatility risk premium is real and persistent. ", True),
      ("Option-implied volatility systematically exceeds subsequently-realized volatility. Selling "
       "options harvests that gap — one of the most robust anomalies in options markets.")]},
    {"runs": [("Income on capital already deployed. ", True),
      ("Covered calls earn premium on stock the factor book already holds. No separate capital, "
       "no cash drag — unlike cash-secured puts, which tie up collateral.")]},
    {"runs": [("Lower-volatility path. ", True),
      ("The CBOE BuyWrite (BXM) has historically matched the S&P 500 with roughly 30% lower volatility "
       "— precisely the trade a preservation mandate wants.")]},
    {"runs": [("Capped upside is an acceptable cost. ", True),
      ("The firm does not need 50% years; it needs consistent, uncorrelated income. Giving up the "
       "right tail to buy a smoother ride is the deliberate trade.")]},
], subtitle="Decision D3 — harvest the volatility risk premium, the strategy's primary income source")

two_col("02 · Why this approach", "Covered calls over the alternatives",
        "Why not cash-secured puts / the wheel?",
        [{"runs": [("Puts require large cash collateral — dead weight in a prop book.")]},
         {"runs": [("Calls earn income on stock already deployed in the factor portfolio.")]},
         {"runs": [("The put leg adds cash drag and operational complexity.")]},
         {"runs": [("Cleaner and more controllable for a first version; puts can be layered later.")]}],
        "Why delta-targeted strikes, not fixed % OTM?",
        [{"runs": [("A fixed 7%-OTM strike means wildly different odds on a 15%-vol vs a 40%-vol stock.")]},
         {"runs": [("Targeting a 0.30 delta gives a consistent ~30% assignment probability everywhere.")]},
         {"runs": [("In high-IV names the call sits further OTM in dollars; in low-IV, closer — it adapts.")]},
         {"runs": [("Industry standard for systematic covered-call programs (D4).")]}],
        subtitle="Decisions D3 / D4 — the simplest controllable design that fits the mandate")

# =========================================================================== #
# § 3 — THE FACTOR STRATEGY
# =========================================================================== #
divider(3, "The factor strategy", "Four risk premia, computed transparently, weighted equally")

content("03 · Factor strategy", "Four factors, one composite score", [
    {"runs": [("Every liquid stock gets a composite factor score on each date. ", True),
      ("The composite is the equal-weighted average of four cross-sectionally standardized sub-scores:")]},
    {"lvl": 1, "runs": [("Quality", True), ("  —  profitability: ROE and gross margin")]},
    {"lvl": 1, "runs": [("Value", True), ("  —  cheapness: earnings yield (E/P) and book yield (B/P)")]},
    {"lvl": 1, "runs": [("Momentum", True), ("  —  12-1 trailing price trend")]},
    {"lvl": 1, "runs": [("Low-volatility", True), ("  —  the inverse of trailing realized volatility")]},
    {"runs": [("Equal weight (25% each) to start. ", True),
      ("The most defensible prior when you have no strong view on which factor will dominate the "
       "period — and it avoids overfitting the weights before the backtest (D2).")]},
    {"runs": [("All four are paid for bearing systematic risk — ", True),
      ("not for finding mispricings. That is what makes them durable.")]},
], subtitle="Composite = 0.25·Quality + 0.25·Value + 0.25·Momentum + 0.25·Low-vol")

# Per-factor slides (formula + papers)
def factor_slide(name, intuition, formula, notes, papers):
    s = _slide()
    y = content_header(s, "03 · Factor strategy", name)
    top = y + 0.15
    # intuition
    text(s, Inches(0.7), Inches(top), Inches(11.9), Inches(0.7),
         intuition, size=15, color=TEXT, line_spacing=1.2)
    # formula panel
    fy = top + 0.95
    rect(s, Inches(0.7), Inches(fy), Inches(11.93), Inches(0.95), fill=INK,
         shape=MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.06)
    text(s, Inches(0.95), Inches(fy + 0.12), Inches(2.4), Inches(0.7),
         "AS COMPUTED", size=11, color=ACCENT, bold=True, anchor=MSO_ANCHOR.MIDDLE)
    text(s, Inches(2.8), Inches(fy), Inches(9.6), Inches(0.95),
         formula, size=16, color=WHITE, bold=True, anchor=MSO_ANCHOR.MIDDLE,
         font="Consolas")
    # notes
    bullets(s, notes, Inches(0.7), Inches(fy + 1.2), Inches(11.93), Inches(2.0), size=14)
    # papers
    py = fy + 1.2 + 0.5 * len(notes) + 0.55
    py = min(py, 5.75)
    rect(s, Inches(0.7), Inches(py), Inches(0.1), Inches(6.55 - py), fill=ACCENT)
    text(s, Inches(1.0), Inches(py), Inches(3), Inches(0.3),
         "SEMINAL LITERATURE", size=11, color=ACCENT_DK, bold=True)
    bullets(s, [{"lvl": 1, "runs": [(p,)], "gap": 4} for p in papers],
            Inches(0.95), Inches(py + 0.32), Inches(11.6), Inches(1.4),
            size=12, lvl1_size=12)
    footer(s, name)
    return s

factor_slide(
    "Value — buy cheap cash flows",
    "Cheap stocks (high earnings/book yield) have historically out-earned expensive ones — "
    "compensation for distress risk and behavioral over-extrapolation.",
    "Value = z ( z(E/P) + z(B/P) ),  where  E/P = 1/PE,  B/P = 1/PB",
    [{"runs": [("Yields, not ratio inversion. ", True), ("~24% of the universe has negative P/E; "
       "inverting (−P/E) would score loss-makers as 'cheap'. E/P and B/P sort negatives to the "
       "bottom where they belong, and keep the name scored.")]},
     {"runs": [("Double-z. ", True), ("The two metric z-scores are summed then re-standardized, so "
       "Value lands at unit variance like the price factors — true equal weighting.")]}],
    ["Fama & French (1992), “The Cross-Section of Expected Stock Returns,” Journal of Finance",
     "Fama & French (1993), “Common Risk Factors in the Returns on Stocks and Bonds,” JFE",
     "Basu (1977), “Investment Performance of Common Stocks in Relation to P/E Ratios,” J. of Finance"])

factor_slide(
    "Momentum — ride the trend",
    "Stocks that outperformed over the past year tend to keep outperforming over the next months "
    "— the most-replicated anomaly in asset pricing.",
    "Momentum = z ( close[t−21] / close[t−252] − 1 )",
    [{"runs": [("12-1 construction. ", True), ("Trailing 12-month return, skipping the most recent "
       "month (the 1-month reversal window) — the canonical academic definition.")]},
     {"runs": [("Recomputed daily. ", True), ("A pure price derivative, refreshed from each day's "
       "close — the fastest-moving of the four sleeves (D19).")]}],
    ["Jegadeesh & Titman (1993), “Returns to Buying Winners and Selling Losers,” J. of Finance",
     "Carhart (1997), “On Persistence in Mutual Fund Performance,” Journal of Finance",
     "Asness, Moskowitz & Pedersen (2013), “Value and Momentum Everywhere,” J. of Finance"])

factor_slide(
    "Quality — own profitable, durable businesses",
    "Profitable, high-margin firms have delivered higher risk-adjusted returns — the market "
    "underprices the persistence of profitability.",
    "Quality = z ( z(ROE) + z(gross margin) )",
    [{"runs": [("Profitability core. ", True), ("Return on equity and gross margin — Novy-Marx's "
       "result that gross profitability is the 'other side of value'.")]},
     {"runs": [("Defensive by nature. ", True), ("Quality names hold up in stress, so the optimizer "
       "tilts defensive automatically when they score well — no separate regime filter needed (D5).")]}],
    ["Novy-Marx (2013), “The Other Side of Value: The Gross Profitability Premium,” JFE",
     "Asness, Frazzini & Pedersen (2019), “Quality Minus Junk,” Review of Accounting Studies",
     "Piotroski (2000), “Value Investing: … Winners from Losers …,” J. of Accounting Research"])

factor_slide(
    "Low-volatility — the low-risk anomaly",
    "Low-volatility stocks have historically delivered higher risk-adjusted returns than high-vol "
    "ones — the opposite of what CAPM predicts (leverage-constrained investors over-pay for risk).",
    "Low-vol = z ( − realized vol ),   σ of daily returns over the trailing 252 days",
    [{"runs": [("Negated, then z-scored. ", True), ("Lower vol → higher score. Scale-invariant, "
       "so it needs no annualization for the cross-section.")]},
     {"runs": [("Carries the defense. ", True), ("Together with Quality, this is why the book needs "
       "no explicit risk-off switch — it tilts defensive endogenously.")]}],
    ["Ang, Hodrick, Xing & Zhang (2006), “The Cross-Section of Volatility …,” J. of Finance",
     "Frazzini & Pedersen (2014), “Betting Against Beta,” Journal of Financial Economics",
     "Baker, Bradley & Wurgler (2011), “Benchmarks as Limits to Arbitrage,” Financial Analysts J."])

content("03 · Factor strategy", "The composite — how the four become one score", [
    {"runs": [("Cross-sectional z-scoring. ", True), ("Each metric is standardized across the "
      "universe on the date, so 'good' means good relative to peers — not an absolute threshold.")]},
    {"runs": [("Winsorize before standardizing (1% / 99%). ", True), ("Raw metrics are clipped to "
      "their 1st–99th percentiles first, so a stock up 5000% or a tiny-denominator E/P can't inflate "
      "σ and crush every other name toward zero. Each sub-score comes out at ~unit variance.")]},
    {"runs": [("Unit variance is what makes equal-weight honest. ", True), ("Because all four "
      "sub-scores share the same scale, 25% each really is 25% — no factor silently dominates.")]},
    {"runs": [("Missing inputs are neutral-filled (z = 0). ", True), ("A name missing one metric is "
      "scored on the rest at the average, rather than dropped — and flagged 'stale' for transparency.")]},
], subtitle="Robust standardization is the difference between true and nominal equal weighting")

content("03 · Factor strategy", "Point-in-time discipline — no looking ahead", [
    {"runs": [("Fundamentals are usable only once they were public. ", True), ("A quarter enters the "
      "score only when its real SEC filing date is on or before the as-of date — never the quarter-end.")]},
    {"runs": [("This is the line between a real backtest and a fantasy. ", True), ("Using restated or "
      "not-yet-filed fundamentals would make historical Value/Quality scores artificially prescient.")]},
    {"runs": [("Sourced from SEC EDGAR XBRL. ", True), ("Each fact carries its filing date, so "
      "point-in-time correctness is structural — not a heuristic lag we bolted on (D22).")]},
    {"runs": [("Price factors refresh daily; fundamentals on each new filing. ", True), ("Momentum "
      "and vol move with prices; ROE and margins don't change between earnings, so they update only "
      "when a filing appears (D19).")]},
], subtitle="The single most important guard against an over-optimistic backtest")

# =========================================================================== #
# § 4 — PORTFOLIO CONSTRUCTION
# =========================================================================== #
divider(4, "Portfolio construction", "From scores to weights — the alpha model selects, the constraints diversify")

content("04 · Construction", "Scores → expected returns → an optimization", [
    {"runs": [("Step 1 — rank to a return proxy. ", True), ("The composite score becomes a stand-in "
      "expected return:  μ = rank(composite)/N × scale.  Best-scoring names get the highest μ (D20).")]},
    {"runs": [("Step 2 — pre-select the top 50 by score. ", True), ("Keeps the optimization small "
      "and focused on the names the signal actually likes.")]},
    {"runs": [("Step 3 — solve a constrained optimization for weights:", True)]},
    {"lvl": 1, "runs": [("maximize  μᵀw  −  λ·wᵀΣw", True, INK)]},
    {"lvl": 1, "runs": [("subject to:  Σw = 95% (rest cash) · 0 ≤ w ≤ 5% · sector ≤ 30% · min position $4k")]},
    {"runs": [("Step 4 — enforce the minimum position. ", True), ("Drop the single smallest "
      "sub-floor name, re-solve, repeat — which settles the book into the ~20-name interior.")]},
    {"runs": [("Step 5 — relax gracefully if infeasible. ", True), ("A ladder widens the sector cap "
      "and lowers the min position before ever holding stale weights (and alerting).")]},
], subtitle="A convex max-Sharpe optimizer with capital-preservation constraints")

content("04 · Construction", "The key decision: λ = 0 (alpha-driven weighting)", [
    {"runs": [("We turned the risk-aversion term off. ", True), ("The objective becomes the pure "
      "linear program  max μᵀw  — the covariance penalty λ·wᵀΣw is set to zero (D25).")]},
    {"runs": [("Why: the composite already prices risk. ", True), ("Low-volatility is one of its four "
      "sub-scores. Adding a mean-variance penalty on top tilts toward low-vol a second time — "
      "double-counting the defense and costing ~10%/yr in backtest.")]},
    {"runs": [("This is the institutional alpha-model / risk-model split. ", True), ("The alpha model "
      "(composite) selects names; the constraints (box, sector, min) diversify; the FF5 covariance "
      "is kept for risk reporting and a thin-history filter — not for weighting.")]},
    {"runs": [("Sound, not reckless. ", True), ("λ = 0 would normally concentrate in one name — but "
      "the 5% cap, 4% floor, and 30% sector cap force diversification regardless. The result is "
      "'top ~20 by score, equal-weight at the cap, sector-limited'.")]},
], subtitle="A counterintuitive choice the backtest forced — and it more than doubled the Sharpe")

table_slide("04 · Construction", "Why λ = 0 — the decomposition that settled it",
            ["Optimizer configuration", "Gross ann. return"],
            [["λ = 1, sector cap 30%  (original mean-variance)", "+2.3%"],
             ["λ ≈ 0, sector cap 30%", "+12.8%"],
             ["λ = 1, no sector cap", "+2.4%"],
             ["λ ≈ 0, no sector cap", "+12.2%"],
             ["Naive top-19 equal-weight by composite", "+13.7%"]],
            [9.0, 2.93], highlight_rows={1},
            subtitle="2021–2026 walk-forward · same scores, same universe",
            note="The risk term — not the sector caps — was suppressing return. Removing it lifted net "
                 "return to +14.0%/yr and Sharpe from 0.15 to 0.75. The risk model still monitors; it just "
                 "no longer drives the weights. (DECISIONS D25)")

content("04 · Construction", "Constraints are the risk controls", [
    {"runs": [("5% maximum single name. ", True), ("The real lever on concentration. It — not λ "
      "— sets the name count: ~budget / max-name ≈ 19–20 names. At 10% the top five names were "
      "half the book; 5% is the capital-preservation choice (D24).")]},
    {"runs": [("4% minimum funded position. ", True), ("No tiny, unmanageable slivers; also the "
      "threshold below which a position can't support even one option contract.")]},
    {"runs": [("30% sector cap. ", True), ("No single sector can dominate — binds in practice "
      "(e.g. Financials) and forces breadth.")]},
    {"runs": [("95% invested, 5% cash buffer. ", True), ("Stable base allocation at all times; no "
      "regime-driven de-risking (the factors handle defense).")]},
    {"runs": [("FF5 covariance, retained for risk. ", True), ("Σ = B·cov(F)·Bᵀ + D from the "
      "Fama-French 5-factor model — well-conditioned for any universe size; powers the risk panel "
      "and the thin-history filter (D8 / D23a).")]},
], subtitle="Diversification is enforced structurally, not hoped for")

# =========================================================================== #
# § 5 — THE INCOME OVERLAY
# =========================================================================== #
divider(5, "The income overlay", "How covered calls are chosen, written, and managed")

content("05 · Income overlay", "Selecting the strike — the load-bearing logic", [
    {"runs": [("Alpaca's chains give strikes and bid/ask, but no Greeks. ", True), ("So the engine "
      "computes delta itself, using Black-Scholes — the indicative feed has no delta to read.")]},
    {"runs": [("1 · Estimate implied volatility. ", True), ("IV ≈ annualized trailing realized "
      "volatility — the conservative, data-available basis.")]},
    {"runs": [("2 · Price delta for every strike in the 30–45 DTE window. ", True), ("Black-Scholes "
      "call delta from spot and the IV estimate.")]},
    {"runs": [("3 · Pick the strike nearest 0.30 delta. ", True), ("≈ 30% probability of "
      "assignment — enough premium, but lets most winners run.")]},
    {"runs": [("4 · Sell-to-open a limit at the chain mid. ", True), ("Real premium from the real "
      "chain, not a modeled number. One standard contract per 100 shares held.")]},
], subtitle="Compute delta from the chain → target 0.30 → write at the mid")

content("05 · Income overlay", "Why 0.30 delta — the sweep", [
    {"runs": [("Delta only pays when implied vol exceeds realized. ", True), ("At the realized-vol "
      "floor (zero premium) Sharpe is flat across delta — selling vol is the whole point.")]},
    {"runs": [("At market-implied IV, Sharpe climbs with delta … ", True), ("0.25 → 1.18,  "
      "0.30 → 1.31,  0.35 → 1.43,  0.40 → 1.52  — more premium harvested.")]},
    {"runs": [("… but assignment crosses the 30% cap near 0.35. ", True), ("0.35 → 30% assigned, "
      "0.40 → 36%. Too much of the book gets called away.")]},
    {"runs": [("0.30 is the balanced step up (D29). ", True), ("Market Sharpe 1.31, assignment a "
      "comfortable 22%, ~26%/yr premium — raised from 0.25, which left premium on the table.")]},
], subtitle="Decision D29 — 0.30 delta balances premium income against names called away")

content("05 · Income overlay", "Lifecycle — one monthly cadence, plus safety actions", [
    {"runs": [("Monthly close-all, then rewrite fresh. ", True), ("At each rebalance every open call "
      "is bought to close before equity trades; new calls are written against the new book. Always "
      "aligned to current positions, always simple (D15 / D31).")]},
    {"runs": [("Close before earnings, rewrite after. ", True), ("The one event-driven exception: "
      "earnings gaps make calls binary, so any call facing an announcement in its life is closed "
      "first (D16).")]},
    {"runs": [("Force-close at expiry. ", True), ("No call is left to expire uncontrolled.")]},
    {"runs": [("Conditional re-entry on assignment. ", True), ("If a name is called away, re-buy it "
      "only if its composite score still clears the threshold — don't chase a stock that just ran "
      "past your strike (D9).")]},
    {"runs": [("No mid-cycle roll. ", True), ("30–45 DTE covers the ~1-month hold; this matches "
      "exactly what the backtest modeled — nothing runs live that wasn't measured (D31).")]},
], subtitle="The live design is identical to the backtested model — by construction")

content("05 · Income overlay", "An honest limit: coverage and leverage", [
    {"runs": [("Standard 100-share contracts only. ", True), ("Single-name 10-share 'mini' options "
      "were delisted ~2014. A position must hold ≥ 100 shares to be coverable — there is no "
      "fractional-contract path (D32).")]},
    {"runs": [("At ~$10k per name, only part of the book clears that bar. ", True), ("On $100+ stocks "
      "a $10k position is under 100 shares — so the high-priced tail stays uncovered.")]},
    {"runs": [("This is why the paper book runs at 2:1 gross leverage. ", True), ("Doubling the "
      "deployable base lifts coverage from ~6 to ~10 of ~19 names — enough to actually exercise "
      "the overlay on the paper account (D32).")]},
    {"runs": [("Stated plainly: 2:1 scales the path. ", True, GOLD), ("Leverage doesn't change Sharpe "
      "but roughly doubles drawdowns. It was deferred to a later phase; on a reversible paper book it "
      "makes the overlay testable. The leverage level is revisited before any real capital.",
      False, GOLD)], "glyph_color": GOLD},
], subtitle="Decision D32 — the coverage constraint, and the trade-off taken to work around it")

# =========================================================================== #
# § 6 — THE DATA PIPELINE
# =========================================================================== #
divider(6, "The data pipeline", "A daily, idempotent flow from raw data to live orders")

# Pipeline DAG diagram
s = _slide()
y = content_header(s, "06 · Data pipeline", "The daily pipeline, end to end",
                   subtitle="Idempotent and safe to re-run mid-rebalance — reconciled against the broker every startup")
stages = [("INGEST", "prices +\nfundamentals"), ("FACTORS", "composite\nscores"),
          ("OPTIMIZE", "target\nweights"), ("OVERLAY", "covered-call\nplan"),
          ("RISK", "pre-trade\ngate"), ("EXECUTE", "orders →\nAlpaca")]
bx, by, bw, bh = 0.7, 2.5, 1.78, 1.25
gap = 0.27
for i, (lab, sub) in enumerate(stages):
    x = bx + i * (bw + gap)
    fill = ACCENT_DK if lab in ("OVERLAY",) else INK
    flow_box(s, x, by, bw, bh, lab, sub, fill=fill)
    if i < len(stages) - 1:
        chevron(s, x + bw + 0.01, by + bh / 2 - 0.25)
# store row
sy = 4.55
rect(s, Inches(3.1), Inches(by + bh + 0.25), Pt(1.4), Inches(0.45), fill=RULE)
flow_box(s, 0.7, sy, 5.7, 1.0, "PostgreSQL",
         "operational state: orders · fills · snapshots · lifecycle · audit", fill=PANEL,
         fg=INK, sub_fg=MUTED)
flow_box(s, 6.93, sy, 5.7, 1.0, "Live dashboard",
         "NAV · holdings · factor tilt · risk · execution · backtest", fill=PANEL,
         fg=INK, sub_fg=MUTED)
text(s, Inches(0.7), Inches(5.95), Inches(11.93), Inches(0.9),
     [("Research and live share the same code. ", True),
      ("Backfill, daily ingest, and the backtest reuse the identical factor, optimizer, and "
       "execution logic — so what is validated is exactly what runs.", False, MUTED)],
     size=14, line_spacing=1.2)
footer(s, "The daily pipeline")

content("06 · Data pipeline", "Three data sources, three clear jobs", [
    {"runs": [("Alpaca — market data, options & execution. ", True), ("Daily OHLCV "
      "(adjustment='all' for corporate actions), option chains for the overlay, and order routing. "
      "The broker is the source of truth.")]},
    {"runs": [("SEC EDGAR — fundamentals. ", True), ("The XBRL companyfacts API: free, "
      "authoritative, and genuinely point-in-time (each fact carries its filing date). US-GAAP filers "
      "back to ~2009; this is what feeds Quality and Value (D22).")]},
    {"runs": [("Ken French data library — factor returns. ", True), ("The daily Fama-French "
      "5-factor series, downloaded directly, to build the covariance matrix (D23a).")]},
    {"runs": [("Stored in two layers. ", True), ("Parquet for the raw research store (prices + "
      "fundamentals); PostgreSQL for operational state (orders, fills, snapshots, audit).")]},
], subtitle="A deliberately small, free, auditable data stack")

content("06 · Data pipeline", "Data integrity — the unglamorous half of the edge", [
    {"runs": [("Corporate actions. ", True), ("All price pulls use adjustment='all' — splits and "
      "dividends never masquerade as returns.")]},
    {"runs": [("Survivorship & point-in-time. ", True), ("Liquidity filter (ADV > $1M, price > $5) "
      "applied on each historical date, and fundamentals gated by real filing dates — the backtest "
      "doesn't get to know the future.")]},
    {"runs": [("History floor is honest. ", True), ("The free Alpaca IEX feed serves consolidated "
      "history from ~mid-2020. The paid SIP feed (full 2016+, consolidated tape) is a documented "
      "one-line upgrade — not a hidden dependency (D21).")]},
    {"runs": [("Reconciled every startup. ", True), ("PostgreSQL is checked against live Alpaca "
      "positions before anything trades; the broker always wins; the pipeline blocks if Alpaca is "
      "unreachable (D13).")]},
], subtitle="Quietly the most important slides for anyone who has been burned by bad data")

callout_slide("06 · Data pipeline", "War story: when bad data hijacked the book",
    "“The dashboard showed the book had quietly rotated 65% into foreign ADRs. The cause wasn't "
    "the model — it was the data: ADR fundamentals from the yfinance fallback had P/E ratios near "
    "0.01, so earnings-yield = 1/PE produced absurd Value scores and the optimizer piled in.”",
    attrib="Fix (D28): restrict the tradable universe to SEC-EDGAR US-GAAP filers. Returns fell to an "
           "honest +10.9%/yr, but all four factor sleeves turned healthily positive and the held names "
           "now have liquid options for the overlay. Lower, and far more defensible.",
    subtitle="Why the universe is EDGAR-only — a real incident, caught by the dashboard")

# =========================================================================== #
# § 7 — EXECUTION, RISK & PROOF
# =========================================================================== #
divider(7, "Execution, risk & proof", "Routing orders safely — and the backtest that validates the whole thing")

content("07 · Execution & risk", "The execution layer", [
    {"runs": [("Reconcile first. ", True), ("Every run starts by squaring the database to live "
      "Alpaca positions — self-correcting drift, halting only if the broker is unreachable.")]},
    {"runs": [("Smart order routing. ", True), ("Market orders for deep, tight names; otherwise a "
      "marketable limit that crosses the spread by up to 50 bps so it actually fills in-session, "
      "rather than resting passively at the mid and never filling (D35).")]},
    {"runs": [("Fill, then defer. ", True), ("Orders poll for ~60s, then cancel; anything unfilled "
      "rolls into pending adjustments and is completed by the monthly catch-up — never left dangling.")]},
    {"runs": [("Slippage measured against arrival mid. ", True), ("Every fill is priced versus the "
      "quote at submission, surfaced on the dashboard's execution panel.")]},
    {"runs": [("Idempotent and crash-safe. ", True), ("Client-order-IDs make re-runs safe; transient "
      "broker read errors are caught so a hiccup can't leave working orders uncancelled.")]},
], subtitle="Reliable in-session fills also give the overlay real shares to write against")

content("07 · Execution & risk", "The pre-trade risk gate & guardrails", [
    {"runs": [("Leverage cap. ", True), ("Gross exposure is hard-capped; a target above the cap is "
      "blocked before any order is sent.")]},
    {"runs": [("Covered-call coverage gate. ", True), ("Re-derives share coverage on the option plan "
      "— a naked or expired call can never reach the broker, even on an upstream bug.")]},
    {"runs": [("Complete-the-book catch-up. ", True), ("A missed or partial rebalance is finished on "
      "later trading days until the book holds ≥ 80% of target names — then it stops (never twice "
      "a month) (D35).")]},
    {"runs": [("Emergency killswitch. ", True), ("One command halts the engine and cancels orders, "
      "or liquidates the entire book to cash.")]},
    {"runs": [("Everything is logged. ", True), ("Every factor score, order, fill, and drift value "
      "— structured JSON, retained, auditable.")]},
], subtitle="For an investment committee, this is the section that matters most")

content("07 · Execution & risk", "How the strategy was validated", [
    {"runs": [("Walk-forward backtest. ", True), ("Monthly rebalances over 2021-09 → 2026-06 "
      "(~58 months), scoring and optimizing on point-in-time data exactly as the live engine does.")]},
    {"runs": [("Realistic transaction costs. ", True), ("A half-spread charged in basis points, "
      "tiered by liquidity (5 / 10 / 20 bps by ADV) — not a frictionless fantasy (D23d).")]},
    {"runs": [("Real option premiums where available. ", True), ("Covered-call premiums use real "
      "historical chains (DoltHub) with real strikes and implied vol; gaps fall back to a "
      "Black-Scholes estimate, and genuinely non-optionable names are left unhedged (D33).")]},
    {"runs": [("Engine validated against the real index. ", True), ("Run on SPY priced off actual "
      "VIX, the option machinery reproduces CBOE's ^BXM BuyWrite index (model +9.8%/yr vs +7.9%, "
      "monthly correlation 0.79).")]},
], subtitle="Methodology rigor is the defense against “you just overfit it”")

table_slide("07 · Execution & risk", "Backtest results — 2021–2026 walk-forward",
            ["Configuration", "Ann. return", "Volatility", "Sharpe", "Max DD", "Premium/yr"],
            [["Equity only (clean EDGAR universe)", "+10.9%", "~20%", "0.62", "−26%", "—"],
             ["+ covered-call overlay (real premiums)", "+19.6%", "15.5%", "1.24", "−16.7%", "24.8%"],
             ["+ overlay (modeled market-IV)", "~+19%", "~16%", "1.31", "~−17%", "26.3%"],
             ["+ put-wheel blend (optional, 1×)", "+18.7%", "12.9%", "1.40", "−12.8%", "—"],
             ["Benchmark: SPY (buy & hold)", "+11.1%", "~20%", "0.76", "—", "—"]],
            [4.6, 1.55, 1.5, 1.15, 1.35, 1.78], highlight_rows={1},
            font_size=11.5, header_size=10.5,
            subtitle="The overlay is the story: comparable return, far lower volatility and drawdown",
            note="1× (unlevered) basis. The overlay improves volatility, drawdown and income unconditionally; "
                 "the Sharpe lift depends on premium richness, which the live paper book will confirm. "
                 "Sources: DECISIONS D25 / D27 / D28 / D33 / D34.")

stat_slide("07 · Execution & risk", "The volatility risk premium, measured",
           [("+5.2 pts", "Implied vol sold (38.4%)\nover realized (33.2%)"),
            ("78%", "of months with\nimplied > realized vol"),
            ("1.16×", "Average implied / realized\nvolatility ratio"),
            ("22%", "Assignment rate\n(under the 30% cap)")],
           subtitle="The premium the overlay harvests is real and persistent (D33)",
           caption="Caveat worth stating: the variance term (IV² − RV²) is slightly negative — the premium "
                   "is tail-dominated, so a few large single-name moves swamp the steady carry. The 0.30-delta "
                   "strike caps exactly that tail, which is why the overlay still beats equity-only.")

content("07 · Execution & risk", "What we are honest about", [
    {"runs": [("Premium income is partly modeled. ", True), ("No free historical single-name implied "
      "vol exists, so where real chains are missing, premium is Black-Scholes-estimated and "
      "sensitivity-tested. The upside given up is always exact; only the income is modeled (D27).")]},
    {"runs": [("Fundamentals carry mild restatement look-ahead. ", True), ("EDGAR is point-in-time, "
      "but a small effect remains — discount the historical Quality/Value edge modestly.")]},
    {"runs": [("2:1 leverage scales drawdown ~linearly. ", True), ("The unlevered −26% / −18.5% "
      "drawdowns become roughly −50% / −37% at 2×. The backtest itself is 1×.")]},
    {"runs": [("The variance premium is tail-dominated. ", True), ("The edge comes from the strike "
      "capping the tail, not from rich variance compensation — the same nuance on both call and put sides.")]},
    {"runs": [("The real verdict is live. ", True), ("These are the questions paper-trading on real "
      "chains is designed to answer — not something more historical data would settle.")]},
], subtitle="Naming the weaknesses is what makes the strengths credible")

# =========================================================================== #
# § 8 — OPERATIONS
# =========================================================================== #
divider(8, "Operations", "A live, monitored system — not a notebook")

content("08 · Operations", "The live dashboard", [
    {"runs": [("Overview. ", True), ("NAV and cash exact to the cent, day P&L, a leverage gauge, "
      "premium collected, and a global health bar (engine heartbeat, market clock, next rebalance, "
      "risk gate, drift, alerts).")]},
    {"runs": [("Portfolio. ", True), ("Holdings vs target with drift, a nested sector/ticker donut "
      "with concentration metrics, the book's factor tilt vs the universe, covered calls, and recent "
      "activity.")]},
    {"runs": [("Performance. ", True), ("Growth vs SPY / BuyWrite benchmarks, a Risk sub-tab "
      "(drawdown, volatility, 1-day 95% VaR, leverage), and an Execution sub-tab with slippage across "
      "all fills plus regulatory fees.")]},
    {"runs": [("Backtest. ", True), ("The full walk-forward and covered-call / variance-risk-premium "
      "analytics, including the optional put-wheel.")]},
    {"runs": [("Self-updating. ", True), ("An in-process monitor reconciles Alpaca → Postgres so the "
      "page reflects live state, not a stale snapshot.")]},
], subtitle="FastAPI + a single-page dashboard in the firm's SFI design language")

content("08 · Operations", "Deployment & infrastructure", [
    {"runs": [("Runs on a GCP VM (e2-standard-4, 16 GB). ", True), ("Sized after a full-universe "
      "rebalance OOM'd the original small instance; compute now finishes in ~19s with headroom (D36).")]},
    {"runs": [("systemd services. ", True), ("The EOD engine, the dashboard, the reverse proxy, "
      "nightly backups, and a watchdog — all managed and restart-safe.")]},
    {"runs": [("Nightly Postgres backups to cloud storage. ", True), ("pg_dump → GCS with a 30-day "
      "retention lifecycle.")]},
    {"runs": [("Email alerts. ", True), ("The six alert types (engine down, risk-gate block, "
      "reconciliation mismatch, data staleness, etc.) email live and record to the dashboard.")]},
    {"runs": [("Shared securely. ", True), ("Read-only public access behind a password (Tailscale "
      "Funnel + reverse-proxy basic auth). ~$98/mo all-in.")]},
], subtitle="Boring, reliable plumbing — exactly what you want operating a book")

# =========================================================================== #
# § 9 — ROADMAP & RISK
# =========================================================================== #
divider(9, "Roadmap & risk", "Where the build stands, and what stands between here and live capital")

content("09 · Roadmap & risk", "Where we are", [
    {"runs": [("Phases 0–2 — complete & gate-passed. ", True), ("Data foundation, factor model, "
      "optimizer, and the covered-call overlay all validated in backtest.")]},
    {"runs": [("Phase 3 — execution engine & risk gate. ", True), ("Code-complete and tested.")]},
    {"runs": [("Phase 4 — covered-call overlay. ", True), ("Strike selection, broker option orders, "
      "write/close, earnings handling, expiry, assignment re-entry — code-complete and tested.")]},
    {"runs": [("Phase 5 — alerting & live dashboard. ", True), ("Live email alerts and the full "
      "monitoring dashboard — deployed and running.")]},
    {"runs": [("The remaining gate: live-paper verification. ", True, ACCENT_DK), ("Confirm the full "
      "strategy fills, prices, and behaves on the real paper account — the first real rebalance is "
      "the milestone in front of us.", False, INK)], "glyph_color": ACCENT},
], subtitle="Code-complete and paper-trading; one verification gate before the go-live discussion")

two_col("09 · Roadmap & risk", "Key risks & what's next",
        "Risks we watch",
        [{"runs": [("Factor crowding / decay", True)]},
         {"lvl": 1, "runs": [("monitored via live factor-sleeve attribution")]},
         {"runs": [("Overlay drag in strong bull markets", True)]},
         {"lvl": 1, "runs": [("capped upside is the deliberate trade")]},
         {"runs": [("Leverage", True)]},
         {"lvl": 1, "runs": [("2:1 scales drawdown; revisited before live")]},
         {"runs": [("Data-source reliability", True)]},
         {"lvl": 1, "runs": [("EDGAR-only universe; reconcile every run")]},
         {"runs": [("Modeled-premium uncertainty", True)]},
         {"lvl": 1, "runs": [("the live paper book is the resolution")]}],
        "Future work",
        [{"runs": [("Factor-weight tuning", True)]},
         {"lvl": 1, "runs": [("refine off live + extended backtest")]},
         {"runs": [("Factor-specific deltas", True)]},
         {"lvl": 1, "runs": [("wider strikes on momentum names")]},
         {"runs": [("The cash-secured put wheel", True)]},
         {"lvl": 1, "runs": [("built & backtested; wiring deferred (D34)")]},
         {"runs": [("Crypto sleeve (3–7%)", True)]},
         {"lvl": 1, "runs": [("after the equity book is validated")]},
         {"runs": [("SIP data upgrade", True)]},
         {"lvl": 1, "runs": [("full 2016+ history, consolidated tape")]}])

# ---- Summary -------------------------------------------------------------- #
s = _slide()
bg(s, INK)
rect(s, 0, 0, Inches(0.22), EMU_H, fill=ACCENT)
text(s, Inches(0.9), Inches(0.8), Inches(11), Inches(0.4),
     "THE THESIS IN ONE SLIDE", size=13, color=ACCENT, bold=True)
summary = [
    ("A disciplined factor portfolio", "Quality, Value, Momentum, Low-vol — 50 years of academic "
     "evidence, computed transparently, weighted equally."),
    ("An income overlay that lowers risk", "Covered calls harvest the volatility risk premium, "
     "cutting volatility and drawdown while paying ~25%/yr in premium."),
    ("Built for preservation", "Hard diversification caps, a pre-trade risk gate, reconciliation, "
     "and a killswitch — capital preservation is structural, not aspirational."),
    ("Validated, and honest about its limits", "Sharpe ~1.2–1.4 with shallow drawdowns in a "
     "rigorous walk-forward — with the modeling caveats stated plainly."),
    ("Live and monitored", "Running on a paper account with a full dashboard; one verification "
     "gate from the go-live conversation."),
]
for i, (h, d) in enumerate(summary):
    yy = 1.55 + i * 1.02
    rect(s, Inches(0.9), Inches(yy + 0.05), Inches(0.13), Inches(0.7), fill=ACCENT)
    text(s, Inches(1.25), Inches(yy), Inches(11.2), Inches(0.4),
         h, size=18, color=WHITE, bold=True)
    text(s, Inches(1.25), Inches(yy + 0.42), Inches(11.2), Inches(0.55),
         d, size=13, color=RGBColor(0xA9, 0xB7, 0xC4), line_spacing=1.05)
footer(s, "The thesis")

# ---- Closing -------------------------------------------------------------- #
s = _slide()
bg(s, INK)
text(s, Inches(0.9), Inches(2.7), Inches(11.5), Inches(1.2),
     "Discussion", size=46, color=WHITE, bold=True)
rect(s, Inches(0.95), Inches(3.85), Inches(2.4), Pt(3), fill=ACCENT)
text(s, Inches(0.95), Inches(4.15), Inches(11), Inches(1.0),
     [("sharpe-engine  ·  systematic factor equity + covered-call overlay", False,
       RGBColor(0xA9, 0xB7, 0xC4))], size=16)
text(s, Inches(0.95), Inches(5.0), Inches(11), Inches(0.5),
     [("Appendix follows: ", True, ACCENT),
      ("formulas · citations · methodology tables · parameters · glossary",
       False, RGBColor(0x8B, 0x99, 0xA8))], size=13)

# =========================================================================== #
# APPENDIX
# =========================================================================== #
divider(10, "Appendix", "Formulas, citations, methodology, parameters, and a glossary")
# overwrite the divider number label "10" look — fine as-is

table_slide("Appendix · A1", "Factor formulas — full reference",
            ["Factor", "Formula (as implemented)", "Inputs"],
            [["Quality", "z( z(ROE) + z(gross margin) )", "EDGAR fundamentals"],
             ["Value", "z( z(E/P) + z(B/P) ),  E/P=1/PE, B/P=1/PB", "EDGAR + price"],
             ["Momentum", "z( close[t−21] / close[t−252] − 1 )", "daily closes"],
             ["Low-vol", "z( −σ of daily returns, trailing 252d )", "daily closes"],
             ["Composite", "0.25·Q + 0.25·V + 0.25·M + 0.25·L", "the four sub-scores"]],
            [1.8, 7.2, 2.93], font_size=12,
            subtitle="z(·) = cross-sectional z-score with 1%/99% winsorization; missing → neutral (0)",
            note="“Double-z”: Quality and Value sum their two metric z-scores, then re-standardize, so all "
                 "four sub-scores share unit variance before the equal-weight average (true equal weighting).")

# Citations
s = _slide()
y = content_header(s, "Appendix · A2", "Selected literature")
cites = [
    ("Value", ["Fama & French (1992), The Cross-Section of Expected Stock Returns, J. of Finance",
               "Fama & French (1993), Common Risk Factors in Stocks and Bonds, JFE",
               "Basu (1977), Investment Performance … P/E Ratios, J. of Finance"]),
    ("Momentum", ["Jegadeesh & Titman (1993), Returns to Buying Winners …, J. of Finance",
                  "Carhart (1997), On Persistence in Mutual Fund Performance, J. of Finance",
                  "Asness, Moskowitz & Pedersen (2013), Value and Momentum Everywhere, J. of Finance"]),
    ("Quality", ["Novy-Marx (2013), The Other Side of Value: Gross Profitability, JFE",
                 "Asness, Frazzini & Pedersen (2019), Quality Minus Junk, Rev. of Accounting Studies",
                 "Piotroski (2000), Value Investing …, J. of Accounting Research"]),
    ("Low-volatility", ["Ang, Hodrick, Xing & Zhang (2006), Cross-Section of Volatility, J. of Finance",
                        "Frazzini & Pedersen (2014), Betting Against Beta, JFE",
                        "Baker, Bradley & Wurgler (2011), Benchmarks as Limits to Arbitrage, FAJ"]),
    ("Covariance & VRP", ["Fama & French (2015), A Five-Factor Asset Pricing Model, JFE",
                          "Whaley (2002), Return and Risk of the CBOE BuyWrite Index, J. of Derivatives",
                          "Carr & Wu (2009), Variance Risk Premiums, Review of Financial Studies"]),
]
top = y + 0.1
for i, (cat, refs) in enumerate(cites):
    yy = top + i * 1.06
    text(s, Inches(0.7), Inches(yy), Inches(2.5), Inches(0.4), cat, size=13, color=ACCENT_DK, bold=True)
    for j, r in enumerate(refs):
        text(s, Inches(3.0), Inches(yy + j * 0.31), Inches(9.6), Inches(0.32),
             "·  " + r, size=11, color=TEXT)
footer(s, "Selected literature")

content("Appendix · A3", "Optimizer — mathematical detail", [
    {"runs": [("Objective:  ", True), ("maximize  μᵀw − λ·wᵀΣw", False, INK)]},
    {"runs": [("Expected return proxy:  ", True), ("μᵢ = rank(compositeᵢ)/N × target_return_scale", False, INK)]},
    {"runs": [("Constraints:  ", True), ("Σw = 0.95 · 0 ≤ wᵢ ≤ 0.05 · Σ(sector) ≤ 0.30 · wᵢ ∈ {0} ∪ [4%, 5%]", False, INK)]},
    {"runs": [("Default λ = 0 ", True), ("— a pure linear program; the box/sector/min constraints "
      "supply diversification (D25). The λ>0 mean-variance path is preserved in code.")]},
    {"runs": [("Covariance:  ", True), ("Σ = B·cov(F)·Bᵀ + diag(residual var), B from OLS of each "
      "asset's excess return on the FF5 factors over a 60-day window (D23a).")]},
    {"runs": [("Semi-continuous min position. ", True), ("Non-convex, and no MIQP solver installed — "
      "so: pre-select top-50, solve the convex QP, drop the single smallest sub-floor name, re-solve "
      "to convergence, then walk an infeasibility-relaxation ladder before holding (D23b).")]},
    {"runs": [("Solver:  ", True), ("CVXPY + CLARABEL; 'Unknown'-sector names (ETFs without a SIC) are exempt from the sector cap.")]},
], subtitle="A convex QP with a min-position cleanup heuristic")

content("Appendix · A4", "Options — Black-Scholes detail", [
    {"runs": [("Call delta:  ", True), ("Δ = N(d₁),   d₁ = [ ln(S/K) + (r + ½σ²)T ] / (σ√T)", False, INK)]},
    {"runs": [("Strike for a target delta (closed form):  ", True), ("K = S·exp[ (r+½σ²)T − Φ⁻¹(Δ)·σ√T ]", False, INK)]},
    {"runs": [("Covered-call return:  ", True), ("min(equity_return, strike_return) + premium_yield", False, INK)]},
    {"runs": [("Cash-secured put return:  ", True), ("premium_yield + min(equity_return − strike_return, 0)", False, INK)]},
    {"runs": [("Rates default r = 0. ", True), ("At 30–45 DTE the carry term is second-order, and the "
      "backtest works in excess-of-cash terms.")]},
    {"runs": [("Volatility is the load-bearing input (D27). ", True), ("Live, IV ≈ annualized trailing "
      "realized vol; in backtest, real chains where available, else this estimate, sensitivity-tested "
      "at the realized-vol floor and a market-implied (VIX-scaled) upper bound.")]},
    {"runs": [("Delta is computed, not read. ", True), ("Alpaca's indicative chain has no Greeks, so "
      "every candidate strike is priced via Black-Scholes from spot and the IV estimate.")]},
], subtitle="Pure, vectorized math — shared by the backtest and the live overlay")

table_slide("Appendix · A5", "Delta sweep — why 0.30 (D29)",
            ["Target delta", "Sharpe @ realized floor", "Sharpe @ market IV", "Assignment rate"],
            [["0.25", "~0.54", "1.18", "18%"],
             ["0.30  (chosen)", "~0.54", "1.31", "22%"],
             ["0.35", "~0.55", "1.43", "30%"],
             ["0.40", "~0.55", "1.52", "36%"]],
            [3.0, 3.1, 2.9, 2.93], highlight_rows={1}, font_size=12,
            subtitle="Premium pays only when implied vol exceeds realized; 0.30 stays under the 30% assignment cap",
            note="Higher delta harvests more premium but calls more names away. 0.30 is the balanced step up from "
                 "the original 0.25 — market-IV Sharpe 1.31 at a comfortable 22% assignment.")

table_slide("Appendix · A6", "Turnover sweep — why churn is left on (D26)",
            ["Incumbent bonus", "Turnover / mo", "Net return", "Sharpe"],
            [["0.00  (off, chosen)", "33%", "+14.0%", "0.75"],
             ["0.10", "25%", "+10.6%", "0.59"],
             ["0.20", "20%", "+10.7%", "0.60"],
             ["0.30", "18%", "+5.5%", "0.36"]],
            [3.4, 2.9, 2.7, 2.93], highlight_rows={0}, font_size=12,
            subtitle="The ~33%/mo turnover is the book staying on fresh signal — it is alpha, not waste",
            note="Realized cost of 33% turnover is only ~1%/yr (already inside net returns); suppressing it gives "
                 "up 3–4%/yr of return. The hysteresis knob is built but defaulted off.")

table_slide("Appendix · A7", "Key parameters (config/settings.yaml)",
            ["Parameter", "Value", "Parameter", "Value"],
            [["Universe", "ADV > $1M, price > $5", "Max single name", "5%"],
             ["Fundamentals", "EDGAR US-GAAP only", "Max sector", "30%"],
             ["NAV (paper)", "$100,000", "Min position", "$4,000"],
             ["Target / max leverage", "2.0× / 2.0×", "Invested / cash", "95% / 5%"],
             ["Factor weights", "25% each", "Risk-aversion λ", "0.0"],
             ["Momentum window", "252 / skip 21", "Vol window", "252d"],
             ["Winsorization", "1% / 99%", "Pre-select top-K", "50"],
             ["Target delta", "0.30", "DTE window", "30–45"],
             ["Covariance", "FF5, 60d window", "Rebalance", "monthly, 13:00 ET"]],
            [3.1, 3.0, 3.0, 2.83], font_size=11.5, header_size=11,
            subtitle="Nothing is hardcoded in source — every tuneable lives in one YAML file")

# Glossary
s = _slide()
y = content_header(s, "Appendix · A8", "Glossary")
glossary = [
    ("Factor premium", "Persistent excess return for bearing a systematic risk (e.g. value)"),
    ("Composite score", "Equal-weighted average of the four standardized factor sub-scores"),
    ("z-score", "Standardized distance from the cross-sectional mean, in std-deviations"),
    ("Winsorize", "Clip extreme values to a percentile before computing statistics"),
    ("Delta (Δ)", "Option's sensitivity to the underlying ≈ probability of finishing in-the-money"),
    ("DTE", "Days to expiration of an option contract"),
    ("Covered call", "Selling a call against stock you own — income for capped upside"),
    ("VRP", "Volatility risk premium — implied vol persistently exceeding realized vol"),
    ("Assignment", "Being obligated to sell the stock when a short call finishes in-the-money"),
    ("Sharpe ratio", "Return per unit of volatility — the risk-adjusted return measure"),
    ("Max drawdown", "Largest peak-to-trough decline over the period"),
    ("Reconciliation", "Squaring the internal database against live broker positions"),
]
top = y + 0.1
for i, (term, defn) in enumerate(glossary):
    col = i // 6
    row = i % 6
    xx = 0.7 + col * 6.15
    yy = top + row * 0.78
    text(s, Inches(xx), Inches(yy), Inches(5.9), Inches(0.32), term, size=13, color=INK, bold=True)
    text(s, Inches(xx), Inches(yy + 0.3), Inches(5.9), Inches(0.45), defn, size=11.5,
         color=MUTED, line_spacing=1.0)
footer(s, "Glossary")

# --------------------------------------------------------------------------- #
import os
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sharpe-engine.pptx")
prs.save(out)
print(f"saved {out}  —  {len(prs.slides.__iter__.__self__._sldIdLst)} slides")
