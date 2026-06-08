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


def _current_price(t: dict) -> float:
    """Best available 'now' price. In PRE-MARKET the regular-session `day.c` is 0
    until 09:30 — the live price sits in `min.c` (last minute bar) or lastTrade.
    Fall back through them so off-hours movers aren't invisible."""
    day = t.get("day") or {}
    mn = t.get("min") or {}
    lt = t.get("lastTrade") or {}
    return (day.get("c") or 0) or (mn.get("c") or 0) or (lt.get("p") or 0)


def _today_volume(t: dict) -> float:
    """Day volume, or the minute bar's accumulated volume (`av`, includes
    pre-market) when the regular-session volume is still 0."""
    day = t.get("day") or {}
    mn = t.get("min") or {}
    return (day.get("v") or 0) or (mn.get("av") or 0)


def find_matches(tickers: List[dict]) -> List[Trigger]:
    out: List[Trigger] = []
    now = datetime.now(timezone.utc)
    for t in tickers:
        prev = t.get("prevDay") or {}
        last = _current_price(t)
        prior = prev.get("c") or 0
        vol = _today_volume(t)
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


def top_movers(tickers: List[dict], n: int = 5) -> List[dict]:
    """Top-n % gainers from the snapshot, regardless of the 2× trigger — this is
    the '% change list' shown on the dashboard during PM/AH."""
    rows = []
    for t in tickers:
        prev = t.get("prevDay") or {}
        last = _current_price(t)
        prior = prev.get("c") or 0
        if prior <= 0 or last <= 0:
            continue
        rows.append({"symbol": t.get("ticker", "?"), "pct": round(last / prior - 1.0, 4),
                     "last": last, "prior": prior})
    rows.sort(key=lambda r: r["pct"], reverse=True)
    for i, r in enumerate(rows[:n], 1):
        r["rank"] = i
    return rows[:n]


# on_scan reports each scan cycle (heartbeat + top movers) to the UI.
ScanHandler = Callable[[dict], None]


class PolygonPMScanner:
    def __init__(self, on_trigger: Optional[TriggerHandler] = None,
                 on_scan: Optional[ScanHandler] = None,
                 poll_interval: float = 60.0):
        self.on_trigger = on_trigger
        self.on_scan = on_scan
        self.poll_interval = poll_interval
        self.triggered_today: set[str] = set()
        self._day_key: Optional[str] = None
        self._stopping = False
        self.last_poll: Optional[datetime] = None

    def stop(self) -> None:
        self._stopping = True

    def _emit_scan(self, movers: List[dict]) -> None:
        self.last_poll = datetime.now(timezone.utc)
        if not self.on_scan:
            return
        try:
            self.on_scan({
                "ts": self.last_poll.astimezone().isoformat(timespec="seconds"),
                "session": session_now(),
                "scanner": "Polygon PM",
                "movers": movers,
                "triggered_today": sorted(self.triggered_today),
            })
        except Exception:
            logger.exception("on_scan handler failed")

    def _roll_day(self) -> None:
        key = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        if key != self._day_key:
            self._day_key = key
            self.triggered_today.clear()

    async def scan_once(self, verbose: bool = False) -> List[Trigger]:
        self._roll_day()
        snapshot = fetch_snapshot()
        matches = find_matches(snapshot)
        self._emit_scan(top_movers(snapshot))
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
