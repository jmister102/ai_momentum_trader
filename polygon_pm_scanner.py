"""
polygon_pm_scanner.py — pre-market / after-hours fallback scanner.

IB's TOP_PERC_GAIN scanner is unreliable outside 09:30–16:00 ET (Error 162).
During 04:00–09:29 and 16:00–20:00 ET this polls the Polygon full-market
snapshot every 60s and fires the same pipeline on ≥2× movers.

Match filter (per CLAUDE.md):
  day.c / prevDay.c ≥ 2.0  AND  prevDay.c ≥ 1.00  AND  day.v × day.c ≥ 200,000

Emits trigger_monitor.Trigger objects so it can share run_trader's pipeline.

Standalone:
    py polygon_pm_scanner.py                 # poll + print, no trades
    py polygon_pm_scanner.py --once           # single snapshot, print matches
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional

import requests

import config
from trigger_monitor import Trigger, session_now

logger = logging.getLogger("pm_scanner")
SNAPSHOT_URL = ("https://api.polygon.io/v2/snapshot/locale/us/markets/"
                "stocks/tickers")

MIN_GAIN_MULT = 2.0
MIN_PRIOR_CLOSE = 1.00
MIN_DOLLAR_VOL = 200_000

TriggerHandler = Callable[[Trigger], Optional[Awaitable[None]]]


def fetch_snapshot() -> List[dict]:
    try:
        r = requests.get(SNAPSHOT_URL,
                         params={"include_otc": "false", "apiKey": config.POLYGON_API_KEY},
                         timeout=15)
        if r.status_code != 200:
            logger.warning("snapshot → HTTP %s", r.status_code)
            return []
        return r.json().get("tickers", []) or []
    except Exception as e:
        logger.warning("snapshot failed: %s", e)
        return []


def find_matches(tickers: List[dict]) -> List[Trigger]:
    out: List[Trigger] = []
    now = datetime.now(timezone.utc)
    for t in tickers:
        day = t.get("day") or {}
        prev = t.get("prevDay") or {}
        last = day.get("c") or 0
        prior = prev.get("c") or 0
        vol = day.get("v") or 0
        if prior < MIN_PRIOR_CLOSE or last <= 0:
            continue
        if last / prior < MIN_GAIN_MULT:
            continue
        if vol * last < MIN_DOLLAR_VOL:
            continue
        out.append(Trigger(
            symbol=t.get("ticker", "?"), last_price=last, prior_close=prior,
            pct_gain=last / prior - 1.0, timestamp=now,
            session=session_now(now), volume=vol,
        ))
    out.sort(key=lambda x: x.pct_gain, reverse=True)
    return out


class PolygonPMScanner:
    def __init__(self, on_trigger: Optional[TriggerHandler] = None,
                 poll_interval: float = 60.0):
        self.on_trigger = on_trigger
        self.poll_interval = poll_interval
        self.triggered_today: set[str] = set()
        self._day_key: Optional[str] = None
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def _roll_day(self) -> None:
        key = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        if key != self._day_key:
            self._day_key = key
            self.triggered_today.clear()

    async def scan_once(self, verbose: bool = False) -> List[Trigger]:
        self._roll_day()
        matches = find_matches(fetch_snapshot())
        fired = []
        for trig in matches:
            if verbose:
                print(f"  {trig.symbol:<6} +{trig.pct_gain:.0%}  "
                      f"${trig.prior_close:.2f}→${trig.last_price:.2f}")
            if trig.symbol in self.triggered_today:
                continue
            self.triggered_today.add(trig.symbol)
            fired.append(trig)
            if self.on_trigger:
                res = self.on_trigger(trig)
                if asyncio.iscoroutine(res):
                    await res
            else:
                print(f"🚨 PM/AH TRIGGER {trig.symbol} +{trig.pct_gain:.0%} "
                      f"entry_limit=${trig.entry_limit:.2f}", flush=True)
        return fired

    async def run(self) -> None:
        logger.info("PM/AH scanner polling every %.0fs", self.poll_interval)
        while not self._stopping:
            sess = session_now()
            if sess in ("PREMARKET", "POSTMARKET"):
                await self.scan_once()
            else:
                logger.debug("session=%s — PM scanner idle (IB scanner is primary)", sess)
            slept = 0.0
            while slept < self.poll_interval and not self._stopping:
                await asyncio.sleep(0.5)
                slept += 0.5


def main() -> int:
    ap = argparse.ArgumentParser(description="Polygon PM/AH fallback scanner (no trades)")
    ap.add_argument("--once", action="store_true", help="single snapshot, print matches")
    ap.add_argument("--interval", type=float, default=60.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s  %(message)s")
    scanner = PolygonPMScanner(poll_interval=args.interval)
    if args.once:
        print(f"snapshot session={session_now()}:")
        matches = asyncio.run(scanner.scan_once(verbose=True))
        print(f"\n{len(matches)} match(es) ≥2× with $vol ≥ ${MIN_DOLLAR_VOL:,}")
        return 0
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
