"""
triggered.py — persistent intraday debounce shared by both scanners.

A ticker that has already fired the pipeline today must not fire again — even if
the service restarts (which used to reset the in-memory set and re-trigger the
same names) and even if the OTHER scanner sees it. Both scanners load today's
triggered symbols from triggers.jsonl on each day-roll and record new ones there,
so the debounce survives restarts and spans IB + Polygon.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz

ET = pytz.timezone("America/New_York")
TRIGGERS_PATH = Path(__file__).parent / "triggers.jsonl"


def _et_date(ts: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def symbols_today(path: Path = TRIGGERS_PATH, now: Optional[datetime] = None) -> set:
    """Set of symbols already triggered today (ET), read from triggers.jsonl."""
    if now is None:
        now = datetime.now(ET)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    out: set = set()
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            sym = rec.get("symbol")
            if sym and _et_date(rec.get("ts", "")) == today:
                out.add(sym)
    return out


def record(symbol: str, extra: Optional[dict] = None,
           path: Path = TRIGGERS_PATH, now: Optional[datetime] = None) -> None:
    """Append a trigger to triggers.jsonl so the debounce persists. (The IB monitor
    also writes a richer record via its own _log_trigger; this is for the PM
    scanner and any caller that just needs the symbol persisted.)"""
    if now is None:
        now = datetime.now(ET)
    rec = {"ts": now.isoformat(timespec="seconds"), "symbol": symbol}
    if extra:
        rec.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
