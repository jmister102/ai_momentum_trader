"""
enricher.py — gather market context for a triggered symbol before the AI call.

Pulls live quote + fundamentals from IB and ADV + news from Polygon. Returns a
plain dict (see FIELDS below). Every field is best-effort: anything that fails
to fetch becomes None with a logged warning — the pipeline never crashes over a
missing metric.

Standalone test (IB Gateway must be running + logged in):
    py enricher.py AAPL
    py enricher.py HKIT --client-id 77
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

try:
    from ib_insync import IB, Stock
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    IB = Stock = object  # type: ignore[assignment,misc]

import config

logger = logging.getLogger("enricher")
POLYGON_BASE = "https://api.polygon.io"

# Generic tick list: 165=misc stats (52w hi/lo), 233=RTVolume, 236=shortable.
# PREV_CLOSE arrives as ticker.close on a snapshot.
_GENERIC_TICKS = "165,233"


# ── Polygon helpers ───────────────────────────────────────────────────────────

def _poly_get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[dict]:
    params = dict(params or {})
    params["apiKey"] = config.POLYGON_API_KEY
    try:
        r = requests.get(f"{POLYGON_BASE}{path}", params=params, timeout=10)
        if r.status_code != 200:
            logger.warning("polygon %s → HTTP %s", path, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.warning("polygon %s failed: %s", path, e)
        return None


def adv_20_dollar(symbol: str) -> Optional[float]:
    """20-trading-day average dollar volume: mean(close × volume)."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=40)  # calendar buffer for weekends/holidays
    data = _poly_get(
        f"/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}",
        {"adjusted": "true", "sort": "desc", "limit": 25},
    )
    if not data or not data.get("results"):
        return None
    bars = data["results"][:20]
    dollar_vols = [b["c"] * b["v"] for b in bars if b.get("c") and b.get("v")]
    if not dollar_vols:
        return None
    return sum(dollar_vols) / len(dollar_vols)


def prior_close_polygon(symbol: str) -> Optional[float]:
    """Fallback prior close when IB's PREV_CLOSE tick returns 0."""
    data = _poly_get(f"/v2/aggs/ticker/{symbol}/prev", {"adjusted": "true"})
    if data and data.get("results"):
        return data["results"][0].get("c")
    return None


def market_cap_polygon(symbol: str) -> Optional[float]:
    """Fallback market cap from Polygon ticker details (IB fundamentals often
    require a paid subscription — Error 10358 'Fundamentals data is not allowed')."""
    data = _poly_get(f"/v3/reference/tickers/{symbol}")
    if data and data.get("results"):
        res = data["results"]
        return res.get("market_cap") or None
    return None


def news_headlines(symbol: str, limit: int = 3) -> list[dict]:
    """Last N headlines: title + published timestamp only."""
    data = _poly_get("/v2/reference/news",
                     {"ticker": symbol, "limit": limit, "order": "desc"})
    out = []
    if data and data.get("results"):
        for art in data["results"][:limit]:
            out.append({
                "title": art.get("title"),
                "published": art.get("published_utc"),
                "publisher": (art.get("publisher") or {}).get("name"),
            })
    return out


# ── IB fundamental XML parsing ─────────────────────────────────────────────────

def _parse_fundamental(xml: str) -> Dict[str, Optional[float]]:
    """Pull MKTCAP and SharesOut from a ReportSnapshot XML blob (best-effort)."""
    out: Dict[str, Optional[float]] = {"market_cap": None, "shares_outstanding": None}
    if not xml:
        return out
    # <Ratio FieldName="MKTCAP" ...>123.4</Ratio>  (value in millions)
    m = re.search(r'FieldName="MKTCAP"[^>]*>([\d.eE+-]+)<', xml)
    if m:
        try:
            out["market_cap"] = float(m.group(1)) * 1_000_000
        except ValueError:
            pass
    # SharesOut appears as a <SharesOut ...>NNN</SharesOut> element.
    m = re.search(r'<SharesOut[^>]*>([\d.eE+,-]+)<', xml)
    if m:
        try:
            out["shares_outstanding"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return out


# ── main enrichment ─────────────────────────────────────────────────────────

async def enrich(
    ib: IB,
    symbol: str,
    prior_close_hint: Optional[float] = None,
    last_price_hint: Optional[float] = None,
) -> Dict[str, Any]:
    """Build the enrichment dict for `symbol`. Hints come from the trigger."""
    result: Dict[str, Any] = {
        "symbol": symbol,
        "last_price": last_price_hint,
        "ask": None,           # entry is priced off the ask (see ai_analyst.entry_for)
        "prior_close": prior_close_hint,
        "pct_gain_today": None,
        "volume_today": None,
        "adv_20": None,
        "vol_adv_ratio": None,
        "market_cap": None,
        "shares_outstanding": None,
        "high_52w": None,
        "low_52w": None,
        "sector": None,
        "news_headlines": [],
        "entry_limit": None,
    }

    contract = Stock(symbol, "SMART", "USD")
    try:
        await ib.qualifyContractsAsync(contract)
    except Exception as e:
        logger.warning("qualify failed for %s: %s", symbol, e)

    # ── live quote (snapshot) ──
    try:
        ticker = ib.reqMktData(contract, _GENERIC_TICKS, False, False)
        for _ in range(40):  # up to ~4s
            await asyncio.sleep(0.1)
            if ticker.last and ticker.close:
                break
        if ticker.last and ticker.last > 0:
            result["last_price"] = float(ticker.last)
        if getattr(ticker, "ask", None) and ticker.ask > 0:
            result["ask"] = float(ticker.ask)
        if ticker.close and ticker.close > 0:
            result["prior_close"] = float(ticker.close)
        if ticker.volume and ticker.volume > 0:
            # ib_insync already reports volume in shares (not round lots) — an
            # extra ×100 here inflated vol/ADV by 100× (e.g. 18× → 1837×).
            result["volume_today"] = float(ticker.volume)
        if getattr(ticker, "high52week", None):
            result["high_52w"] = float(ticker.high52week)
        if getattr(ticker, "low52week", None):
            result["low_52w"] = float(ticker.low52week)
        ib.cancelMktData(contract)
    except Exception as e:
        logger.warning("market data failed for %s: %s", symbol, e)

    # ── prior close fallback ──
    if not result["prior_close"]:
        result["prior_close"] = prior_close_polygon(symbol)

    # ── sector / industry ──
    try:
        details = await ib.reqContractDetailsAsync(contract)
        if details:
            result["sector"] = getattr(details[0], "industry", None) \
                or getattr(details[0], "category", None)
    except Exception as e:
        logger.warning("contract details failed for %s: %s", symbol, e)

    # ── fundamentals (market cap, shares out) ──
    try:
        xml = await ib.reqFundamentalDataAsync(contract, "ReportSnapshot")
        fund = _parse_fundamental(xml or "")
        result.update({k: v for k, v in fund.items() if v is not None})
    except Exception as e:
        logger.warning("fundamentals failed for %s: %s", symbol, e)

    # ── Polygon: ADV + news ──
    result["adv_20"] = adv_20_dollar(symbol)
    result["news_headlines"] = news_headlines(symbol)

    # Polygon market-cap fallback when IB fundamentals are unavailable.
    if result["market_cap"] is None:
        result["market_cap"] = market_cap_polygon(symbol)

    # ── derived ──
    pc = result["prior_close"]
    lp = result["last_price"]
    if pc and lp:
        result["pct_gain_today"] = (lp - pc) / pc
    if pc:
        result["entry_limit"] = round(2.0 * pc, 4)
    if result["adv_20"] and result["volume_today"] and lp:
        today_dollar = result["volume_today"] * lp
        result["vol_adv_ratio"] = today_dollar / result["adv_20"]

    return result


async def _amain(symbol: str, client_id: int) -> int:
    if not _IB_AVAILABLE:
        print("ib_insync not installed: py -m pip install ib_insync")
        return 1
    ib = IB()
    await ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=client_id)
    try:
        data = await enrich(ib, symbol.upper())
    finally:
        ib.disconnect()
    print(json.dumps(data, indent=2, default=str))
    missing = [k for k, v in data.items() if v is None]
    if missing:
        print(f"\n⚠ missing fields: {', '.join(missing)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Enrich a symbol with market context")
    ap.add_argument("symbol")
    ap.add_argument("--client-id", type=int, default=77)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s  %(message)s")
    return asyncio.run(_amain(args.symbol, args.client_id))


if __name__ == "__main__":
    raise SystemExit(main())
