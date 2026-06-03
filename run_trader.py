"""
run_trader.py — main entry point. Wires scanner → enrich → chart → AI → orders.

    py run_trader.py                 # paper mode (config ACCOUNT_TYPE/port decide)
    py run_trader.py --allow-live     # required if connected account is live
    py run_trader.py --dry-run        # run pipeline + AI, but never place orders
    py run_trader.py --no-pm          # disable the Polygon PM/AH fallback scanner

Event loop:
  • IB scanner (TriggerMonitor) is primary during RTH.
  • Polygon PM scanner runs in parallel and only acts in PM/AH sessions.
  • A 15:55 ET task force-flattens every open position (hard kill).
  • Each trigger runs run_pipeline() as its own task so a slow AI call never
    blocks the scanner.

Run preflight.py first. IB Gateway must be running and logged in.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict

import pytz

import config
import fill_logger
import status as status_io
from ai_analyst import call_ai
from chart_builder import build_chart
from enricher import enrich
from order_router import forced_exit_sweep, shares_for, submit_orders
from polygon_pm_scanner import PolygonPMScanner
from trigger_monitor import (Trigger, TriggerMonitor, quiet_scanner_cancel_noise,
                             session_now)

# Force UTF-8 on stdout/stderr. Under Task Scheduler / redirected output, Windows
# defaults to the cp1252 codepage, which can't encode the ━ / — / 🚨 characters in
# the console blocks → UnicodeEncodeError crash. reconfigure() makes us emit UTF-8
# regardless of how we're launched.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Windows + ib_insync need the selector loop policy (see CLAUDE.md gotchas).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logger = logging.getLogger("run_trader")
ET = pytz.timezone("America/New_York")
BAR = "━" * 40


async def _connect_ib(max_wait_seconds: int = 600, retry_every: int = 15):
    # Must use connectAsync here: the sync ib.connect() spins its own loop and
    # blows up ("event loop is already running") inside asyncio.run().
    #
    # Retry instead of crashing: when auto-launched at 03:55 the Gateway may still
    # be coming up (evening auto-restart, slow morning). One failed attempt would
    # otherwise forfeit the whole pre-market session before anyone is awake to see.
    from ib_insync import IB
    ib = IB()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            await ib.connectAsync(config.IB_HOST, config.IB_PORT,
                                  clientId=config.IB_CLIENT_ID, timeout=10)
            logger.info("IB connected on attempt %d", attempt)
            return ib
        except Exception as e:
            if loop.time() >= deadline:
                logger.error("could not connect to IB Gateway after %ds (%d attempts): "
                             "%s — is it running and logged in?",
                             max_wait_seconds, attempt, e)
                raise
            logger.warning("IB connect attempt %d failed (%s) — retrying in %ds",
                           attempt, e, retry_every)
            await asyncio.sleep(retry_every)


def _start_dashboard(host: str, port: int):
    """Serve the status dashboard in a daemon thread. Best-effort: a bind
    failure (e.g. port in use) logs a warning and trading continues unaffected."""
    import threading
    from http.server import ThreadingHTTPServer
    import dashboard
    try:
        srv = ThreadingHTTPServer((host, port), dashboard.Handler)
    except OSError as e:
        logger.warning("dashboard not started on %s:%d (%s) — trading continues",
                       host, port, e)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True, name="dashboard").start()
    logger.info("dashboard → http://%s:%d", host, port)
    return srv


def _account_guard(ib, allow_live: bool, dry_run: bool) -> bool:
    """
    Decide whether it's safe to start, given the connected account + flags.

      • --dry-run            : runs against ANY account (incl. live) — places no
                               orders, so no --allow-live needed.
      • live account + --allow-live : real orders ENABLED (loud warning).
      • live account, neither flag  : REFUSE — force an explicit choice so live
                               trading is never accidental.
      • paper-declared config + live account : unbypassable abort (mismatch).
    """
    accounts = ib.managedAccounts()
    live_accts = [a for a in accounts if a and not a.startswith("DU")]
    declared_paper = str(config.ACCOUNT_TYPE).lower() != "live"

    # config says paper but the Gateway is on a live account → fix the config or
    # the login mode, never wave it through. (Won't trigger with ACCOUNT_TYPE=live.)
    if live_accts and declared_paper:
        logger.error("⛔ REFUSING TO START: ACCOUNT_TYPE=paper but Gateway is on "
                     "LIVE account(s) %s. Set ACCOUNT_TYPE='live' (real-time data) "
                     "or log Gateway into Paper Trading. --allow-live can't override.",
                     live_accts)
        return False

    if dry_run:
        logger.info("DRY-RUN — connected to %s; live data, NO orders will be placed",
                    accounts)
        return True

    is_live = bool(live_accts) or config.IB_PORT == 4001
    if is_live:
        if not allow_live:
            logger.error("⛔ REFUSING TO START: live account %s and no mode flag. "
                         "Choose one:\n"
                         "    py run_trader.py --dry-run      # live data, NO orders "
                         "(what-if validation)\n"
                         "    py run_trader.py --allow-live   # place REAL orders",
                         live_accts)
            return False
        logger.warning("⚠⚠ --allow-live set on LIVE account %s — REAL ORDERS ENABLED ⚠⚠",
                       live_accts)
    else:
        logger.info("paper account confirmed: %s", accounts)
    return True


def _print_trigger_block(trig: Trigger, enrichment: Dict[str, Any],
                         decision: Dict[str, Any], order_rec) -> None:
    et = trig.timestamp.astimezone(ET).strftime("%H:%M")
    e = enrichment
    print(f"\n{BAR}")
    print(f"🚨 TRIGGER: {trig.symbol}  +{trig.pct_gain:.0%}  "
          f"${trig.prior_close:.2f} → ${trig.last_price:.2f}  {et} ET ({trig.session})")
    print(BAR)
    adv = f"${e['adv_20']:,.0f}" if e.get("adv_20") else "n/a"
    ratio = f"{e['vol_adv_ratio']:.1f}×" if e.get("vol_adv_ratio") else "n/a"
    mcap = f"${e['market_cap']/1e6:,.0f}M" if e.get("market_cap") else "n/a"
    print(f"  ADV (20d):  {adv:>14}   Vol ratio: {ratio}")
    print(f"  Mkt Cap:    {mcap:>14}   Sector: {e.get('sector') or 'n/a'}")
    news = e.get("news_headlines") or []
    if news:
        print(f"  News:       \"{news[0].get('title')}\" ({news[0].get('published','')})")
    print(f"  Chart:      {e.get('_chart_path') or 'n/a'}")
    print()
    if decision.get("decision") == "GO":
        entry = decision["entry_limit"]
        t1 = decision["target_1_price"]
        t1pct = (t1 / entry - 1) * 100
        stop = decision["stop_loss_price"]
        stoppct = (stop / entry - 1) * 100
        print(f"  AI DECISION:  ✅ GO  (confidence: {decision.get('confidence')})")
        print(f"  Entry limit:  ${entry:.2f}")
        print(f"  Target 1:     ${t1:.2f}  (+{t1pct:.0f}%)  "
              f"{decision.get('target_1_pct_shares')}% shares"
              f"{' → trail remainder' if decision.get('trail_after_target_1') else ''}")
        if decision.get("target_2_price"):
            t2 = decision["target_2_price"]
            print(f"  Target 2:     ${t2:.2f}  (+{(t2/entry-1)*100:.0f}%)")
        print(f"  Stop loss:    ${stop:.2f}  ({stoppct:.0f}%)")
        on_pct = int(decision.get("overnight_hold_pct") or 0)
        if on_pct > 0:
            print(f"  Overnight:    hold {on_pct}% past close on GTC stop "
                  f"(swing entry; flattened by next 15:55)")
        else:
            print(f"  Overnight:    none — intraday, flat by 15:55 today")
        print(f"  Rationale:    {decision.get('rationale')}")
        rf = decision.get("risk_factors") or []
        if rf:
            print(f"  Risk factors: {rf}")
        if order_rec:
            print(f"\n  ORDER SENT → {order_rec['shares']} shares  |  "
                  f"Trade {order_rec.get('trades_today')} of 2 today")
        else:
            print("\n  ORDER NOT SENT (guard/limit/cutoff — see log above)")
    else:
        print(f"  AI DECISION:  ⛔ NO-GO")
        print(f"  Reason:       {decision.get('reject_reason') or decision.get('rationale')}")
    print(BAR, flush=True)


class Trader:
    def __init__(self, ib, dry_run: bool, allow_live: bool, mode: str):
        self.ib = ib
        self.dry_run = dry_run
        self.allow_live = allow_live
        self.mode = mode
        self._swept_date = None

    def on_scan(self, summary: Dict[str, Any]) -> None:
        """Scan-cycle heartbeat → status.json for the dashboard. Tags each top
        mover with whether the IB account holds it and whether it's bot-owned."""
        try:
            account_long = {}
            for p in self.ib.positions():
                c = p.contract
                if getattr(c, "secType", "STK") == "STK" and p.position != 0:
                    account_long[c.symbol] = int(p.position)
            bot_held = fill_logger.open_bot_positions()
            for m in summary.get("movers", []):
                sym = m.get("symbol")
                m["held"] = sym in account_long
                m["held_shares"] = account_long.get(sym)
                m["bot"] = sym in bot_held
            status_io.write_status({
                "running": True,
                "mode": self.mode,
                "pid": os.getpid(),
                "last_poll": summary.get("ts"),
                "session": summary.get("session"),
                "scanner": summary.get("scanner"),
                "movers": summary.get("movers", []),
                "held": [{"symbol": s, "shares": n, "bot": s in bot_held}
                         for s, n in sorted(account_long.items())],
                "triggered_today": summary.get("triggered_today", []),
            })
        except Exception:
            logger.exception("on_scan/status write failed")

    async def run_pipeline(self, trig: Trigger) -> None:
        symbol = trig.symbol
        logger.info("pipeline start %s", symbol)
        try:
            enrichment = await enrich(
                self.ib, symbol,
                prior_close_hint=trig.prior_close, last_price_hint=trig.last_price)

            chart_path = build_chart(
                symbol, prior_close=enrichment.get("prior_close"),
                pct_gain=enrichment.get("pct_gain_today"),
                trigger_time_et=trig.timestamp.astimezone(ET).strftime("%H:%M"))
            enrichment["_chart_path"] = chart_path

            trades_today = fill_logger.trades_today()
            decision = call_ai(enrichment, chart_path, trades_today)

            order_rec = None
            if decision.get("decision") == "GO" and not self.dry_run:
                order_rec = await submit_orders(self.ib, symbol, decision,
                                                allow_live=self.allow_live)
            elif decision.get("decision") == "GO" and self.dry_run:
                logger.info("[dry-run] would submit %d sh of %s",
                            shares_for(decision["entry_limit"]), symbol)

            _print_trigger_block(trig, enrichment, decision, order_rec)
        except Exception:
            logger.exception("pipeline failed for %s", symbol)

    async def forced_exit_loop(self) -> None:
        """Fire forced_exit_sweep once at 15:55 ET each day."""
        while True:
            now = datetime.now(ET)
            today = now.strftime("%Y-%m-%d")
            target = now.replace(hour=15, minute=55, second=0, microsecond=0)
            if now >= target and self._swept_date != today:
                if not self.dry_run:
                    forced_exit_sweep(self.ib, allow_live=self.allow_live)
                else:
                    logger.info("[dry-run] forced-exit sweep would fire now")
                self._swept_date = today
            await asyncio.sleep(20)


async def main_async(args) -> int:
    ib = await _connect_ib()
    if not _account_guard(ib, args.allow_live, args.dry_run):
        ib.disconnect()
        return 1

    mode = "DRY-RUN" if args.dry_run else ("LIVE" if args.allow_live else "PAPER")
    trader = Trader(ib, dry_run=args.dry_run, allow_live=args.allow_live, mode=mode)

    dash = None if args.no_dashboard else _start_dashboard(args.dashboard_host,
                                                           args.dashboard_port)

    monitor = TriggerMonitor(ib=None, on_trigger=trader.run_pipeline,
                             on_scan=trader.on_scan)
    # Scanner gets its own clientId/connection so it never contends with orders.
    await monitor.connect()

    tasks = [
        asyncio.create_task(monitor.run()),
        asyncio.create_task(trader.forced_exit_loop()),
    ]
    if not args.no_pm:
        pm = PolygonPMScanner(on_trigger=trader.run_pipeline, on_scan=trader.on_scan)
        tasks.append(asyncio.create_task(pm.run()))

    dash_line = (f"\n  Dashboard: http://{args.dashboard_host}:{args.dashboard_port}"
                 if dash is not None else "")
    print(f"\n{BAR}\n  AI Momentum Trader running — {mode} mode  (session: {session_now()})"
          f"{dash_line}\n  Ctrl-C to stop\n{BAR}\n", flush=True)

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        monitor.stop()
        for t in tasks:
            t.cancel()
        # Flag the dashboard as stopped (best-effort, preserve the last snapshot).
        last = status_io.read_status() or {}
        last["running"] = False
        last["mode"] = mode
        status_io.write_status(last)
        if dash is not None:
            dash.shutdown()
        await monitor.disconnect()
        if ib.isConnected():
            ib.disconnect()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Momentum Trader")
    ap.add_argument("--allow-live", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="run pipeline + AI but never place orders")
    ap.add_argument("--no-pm", action="store_true", help="disable PM/AH Polygon scanner")
    ap.add_argument("--no-dashboard", action="store_true",
                    help="don't serve the status dashboard")
    ap.add_argument("--dashboard-host", default="127.0.0.1")
    ap.add_argument("--dashboard-port", type=int, default=8787)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    quiet_scanner_cancel_noise()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
