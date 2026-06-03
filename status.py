"""
status.py — the bot's heartbeat file (status.json), read by dashboard.py.

The trader writes a fresh snapshot on every scan cycle; the dashboard polls the
file. Writes are atomic (temp file + os.replace) so the dashboard never reads a
half-written file. Dependency-free; safe to import anywhere.

Snapshot shape:
{
  "running": true,
  "mode": "DRY-RUN",
  "pid": 12345,
  "last_poll": "2026-06-03T08:14:31-04:00",   # ET ISO
  "session": "PRE-MARKET",
  "scanner": "Polygon PM" | "IB",
  "movers": [{"rank":1,"symbol":"DXST","pct":1.64,"last":4.2,"prior":1.6,
              "held":true,"held_shares":120,"bot":false}, ...],  # top 5
  "held":   [{"symbol":"CNSP","shares":100,"bot":false}, ...],   # all account longs
  "triggered_today": ["DXST","BJDX"]
}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

STATUS_PATH = Path(__file__).parent / "status.json"


def write_status(data: Dict[str, Any], path: Path = STATUS_PATH) -> None:
    """Atomically write the snapshot. Never raises into the caller."""
    try:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on the same filesystem
    except Exception:
        # The dashboard is best-effort; a status-write failure must never
        # disrupt the trading loop.
        pass


def read_status(path: Path = STATUS_PATH) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
