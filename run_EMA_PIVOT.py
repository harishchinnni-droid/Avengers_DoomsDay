"""
ORCHESTRATOR (EMA-Pivot) — same downstream pipeline as run_TW_ALL.py /
run_MACD.py, EMA10 + daily Pivot in place of either's confluence set.

    py run_EMA_PIVOT.py

Everything downstream of the matrix sheets (option chain, order engine,
dashboard, formatting) is UNCHANGED from the other two pipelines -- it
only ever reads the "Final Recomm" row out of the Final sheet, and
matrix_sheets_ema_pivot.py produces that row in the exact same shape. The
only real difference is step 8:

    run_TW_ALL.py    8  matrix_sheets.py            TW ALL + RSI + ADX + EMA VWAP -> Final
    run_MACD.py      8  matrix_sheets_v2.py          RSI + MACD                     -> Final
    this file        8  matrix_sheets_ema_pivot.py   EMA10 + Pivot (one rule)       -> Final

Audit checks are OFF by default here too (config.EMA_PIVOT_AUDIT_ENABLED)
-- straight from Final Recomm to the order sheet, same starting point as
the MACD pipeline, both being built up step by step.

WHY A SEPARATE FILE INSTEAD OF EDITING run_TW_ALL.py / run_MACD.py
--------------------------------------------------------------------
Same reasoning as run_MACD.py's own docstring: each pipeline is a full,
independently runnable copy with its own output filenames, so all three
can be run on the same date without any one overwriting another's
workbook, and none of them can regress another by being edited.

OUTPUT FILES
------------
Dated workbook : 'DD-Mon-YY FNO-L-EMA-PIVOT.xlsx' / '-BT-EMA-PIVOT.xlsx'
                 (paths.dated_workbook_path_ema_pivot)
Backtest report: 'Backtest EMA-PIVOT <first> to <last>.xlsx' (+ matching .html)

STEPS
-----
     1  bootstrap.py               install missing packages
     2  broker_auth.py             Angel One (headless TOTP) + Zerodha (cached/Selenium)
     3  calendar_mgmt.py           LIVE or BACKTEST, validate dates against NSE calendar
     4  file_mgmt.py               copy 01_SourceFile.xlsx -> 'DD-Mon-YY FNO-L-EMA-PIVOT.xlsx'
     5  token_mgmt.py              resolve symbols -> Zerodha instrument tokens
     6  angel_scrip.py             download Angel One scrip master (weekly cache)
     7  data_ingestion.py          5-min candles, cached, no unclosed bars
     8  matrix_sheets_ema_pivot.py EMA10 + Pivot matrix sheet + Final
     9  option_chain.py            ATM strike, +/-2 window, quotes
    10  option_audit.py            SKIPPED by default (EMA_PIVOT_AUDIT_ENABLED)
    11  order_engine.py            qualification -> sizing -> exit ladder -> Orders
    12  live_loop.py               LIVE only: fetch at candle close + 5s
    13  dashboard.py               KPI panels (off by default, DASHBOARD_ENABLED)
    14  excel_format.py            auto-fit columns, colour the Final Recomm row

Steps 9-11 need an Angel One session. Without one the run still produces the
matrix sheet and says plainly that the order sheets were skipped -- it does
not write empty Orders/Rejected sheets that look like "no signals today".

NOTHING HERE PLACES A BROKER ORDER. Every position is PAPER until
config.LIVE_TRADING is True and an order-placement module exists.
"""

from __future__ import annotations

import sys
import traceback
from datetime import date

import bootstrap
import matrix_sheets_ema_pivot


def _banner(step, title: str) -> None:
    print(f"\n{'=' * 70}\nSTEP {step} — {title}  [EMA-Pivot]\n{'=' * 70}")


def run_one_date(trade_date: date, mode: str, kite, angel,
                 accumulator=None) -> None:
    """Everything that happens for a single trading date."""
    import angel_scrip
    import calendar_mgmt
    import config
    import data_ingestion
    import dashboard
    import excel_format
    import file_mgmt
    import ist_clock
    import option_audit
    import order_engine
    import pandas as pd
    import paths
    import token_mgmt

    stamp = calendar_mgmt.format_date(trade_date)
    print(f"\n{'#' * 70}\n#  {stamp}  [{mode}]  -- PIPELINE EMA-PIVOT\n{'#' * 70}")

    # ---- 4. trade file --------------------------------------------------
    _banner(4, "Trade file")
    workbook = file_mgmt.create_trade_file(
        trade_date, mode, path_fn=paths.dated_workbook_path_ema_pivot)

    # ---- 5. Zerodha tokens ----------------------------------------------
    _banner(5, "Zerodha instrument tokens")
    df_ref = token_mgmt.update_instrument_tokens(workbook, kite, trade_date)

    # ---- 6. Angel scrip master -------------------------------------------
    _banner(6, "Angel One scrip master")
    scrip = None
    try:
        scrip = angel_scrip.load_scrip_master()
        df_ref = angel_scrip.resolve_angel_tokens(df_ref, scrip)
        file_mgmt.write_sheet(workbook, config.WATCHLIST_SHEET, df_ref)
    except Exception as exc:
        print(f"[scrip] non-fatal: {exc}")

    # ---- 7. candles -------------------------------------------------------
    _banner(7, "Historical candles")
    candles = data_ingestion.download_historical_data(
        df_ref, trade_date, kite, mode=mode)
    if not candles:
        print("[pipeline] no candle data for any symbol -- skipping this date")
        return

    problems: list[str] = []
    for sym, df in candles.items():
        problems.extend(data_ingestion.validate_candles(df, sym))
    if problems:
        print(f"[pipeline] {len(problems)} data quality issue(s):")
        for p in problems[:10]:
            print("   -", p)

    # ---- 7a. day Opening/Closing/Change% on the Reference sheet -----------
    df_ref = token_mgmt.add_day_ohlc(df_ref, candles, trade_date)
    file_mgmt.write_sheet(workbook, config.WATCHLIST_SHEET, df_ref)

    # ---- 7b/7c. index + VIX gates -- same as the other pipelines, kept for parity
    index_candles = None
    if config.REGIME_FILTER_ENABLED:
        try:
            from datetime import timedelta as _td
            start = ist_clock.combine_ist(
                trade_date - _td(days=config.LOOKBACK_DAYS), ist_clock.MARKET_OPEN)
            end = ist_clock.combine_ist(trade_date, ist_clock.MARKET_CLOSE)
            if mode == config.LIVE:
                end = min(end, ist_clock.now_ist())
            index_candles = data_ingestion._fetch_from_kite(
                kite, config.NIFTY_INDEX_TOKEN, config.INTERVAL,
                start, end, "NIFTY50-INDEX")
            print(f"[pipeline] NIFTY index candles: "
                  f"{0 if index_candles is None else len(index_candles)} bars "
                  f"for the regime gate")
        except Exception as exc:
            print(f"[pipeline] index feed unavailable ({exc}) -- regime gate "
                  f"will report SKIPPED")

    vix_candles = None
    if config.VIX_FILTER_ENABLED:
        try:
            from datetime import timedelta as _td2
            vstart = ist_clock.combine_ist(
                trade_date - _td2(days=5), ist_clock.MARKET_OPEN)
            vend = ist_clock.combine_ist(trade_date, ist_clock.MARKET_CLOSE)
            if mode == config.LIVE:
                vend = min(vend, ist_clock.now_ist())
            vix_candles = data_ingestion._fetch_from_kite(
                kite, config.INDIA_VIX_TOKEN, config.INTERVAL,
                vstart, vend, "INDIAVIX")
            if vix_candles is not None and not vix_candles.empty:
                print(f"[pipeline] India VIX last close: "
                      f"{float(vix_candles['close'].iloc[-1]):.2f} "
                      f"(gate at {config.VIX_MAX})")
        except Exception as exc:
            print(f"[pipeline] VIX feed unavailable ({exc}) -- gate SKIPPED")

    # ---- 8. matrix sheet (EMA10 + Pivot) -----------------------------------
    _banner(8, "Matrix sheet")
    built = matrix_sheets_ema_pivot.build_all_sheets_ema_pivot(
        workbook, candles, trade_date)
    final_df = built[config.FINAL_SHEET_NAME]
    slots = ist_clock.candle_slots(
        trade_date, config.INTERVAL_MINUTES,
        pd.Timestamp(config.MATRIX_LAST_SLOT).time())

    # ---- 9-11. order engine (unchanged from the other pipelines) -----------
    _banner("9-11", "Option chain, audit, orders")
    orders_df = rejected_df = None

    if angel is None:
        print("[orders] SKIPPED -- no Angel One session. Option quotes and "
              "the exit ladder both need one.")
        print("[orders] Orders / Rejected / Dashboard sheets NOT written, so "
              "they cannot be mistaken for 'no signals today'.")
    elif scrip is None:
        print("[orders] SKIPPED -- Angel scrip master unavailable, cannot "
              "resolve option contracts.")
    else:
        capital = option_audit.get_available_capital(angel)
        if capital is None:
            print("[orders] available capital unknown -- funding gate disabled "
                  "for this run. Sizing still respects the risk budget.")
        else:
            print(f"[orders] Angel One available cash: Rs {capital:,.2f}")

        (orders_df, rejected_df, positions, missed_df, capital_shadow_df,
         oi_blocked_df) = order_engine.process_date(
            final_df, df_ref, candles, scrip, angel, trade_date, mode,
            slots, available_capital=capital, index_candles=index_candles,
            vix_candles=vix_candles, kite=kite,
            audit_enabled=config.EMA_PIVOT_AUDIT_ENABLED)

        file_mgmt.write_sheet(workbook, "Orders", orders_df)
        file_mgmt.write_sheet(workbook, "Rejected", rejected_df)
        file_mgmt.write_sheet(workbook, "Missed_Concurrent", missed_df)
        file_mgmt.write_sheet(workbook, "Capital Shadow", capital_shadow_df)
        file_mgmt.write_sheet(workbook, "OI Blocked", oi_blocked_df)

        if accumulator is not None:
            accumulator.add(trade_date, orders_df, len(rejected_df))

        # ---- 13. dashboard --------------------------------------------------
        _banner(13, "Dashboard")
        if config.DASHBOARD_ENABLED:
            dashboard.write_dashboard_sheet(workbook, orders_df, trade_date, mode)
            stats = dashboard.compute_stats(orders_df, "OVERALL")
            print(f"[dashboard] {stats.trades} trade(s), win rate "
                  f"{stats.win_rate:.1f}%, net Rs {stats.net:,.2f}")
            if stats.closed >= 2 and stats.net > 0 and stats.net_ex_best < 0:
                print(f"[dashboard] WARNING: all profit came from one trade; the "
                      f"rest netted Rs {stats.net_ex_best:,.2f}")
        else:
            print("[dashboard] skipped (config.DASHBOARD_ENABLED = False)")

    # ---- 14. formatting ----------------------------------------------------
    _banner(14, "Workbook formatting")
    excel_format.format_workbook(workbook)

    print(f"\n[pipeline-ema-pivot] {stamp} done -> {workbook.name}")
    print(f"[pipeline-ema-pivot] sheets now in file: {file_mgmt.list_sheets(workbook)}")


def _write_skeleton_matrix_sheets(workbook, symbols: list[str],
                                  trade_date: date) -> None:
    """EMA-Pivot equivalent of run_TW_ALL._write_skeleton_matrix_sheets."""
    import config
    import file_mgmt
    import pandas as pd
    from matrix_sheets import build_matrix

    empty_frames = {sym: pd.DataFrame() for sym in symbols}
    for sheet_name, metrics in config.MATRIX_SHEETS_EMA_PIVOT.items():
        df = build_matrix(sheet_name, empty_frames, trade_date, metrics)
        file_mgmt.write_sheet(workbook, sheet_name, df)
    final_df = build_matrix(
        config.FINAL_SHEET_NAME, empty_frames, trade_date,
        config.FINAL_ROWS_EMA_PIVOT)
    file_mgmt.write_sheet(workbook, config.FINAL_SHEET_NAME, final_df)


def setup_live_day(trade_date: date, kite, angel):
    """EMA-Pivot equivalent of run_TW_ALL.setup_live_day. See that
    function's docstring for why this two-phase setup exists."""
    import angel_scrip
    import config
    import excel_format
    import file_mgmt
    import order_sheet
    import paths
    import token_mgmt

    _banner(4, "Trade file")
    workbook = file_mgmt.create_trade_file(
        trade_date, config.LIVE, path_fn=paths.dated_workbook_path_ema_pivot)

    _banner(5, "Zerodha instrument tokens")
    df_ref = token_mgmt.update_instrument_tokens(workbook, kite, trade_date)

    _banner(6, "Angel One scrip master")
    scrip = None
    try:
        scrip = angel_scrip.load_scrip_master()
        df_ref = angel_scrip.resolve_angel_tokens(df_ref, scrip)
        file_mgmt.write_sheet(workbook, config.WATCHLIST_SHEET, df_ref)
    except Exception as exc:
        print(f"[scrip] non-fatal: {exc}")

    _banner(7, "Historical backfill (warms the cache for the live loop)")
    import data_ingestion
    candles = data_ingestion.download_historical_data(df_ref, trade_date, kite,
                                                       mode=config.LIVE)
    print(f"[live-setup] backfilled {len(candles)}/{len(df_ref)} symbol(s) -- "
          f"cache warm for incremental top-ups")

    print("[live-setup] writing skeleton sheets (no candles yet)")
    symbols = df_ref[config.COL_SYMBOL].astype(str).tolist()
    _write_skeleton_matrix_sheets(workbook, symbols, trade_date)
    file_mgmt.write_sheet(workbook, "Orders", order_sheet.build_orders_sheet([]))
    file_mgmt.write_sheet(workbook, "Rejected", order_sheet.build_rejected_sheet([]))
    file_mgmt.write_sheet(workbook, "Missed_Concurrent", order_sheet.build_orders_sheet([]))
    file_mgmt.write_sheet(workbook, "Capital Shadow", order_sheet.build_orders_sheet([]))
    file_mgmt.write_sheet(workbook, "OI Blocked", order_sheet.build_orders_sheet([]))

    excel_format.format_workbook(workbook)

    print(f"\n[live-setup] ready -- {workbook.name}, {len(df_ref)} symbols. "
          f"Waiting for the first candle close at 09:20:05.")
    return workbook, df_ref, scrip


def run_live_day(trade_date: date, kite, angel) -> None:
    """EMA-Pivot equivalent of run_TW_ALL.run_live_day."""
    import config
    import data_ingestion
    import dashboard
    import excel_format
    import file_mgmt
    import ist_clock
    import live_loop
    import option_audit
    import order_engine
    import pandas as pd
    import token_mgmt

    workbook, df_ref, scrip = setup_live_day(trade_date, kite, angel)
    slots = ist_clock.candle_slots(
        trade_date, config.INTERVAL_MINUTES,
        pd.Timestamp(config.MATRIX_LAST_SLOT).time())
    state = order_engine.new_live_state()

    def on_candle(fetch_time, slot: str) -> None:
        _banner(7, f"{slot} candle closed -> updating")
        candles = data_ingestion.update_incremental_data(df_ref, trade_date, kite)
        if not candles:
            print("[live] no candle data yet -- skipping this cycle")
            return

        df_ref_now = token_mgmt.add_day_ohlc(df_ref, candles, trade_date)
        file_mgmt.write_sheet(workbook, config.WATCHLIST_SHEET, df_ref_now)

        built = matrix_sheets_ema_pivot.build_all_sheets_ema_pivot(
            workbook, candles, trade_date)
        final_df = built[config.FINAL_SHEET_NAME]

        _banner("9-11", f"{slot}: qualify signals, advance open positions")
        if angel is None:
            print("[orders] SKIPPED -- no Angel One session.")
        elif scrip is None:
            print("[orders] SKIPPED -- Angel scrip master unavailable.")
        else:
            index_candles = None
            if config.REGIME_FILTER_ENABLED:
                try:
                    from datetime import timedelta as _td
                    start = ist_clock.combine_ist(
                        trade_date - _td(days=config.LOOKBACK_DAYS),
                        ist_clock.MARKET_OPEN)
                    index_candles = data_ingestion._fetch_from_kite(
                        kite, config.NIFTY_INDEX_TOKEN, config.INTERVAL,
                        start, ist_clock.now_ist(), "NIFTY50-INDEX")
                except Exception as exc:
                    print(f"[live] index feed unavailable ({exc}) -- "
                          f"regime gate will report SKIPPED")

            vix_candles = None
            if config.VIX_FILTER_ENABLED:
                try:
                    from datetime import timedelta as _td2
                    vstart = ist_clock.combine_ist(
                        trade_date - _td2(days=5), ist_clock.MARKET_OPEN)
                    vix_candles = data_ingestion._fetch_from_kite(
                        kite, config.INDIA_VIX_TOKEN, config.INTERVAL,
                        vstart, ist_clock.now_ist(), "INDIAVIX")
                except Exception as exc:
                    print(f"[live] VIX feed unavailable ({exc}) -- gate SKIPPED")

            capital = option_audit.get_available_capital(angel)
            if capital is not None:
                print(f"[orders] Angel One available cash: Rs {capital:,.2f}")

            (orders_df, rejected_df, missed_df, capital_shadow_df,
             oi_blocked_df) = order_engine.advance_live_day(
                state, final_df, df_ref_now, candles, scrip, angel, trade_date,
                config.LIVE, slots, available_capital=capital,
                index_candles=index_candles, vix_candles=vix_candles, kite=kite)

            file_mgmt.write_sheet(workbook, "Orders", orders_df)
            file_mgmt.write_sheet(workbook, "Rejected", rejected_df)
            file_mgmt.write_sheet(workbook, "Missed_Concurrent", missed_df)
            file_mgmt.write_sheet(workbook, "Capital Shadow", capital_shadow_df)
            file_mgmt.write_sheet(workbook, "OI Blocked", oi_blocked_df)

            _banner(13, "Dashboard")
            if config.DASHBOARD_ENABLED:
                dashboard.write_dashboard_sheet(
                    workbook, orders_df, trade_date, config.LIVE)
                stats = dashboard.compute_stats(orders_df, "OVERALL")
                print(f"[dashboard] {stats.trades} trade(s) ({stats.open_now} open), "
                      f"win rate {stats.win_rate:.1f}%, net Rs {stats.net:,.2f}")
            else:
                print("[dashboard] skipped (config.DASHBOARD_ENABLED = False)")

        excel_format.format_workbook(workbook)
        print(f"[live] {slot} cycle done -> {workbook.name}")

    live_loop.run_live_session(on_candle, trade_date)
    print(f"\n[live] session complete -> {workbook.name}")


def main() -> int:
    _banner(1, "Package check")
    bootstrap.ensure_requirements(interactive=True)

    import broker_auth
    import calendar_mgmt
    import config
    import paths

    paths.ensure_dirs()
    print("\n" + paths.describe())
    problems = paths.verify_layout()
    if problems:
        print("\n[pipeline] layout problems:")
        for p in problems:
            print("   -", p)
        return 1

    _banner(3, "Run mode")
    mode, dates = calendar_mgmt.get_run_config()

    _banner(2, "Broker login")
    try:
        kite = broker_auth.initialize_zerodha()
    except broker_auth.AuthError as exc:
        print(f"\n[auth] Zerodha FAILED: {exc}")
        return 1

    angel = None
    try:
        angel = broker_auth.initialize_angel_one()
    except broker_auth.AuthError as exc:
        print(f"[auth] Angel One unavailable ({exc})")
        print("[auth] continuing with Zerodha only -- matrix sheets will be "
              "built, order sheets will be skipped")

    print("\nPipeline EMA-Pivot confluence: EMA10 + daily Pivot (one combined rule)")
    if config.LIVE_TRADING:
        print("\n[pipeline] WARNING: LIVE_TRADING is True in config.py")
    else:
        print("\n[pipeline] LIVE_TRADING is False -- paper only, no broker orders")

    if mode == config.LIVE:
        try:
            run_live_day(dates[0], kite, angel)
        except KeyboardInterrupt:
            print("\n[pipeline] interrupted by user")
            return 130
        except Exception:
            print(f"\n[pipeline] {calendar_mgmt.format_date(dates[0])} FAILED:")
            traceback.print_exc()
            return 1
        print(f"\n{'=' * 70}\nPIPELINE EMA-PIVOT COMPLETE — LIVE session\n{'=' * 70}")
        return 0

    accumulator = None
    if len(dates) > 1:
        import backtest_report
        accumulator = backtest_report.BacktestAccumulator()

    failures = 0
    for i, d in enumerate(dates, 1):
        print(f"\n[pipeline] date {i}/{len(dates)}")
        try:
            run_one_date(d, mode, kite, angel, accumulator)
        except KeyboardInterrupt:
            print("\n[pipeline] interrupted by user")
            return 130
        except Exception:
            failures += 1
            print(f"\n[pipeline] {calendar_mgmt.format_date(d)} FAILED:")
            traceback.print_exc()
            print("[pipeline] backtest mode -- continuing to next date")

    if accumulator is not None and accumulator.sessions:
        _banner(15, "Backtest report")
        accumulator.print_summary()
        if config.AUTO_CONSOLIDATED_REPORT:
            first, last = dates[0], dates[-1]
            # 'EMA-PIVOT' in the name -- distinct from the other two
            # pipelines' own consolidated report names, so none overwrites
            # another's.
            out = paths.BASE_DIR / (f"Backtest EMA-PIVOT {calendar_mgmt.format_date(first)} "
                                    f"to {calendar_mgmt.format_date(last)}.xlsx")
            accumulator.write(out)
        else:
            print("[pipeline] consolidated report NOT written "
                  "(config.AUTO_CONSOLIDATED_REPORT = False) -- run "
                  "`py rebuild_dashboard.py` to build it manually")

    print(f"\n{'=' * 70}")
    print(f"PIPELINE EMA-PIVOT COMPLETE — {len(dates)} date(s), {failures} failure(s)")
    print(f"{'=' * 70}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
