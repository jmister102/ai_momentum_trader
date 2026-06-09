"""
blocklist.py — symbols IBKR refuses to let us OPEN (compliance / closing-only).

A meaningful share of +100% small-cap movers are blocked at IBKR with Error 201
("No Opening Trades: Small Cap, Subject to Compliance Restriction" or closing-only
status). These restrictions are persistent per-symbol and are NOT distinguishable
from tradable names in the IB/Polygon contract metadata (both label them NASDAQ),
so we can't pre-filter by exchange — we learn from the reject instead.

When an entry is rejected for one of these reasons, the symbol is added here; the
pipeline then skips it on every future trigger (across restarts and days), saving
the wasted enrich/AI/order churn. Delete blocklist.json to reset (e.g. if IBKR
lifts a restriction).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import pytz

ET = pytz.timezone("America/New_York")
BLOCKLIST_PATH = Path(__file__).parent / "blocklist.json"


def _load(path: Path = BLOCKLIST_PATH) -> Dict[str, dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def is_blocked(symbol: str, path: Path = BLOCKLIST_PATH) -> bool:
    return symbol in _load(path)


def reason(symbol: str, path: Path = BLOCKLIST_PATH) -> Optional[str]:
    return (_load(path).get(symbol) or {}).get("reason")


def add(symbol: str, reason: Optional[str] = None, path: Path = BLOCKLIST_PATH) -> None:
    """Record a symbol IBKR won't let us open. Idempotent (keeps first-seen date)."""
    data = _load(path)
    if symbol not in data:
        data[symbol] = {
            "reason": (reason or "")[:300],
            "since": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        }
        try:
            path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass


def all_blocked(path: Path = BLOCKLIST_PATH) -> Dict[str, dict]:
    return _load(path)
