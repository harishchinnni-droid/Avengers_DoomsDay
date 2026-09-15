"""
STEP 14 — Workbook presentation: auto-fit columns and conditional formatting.

Runs last, after every sheet is written. Purely cosmetic -- nothing here
changes a number.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import config
import order_sheet

# BUY CE green, BUY PE red, WAIT grey. Deliberately not red/green for
# profit/loss elsewhere -- these mark direction, not outcome.
FILL_CE = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FONT_CE = Font(color="006100", bold=True)
FILL_PE = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FONT_PE = Font(color="9C0006", bold=True)
FILL_WAIT = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
FONT_WAIT = Font(color="808080")
# INVALID (2nd candle contradicted the signal) is amber, not red -- red
# already means BUY PE on these sheets and re-using it would read as a
# direction, not a rejection.
FILL_INVALID = PatternFill(start_color="FFE699", end_color="FFE699", fill_type="solid")
FONT_INVALID = Font(color="9C6500", bold=True)
# Candle row: coloured font only, no fill -- it's a fact row, not a decision
# row, and should not shout as loudly as Final/Confirmed Recomm.
FONT_BULL = Font(color="006100")
FONT_BEAR = Font(color="9C0006")

FILL_HEADER = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")
FONT_HEADER = Font(bold=True)

MAX_COL_WIDTH = 42
MIN_COL_WIDTH = 6


def autofit_columns(ws, max_width: int = MAX_COL_WIDTH,
                    sample_rows: int = 400) -> None:
    """
    Size every column to its widest visible value.

    Only the first `sample_rows` rows are measured. On a 200-row x 75-column
    matrix sheet, measuring everything is slow and the extra rows almost
    never change the answer.
    """
    widths: dict[int, int] = {}
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, sample_rows)):
        for cell in row:
            if cell.value is None:
                continue
            length = len(str(cell.value))
            if length > widths.get(cell.column, 0):
                widths[cell.column] = length

    for col_idx, width in widths.items():
        ws.column_dimensions[get_column_letter(col_idx)].width = min(
            max(width + 2, MIN_COL_WIDTH), max_width
        )


def style_header(ws) -> None:
    for cell in ws[1]:
        if cell.value is not None:
            cell.fill = FILL_HEADER
            cell.font = FONT_HEADER
            cell.alignment = Alignment(horizontal="center", vertical="center")


def freeze_matrix_panes(ws) -> None:
    """Lock Symbol + Metrics and the header row while scrolling 73 time columns."""
    ws.freeze_panes = "C2"


def highlight_final_recomm(ws, metrics_col: int = 2) -> int:
    """
    Colour the decision rows of the Final sheet.

    Final Recomm / Confirmed Recomm: green BUY CE, red BUY PE, grey WAIT,
    amber INVALID. Candle: green/red font only (Bullish/Bearish) -- a fact
    row, kept quieter than the decision rows.

    Direct cell styling rather than a conditional-formatting rule, because
    the rule would have to cover the whole sheet and would then also colour
    the component rows above it. Only the decision row should stand out.
    """
    touched = 0
    for row in ws.iter_rows(min_row=2):
        label = row[metrics_col - 1].value
        if label not in ("Final Recomm", "Confirmed Recomm", "Candle"):
            continue
        touched += 1
        for cell in row[metrics_col:]:
            value = str(cell.value).strip() if cell.value is not None else ""
            if label == "Candle":
                if value == getattr(config, "CANDLE_BULLISH", "Bullish"):
                    cell.font = FONT_BULL
                elif value == getattr(config, "CANDLE_BEARISH", "Bearish"):
                    cell.font = FONT_BEAR
                continue
            if value == config.SIGNAL_BUY_CE:
                cell.fill, cell.font = FILL_CE, FONT_CE
            elif value == config.SIGNAL_BUY_PE:
                cell.fill, cell.font = FILL_PE, FONT_PE
            elif value == config.SIGNAL_WAIT:
                cell.fill, cell.font = FILL_WAIT, FONT_WAIT
            elif value == getattr(config, "SIGNAL_INVALID", "INVALID"):
                cell.fill, cell.font = FILL_INVALID, FONT_INVALID
    return touched


def highlight_pnl_column(ws, header_name: str = "Net P/L (Rs)") -> None:
    """Green above zero, red below, on the Orders sheet."""
    headers = {c.value: c.column for c in ws[1] if c.value}
    col = headers.get(header_name)
    if not col or ws.max_row < 2:
        return
    letter = get_column_letter(col)
    rng = f"{letter}2:{letter}{ws.max_row}"
    ws.conditional_formatting.add(rng, CellIsRule(
        operator="greaterThan", formula=["0"], fill=FILL_CE, font=FONT_CE))
    ws.conditional_formatting.add(rng, CellIsRule(
        operator="lessThan", formula=["0"], fill=FILL_PE, font=FONT_PE))


def style_change_pct_column(ws, header_name: str = "Change%") -> None:
    """
    Native Excel percentage format on the Reference sheet's day Change%
    column (values are stored as raw ratios, e.g. 0.0234 for +2.34%, so
    Excel's own '%' format multiplies and renders correctly -- and, unlike
    a text string, the column stays sortable and usable in a formula).
    Green above zero, red below, same convention as the P/L columns
    elsewhere.
    """
    headers = {c.value: c.column for c in ws[1] if c.value}
    col = headers.get(header_name)
    if not col or ws.max_row < 2:
        return
    letter = get_column_letter(col)
    for row in range(2, ws.max_row + 1):
        cell = ws.cell(row=row, column=col)
        if cell.value is not None:
            cell.number_format = "0.00%"
    rng = f"{letter}2:{letter}{ws.max_row}"
    ws.conditional_formatting.add(rng, CellIsRule(
        operator="greaterThan", formula=["0"], fill=FILL_CE, font=FONT_CE))
    ws.conditional_formatting.add(rng, CellIsRule(
        operator="lessThan", formula=["0"], fill=FILL_PE, font=FONT_PE))


def style_header_groups(ws, groups: dict[str, str],
                        notes: dict[str, str] | None = None) -> int:
    """
    Colour each header cell by which step of the pipeline produced that
    column, instead of the flat FILL_HEADER every sheet gets from
    style_header(). Orders alone has 56 columns spanning signal, trigger
    timing, contract resolution, sizing, live tracking and outcome -- one
    colour told Harish nothing about which was which (his ask, 02-Aug-26).

    A cell comment names the group too, so the colour is self-explanatory
    on first hover rather than something to memorise. `notes` lets a sheet
    override the generic per-colour blurb with something specific to that
    column -- the "sizing" colour means "Entry price/stop/targets" on
    Orders but "day Opening/Closing/Change%" on Reference, and re-using the
    Orders wording there would be actively misleading.

    Only touches header cells whose text is a KNOWN key in `groups` -- an
    unrecognised header (a column added later and not yet classified here)
    keeps whatever style_header() already gave it rather than being
    silently mis-grouped.
    """
    notes = notes or {}
    touched = 0
    for cell in ws[1]:
        entry = _HEADER_GROUP_FILLS.get(groups.get(cell.value))
        if entry is None:
            continue
        fill_hex, default_note = entry
        cell.fill = PatternFill(start_color=fill_hex, end_color=fill_hex,
                                fill_type="solid")
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        if cell.comment is None:
            cell.comment = Comment(notes.get(cell.value, default_note), "MJ Spidey")
        touched += 1
    return touched


# One fill + one explanatory note per logical group. Colours are deliberately
# far apart on the wheel (not shades of one hue) so they read as distinct
# categories at a glance, not as a gradient.
_HEADER_GROUP_FILLS: dict[str, tuple[str, str]] = {
    "identity": ("BDD7EE", "Symbol / trade identity"),
    "trigger":  ("D9C6EC", "3-bar confluence trigger timing "
                          "(Pre-Entry / Entry / Support)"),
    "contract": ("C6E0D4", "Option contract resolved from the chain"),
    "decision": ("FCE4D6", "Entry decision timing + no-lookahead audit"),
    "sizing":   ("FFF2CC", "Entry price, stop/targets, position size"),
    "tracking": ("E2EFDA", "Live exit-ladder tracking as the trade runs"),
    "outcome":  ("F8CBAD", "Resolved P/L and exit"),
    "broker":   ("D9D9D9", "Real broker order fields -- unused while "
                          "LIVE_TRADING is False"),
}

# Orders sheet, all columns from order_sheet.ORDER_COLUMNS.
ORDERS_HEADER_GROUPS: dict[str, str] = {
    "Symbol": "identity", "Signal": "identity",
    "Entry Type": "identity", "OI Check": "identity",

    "Pre-Entry Trigger Time": "trigger", "Pre-Entry Trigger Status": "trigger",
    "Entry Trigger Time": "trigger", "Entry Trigger Status": "trigger",
    "Support Entry Time": "trigger", "Support Trigger Status": "trigger",
    "Exit Trigger Time": "trigger", "Exit Trigger Status": "trigger",

    "Spot Price": "contract", "ATM Strike": "contract",
    "Option Symbol": "contract", "Option Token": "contract",
    "Lot Size": "contract", "Days To Expiry": "contract",

    "Signal Confirmed At": "decision", "Entry Time": "decision",
    "Entry Lag (min)": "decision", "Lookahead Check": "decision",

    "Entry LTP": "sizing", "Stop Loss LTP": "sizing",
    # Target 1..N LTP -- N = len(config.TARGET_MULTS), 10 as of 12-Sep-26
    # (was a hardcoded 3). Built from order_sheet.TARGET_LTP_COLUMNS so this
    # never drifts out of sync with the actual target ladder again.
    **{col: "sizing" for col in order_sheet.TARGET_LTP_COLUMNS},
    "Risk/Unit (Rs)": "sizing", "Quantity (Lots)": "sizing",
    "Quantity (Units)": "sizing", "Risk Amount (Rs)": "sizing",
    "Capital Required (Rs)": "sizing",

    "Current LTP": "tracking", "Max LTP": "tracking", "Min LTP": "tracking",
    **{col: "tracking" for col in order_sheet.TARGET_HIT_COLUMNS},
    "Breakeven Active": "tracking", "TSL Breach Streak": "tracking",
    "Effective Stop": "tracking",

    "Gross P/L (Rs)": "outcome", "Costs (Rs)": "outcome",
    "Net P/L (Rs)": "outcome", "Order ID": "outcome", "Exit Time": "outcome",
    "Exit Reason": "outcome", "Trade Mode": "outcome",

    "Broker Order ID": "broker",
    "Actual Fill Price": "broker", "Actual Exit Price": "broker",
    "Exit Order ID": "broker",
}

# Reference sheet.
REFERENCE_HEADER_GROUPS: dict[str, str] = {
    "Sector": "identity", "Symbol / StrikePrice": "identity",
    "Company Name": "identity", "Expiry Date": "identity",
    "Option Price Difference": "identity",

    "Zerodha_Token": "contract", "Angel_Token": "contract",
    "Instrument Type": "contract", "Token Note": "contract",

    "Opening": "sizing", "Closing": "sizing", "Change%": "sizing",
}

REFERENCE_HEADER_NOTES: dict[str, str] = {
    "Zerodha_Token": "Zerodha instrument token (step 5, token_mgmt.py)",
    "Angel_Token": "Angel One scrip token (step 6, angel_scrip.py)",
    "Instrument Type": "FUTSTK/OPTSTK etc -- decides which Kite instrument "
                       "row matches (step 5)",
    "Token Note": "How this row's token was resolved -- cached, matched, "
                 "or UNRESOLVED",
    "Opening": "This trade date's Opening/Closing/Change%, off the "
              "underlying's own candles (token_mgmt.add_day_ohlc)",
    "Closing": "This trade date's Opening/Closing/Change%, off the "
              "underlying's own candles (token_mgmt.add_day_ohlc)",
    "Change%": "(Closing-Opening)/Opening. Green above zero, red below.",
}


def format_workbook(path: Path, verbose: bool = True) -> None:
    """Apply everything. Safe to run repeatedly."""
    wb = load_workbook(path)

    for ws in wb.worksheets:
        if ws.max_row < 1:
            continue

        # Dashboard owns its own styling end to end (dashboard.py) -- title
        # banner, section colours, column widths matched against
        # F:\06_Claude_v2's layout. A generic autofit/style_header pass here
        # would overwrite the navy title with the generic header fill and
        # resize columns back to content-width, wrecking the panel layout.
        if ws.title == "Dashboard":
            continue

        autofit_columns(ws)
        style_header(ws)

        if ws.title == config.FINAL_SHEET_NAME:
            n = highlight_final_recomm(ws)
            freeze_matrix_panes(ws)
            if verbose:
                print(f"[format] {ws.title}: highlighted {n} Final Recomm row(s)")
        elif ws.title in config.MATRIX_SHEETS or ws.title in getattr(
                config, "MATRIX_SHEETS_V2", {}):
            # MATRIX_SHEETS_V2 (run_MACD.py, MACD in place of TW ALL)
            # gets the same freeze-pane treatment as the original pipeline's
            # matrix sheets -- getattr guard so this file still works if an
            # older config.py without V2 constants is ever loaded.
            freeze_matrix_panes(ws)
        elif ws.title in ("Orders", "Missed_Concurrent", "Capital Shadow", "OI Blocked"):
            # Missed_Concurrent/Capital Shadow/OI Blocked (07/09-Aug-26) are
            # all built from the same order_sheet.build_orders_sheet() call
            # as Orders -- identical columns, so they get identical styling
            # rather than the plain header Rejected/Reference get.
            highlight_pnl_column(ws)
            n = style_header_groups(ws, ORDERS_HEADER_GROUPS)
            ws.freeze_panes = "B2"
            if verbose:
                print(f"[format] {ws.title}: grouped {n}/{len(ORDERS_HEADER_GROUPS)} "
                      f"header(s) by pipeline step")
        elif ws.title in ("Reference", "Rejected"):
            ws.freeze_panes = "A2"
            if ws.title == "Reference":
                style_change_pct_column(ws)
                style_header_groups(ws, REFERENCE_HEADER_GROUPS,
                                   REFERENCE_HEADER_NOTES)

    wb.save(path)
    if verbose:
        print(f"[format] auto-fitted and styled {len(wb.worksheets)} sheet(s) "
              f"in {path.name}")


if __name__ == "__main__":
    import tempfile
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = config.FINAL_SHEET_NAME
    ws.append(["Symbol", "Metrics", "09:15", "09:20", "09:25", "09:30"])
    ws.append(["ASIANPAINT", "TW ALL Recomm", "WAIT", "BUY CE", "BUY CE", "BUY CE"])
    ws.append(["ASIANPAINT", "RSI Recomm", "BUY CE", "BUY CE", "BUY CE", "BUY PE"])
    ws.append(["ASIANPAINT", "ADX Recomm", "WAIT", "BUY CE", "BUY CE", "BUY CE"])
    ws.append(["ASIANPAINT", "Final Recomm", "WAIT", "BUY CE", "BUY CE", "WAIT"])

    tmp = Path(tempfile.mkdtemp()) / "fmt_test.xlsx"
    wb.save(tmp)
    format_workbook(tmp)

    check = load_workbook(tmp)[config.FINAL_SHEET_NAME]
    final_row = [r for r in check.iter_rows(min_row=2)
                 if r[1].value == "Final Recomm"][0]
    print("\nFinal Recomm cell fills:")
    for cell in final_row[2:]:
        print(f"  {str(cell.value):<8} -> fill {cell.fill.start_color.rgb}")
    assert final_row[3].fill.start_color.rgb.endswith("C6EFCE"), "BUY CE not green"
    assert final_row[5].fill.start_color.rgb.endswith("F2F2F2"), "WAIT not grey"

    other = [r for r in check.iter_rows(min_row=2) if r[1].value == "RSI Recomm"][0]
    assert other[2].fill.start_color.rgb in ("00000000", None), \
        "component row should not be coloured"
    print("\ncomponent rows left unstyled: OK")
    print("excel_format self-check passed")
