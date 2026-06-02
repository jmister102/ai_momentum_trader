"""
trigger_monitor.py — IB scanner subscription for ≥+100% intraday movers.

Replicates the saved "MOMENTUM_100" scanner programmatically (TOP_PERC_GAIN,
price ≥ 1.00, volume ≥ 50,000, US major exchanges) via reqScannerSubscription,
then for every returned symbol pulls live price + prior close and fires a
trigger when (last/prior_close − 1) ≥ trigger_pct.

Two ways to run:

  1. Standalone (verify the scanner the moment Gateway is logged in):
         py trigger_monitor.py                 # poll + print/log triggers, no trades
         py trigger_monitor.py --diagnose       # one scan, dump raw rows + gains
         py trigger_monitor.py --trigger 50      # lower bar to see it fire sooner
     This NEVER places orders — it only prints and writes triggers.jsonl.

  2. Imported by run_trader.py:
         mon = TriggerMonitor(ib, on_trigger=run_pipeline)
         await mon.run()

Debounce: each symbol fires at most once per UTC/ET day (triggered_today set).

Gotcha handled: TOP_PERC_GAIN is unreliable pre-market / after-hours and tends
to throw Error 162 (data farm). In those sessions, run polygon_pm_scanner.py
alongside this. See CLAUDE.md → "PM/AH Fallback".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

import pytz

try:
    from ib_insync import IB, ScannerSubscription, Ticker  # noqa: F401
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    IB = ScannerSubscription = Ticker = object  # type: ignore[assignment,misc]

import config

ET = pytz.timezone("America/New_York")
TRIGGERS_PATH = Path(__file__).parent / "triggers.jsonl"
logger = logging.getLogger("trigger_monitor")


class _ScannerCancelNoiseFilter(logging.Filter):
    """Drop the routine 'Error 162: API scanner subscription cancelled' that IB
    emits every scan cycle when reqScannerDataAsync tears down its one-shot
    snapshot. Genuine 162s (data-farm disconnects) are NOT suppressed."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("162" in msg and "scanner subscription cancelled" in msg.lower())


def quiet_scanner_cancel_noise() -> None:
    """Attach the filter to ib_insync's wrapper logger. Idempotent."""
    wrapper_log = logging.getLogger("ib_insync.wrapper")
    if not any(isinstance(f, _ScannerCancelNoiseFilter) for f in wrapper_log.filters):
        wrapper_log.addFilter(_ScannerCancelNoiseFilter())


@dataclass
class Trigger:
    symbol: str
    last_price: float
    prior_close: float
    pct_gain: float          # fraction, e.g. 1.47 == +147%
    timestamp: datetime
    session: str
    volume: Optional[float] = None
    rank: Optional[int] = None

    @property
    def entry_limit(self) -> float:
        """Entry is fixed at 2× prior close per the strategy spec."""
        return round(2.0 * self.prior_close, 4)


def session_now(now: Optional[datetime] = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    et = now.astimezone(ET).time()
    if dtime(4, 0) <= et < dtime(9, 30):
        return "PREMARKET"
    if dtime(9, 30) <= et < dtime(16, 0):
        return "REGULAR"
    if dtime(16, 0) <= et < dtime(20, 0):
        return "POSTMARKET"
    return "CLOSED"


# on_trigger may be sync or async; the monitor awaits coroutines.
TriggerHandler = Callable[[Trigger], Optional[Awaitable[None]]]


class TriggerMonitor:
    def __init__(
        self,
        ib: Optional[IB] = None,
        on_trigger: Optional[TriggerHandler] = None,
        trigger_pct: float = 1.00,       # +100%
        poll_interval: float = 30.0,
        scan_rows: int = 25,
        market_data_timeout: float = 4.0,
        client_id: Optional[int] = None,
        log_path: Path = TRIGGERS_PATH,
    ) -> None:
        if ib is None:
            if not _IB_AVAILABLE:
                raise ImportError("ib_insync required: py -m pip install ib_insync")
            ib = IB()
        self.ib = ib
        self.on_trigger = on_trigger
        self.trigger_pct = trigger_pct
        self.poll_interval = poll_interval
        self.scan_rows = scan_rows
        self.market_data_timeout = market_data_timeout
        self.log_path = log_path

        self.host = config.IB_HOST
        self.port = config.IB_PORT
        self.client_id = client_id if client_id is not None \
            else getattr(config, "IB_SCANNER_CLIENT_ID", 11)

        self.triggered_today: set[str] = set()
        self._tickers: Dict[str, Ticker] = {}
        self._day_key: Optional[str] = None
        self._stopping = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        if self.ib.isConnected():
            return
        logger.info("connecting host=%s port=%s clientId=%s",
                    self.host, self.port, self.client_id)
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        logger.info("connected — server v%s", self.ib.client.serverVersion())

    async def disconnect(self) -> None:
        for ticker in list(self._tickers.values()):
            try:
                self.ib.cancelMktData(ticker.contract)
            except Exception:
                pass
        self._tickers.clear()
        if self.ib.isConnected():
            self.ib.disconnect()

    def stop(self) -> None:
        self._stopping = True

    # ── scanning ───────────────────────────────────────────────────────────

    def _subscription(self) -> ScannerSubscription:
        return ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode="TOP_PERC_GAIN",
            abovePrice=1.0,
            aboveVolume=50_000,
            numberOfRows=self.scan_rows,
        )

    def _roll_day(self) -> None:
        """Clear the debounce set at the start of a new ET day."""
        key = datetime.now(ET).strftime("%Y-%m-%d")
        if key != self._day_key:
            self._day_key = key
            self.triggered_today.clear()
            logger.info("new ET day %s — debounce cleared", key)

    async def scan_once(self, diagnose: bool = False) -> List[Trigger]:
        self._roll_day()
        sub = self._subscription()
        try:
            rows = await asyncio.wait_for(self.ib.reqScannerDataAsync(sub), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("scanner timeout (data farm may be down / wrong session)")
            return []
        except Exception as e:
            logger.warning("scanner error: %s", e)
            return []

        if diagnose:
            print(f"\n  scanner returned {len(rows)} rows "
                  f"(session={session_now()}):")

        triggers: List[Trigger] = []
        for row in rows:
            try:
                contract = row.contractDetails.contract
            except AttributeError:
                continue
            symbol = contract.symbol
            if not symbol:
                continue

            last, prior, vol = await self._quote(contract)
            pct = (last / prior - 1.0) if (last and prior) else None

            if diagnose:
                rk = getattr(row, "rank", None)
                shown = f"+{pct:.0%}" if pct is not None else "  n/a"
                print(f"    #{rk:<2} {symbol:<6} last={last or '—':<8} "
                      f"prior={prior or '—':<8} {shown}")

            if pct is None or pct < self.trigger_pct:
                continue
            if symbol in self.triggered_today:
                continue

            trig = Trigger(
                symbol=symbol, last_price=last, prior_close=prior, pct_gain=pct,
                timestamp=datetime.now(timezone.utc), session=session_now(),
                volume=vol, rank=getattr(row, "rank", None),
            )
            self.triggered_today.add(symbol)
            self._log_trigger(trig)
            triggers.append(trig)
            await self._dispatch(trig)

        return triggers

    async def _quote(self, contract) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Live last, prior close, today's volume. Reuses streaming subscriptions."""
        symbol = contract.symbol
        ticker = self._tickers.get(symbol)
        if ticker is None:
            ticker = self.ib.reqMktData(contract, "", False, False)
            self._tickers[symbol] = ticker

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.market_data_timeout
        while loop.time() < deadline:
            if ticker.last and ticker.close and ticker.last > 0 and ticker.close > 0:
                break
            await asyncio.sleep(0.1)

        last = float(ticker.last) if ticker.last and ticker.last > 0 else None
        prior = float(ticker.close) if ticker.close and ticker.close > 0 else None
        vol = float(ticker.volume) if ticker.volume and ticker.volume > 0 else None
        return last, prior, vol

    async def _dispatch(self, trig: Trigger) -> None:
        if self.on_trigger is None:
            self._print_trigger(trig)
            return
        try:
            result = self.on_trigger(trig)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("on_trigger handler failed symbol=%s", trig.symbol)

    async def run(self, diagnose: bool = False) -> None:
        await self.connect()
        logger.info("scanning every %.0fs for ≥+%.0f%% movers — Ctrl-C to stop",
                    self.poll_interval, self.trigger_pct * 100)
        try:
            while not self._stopping:
                await self.scan_once(diagnose=diagnose)
                slept = 0.0
                while slept < self.poll_interval and not self._stopping:
                    await asyncio.sleep(0.5)
                    slept += 0.5
        finally:
            await self.disconnect()

    # ── output ───────────────────────────────────────────────────────────────

    def _log_trigger(self, trig: Trigger) -> None:
        rec = {
            "ts": trig.timestamp.astimezone(ET).isoformat(timespec="seconds"),
            "symbol": trig.symbol,
            "last_price": round(trig.last_price, 4),
            "prior_close": round(trig.prior_close, 4),
            "pct_gain": round(trig.pct_gain, 4),
            "entry_limit": trig.entry_limit,
            "volume": trig.volume,
            "session": trig.session,
            "rank": trig.rank,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()

    @staticmethod
    def _print_trigger(trig: Trigger) -> None:
        et = trig.timestamp.astimezone(ET).strftime("%H:%M")
        print(
            f"🚨 TRIGGER {trig.symbol:<6} +{trig.pct_gain:.0%}  "
            f"${trig.prior_close:.2f} → ${trig.last_price:.2f}  "
            f"{et} ET  ({trig.session})  entry_limit=${trig.entry_limit:.2f}",
            flush=True,
        )


# ── standalone entry point ────────────────────────────────────────────────────

async def _amain(args: argparse.Namespace) -> int:
    mon = TriggerMonitor(
        trigger_pct=args.trigger / 100.0,
        poll_interval=args.interval,
    )
    try:
        if args.diagnose:
            await mon.connect()
            await mon.scan_once(diagnose=True)
            print("\n  ✓ scanner responded. If rows appear above, the data farm "
                  "is connected.\n    (No ≥+%.0f%% movers required to confirm this.)"
                  % (args.trigger,))
            await mon.disconnect()
        else:
            await mon.run()
    except KeyboardInterrupt:
        mon.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="IB ≥+100% scanner (no trades)")
    p.add_argument("--trigger", type=float, default=100.0,
                   help="percent gain to fire (default 100)")
    p.add_argument("--interval", type=float, default=30.0,
                   help="seconds between scans (default 30)")
    p.add_argument("--diagnose", action="store_true",
                   help="single scan: dump raw rows + gains, then exit")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
    )
    quiet_scanner_cancel_noise()
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
