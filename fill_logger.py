"""
fill_logger.py — append-only event log for the AI Momentum Trader.

Every trade event (entry submission, exit fill, forced exit) is written as one
JSON object per line to fills.jsonl. This is the single source of truth for:
  • the daily trade-count safety guard (order_router.py)
  • the P&L report (perf.py)

It is intentionally dependency-free and crash-safe: each write opens, appends,
flushes and closes, so a killed process never corrupts earlier records.

CLI (manual "what-if" fill entry during paper validation):
    py fill_logger.py entry --symbol HKIT --entry-limit 2.74 --shares 72 \
        --target-1 3.29 --stop 1.37 --confidence 0.78
    py fill_logger.py exit  --symbol HKIT --kind target_1 --price 3.29 \
        --shares 36 --pnl 39.60 --pnl-pct 0.20
    py fill_logger.py count          # entries logged today
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz

ET = pytz.timezone("America/New_York")
FILLS_PATH = Path(__file__).parent / "fills.jsonl"

# Event names that count as "opening a position today" for the daily limit.
ENTRY_EVENTS = {"entry_submitted", "entry_fill"}
EXIT_KINDS = {"target_1", "target_2", "stop", "trail", "timeout_forced"}


def _now_et_iso() -> str:
    return datetime.now(ET).isoformat(timespec="seconds")


def _append(record: Dict[str, Any], path: Path = FILLS_PATH) -> Dict[str, Any]:
    """Append one record as a JSON line. Returns the record (with ts filled)."""
    record.setdefault("ts", _now_et_iso())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()
    return record


def read_all(path: Path = FILLS_PATH) -> List[Dict[str, Any]]:
    """Read every record. Skips blank / malformed lines without crashing."""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"⚠ skipping malformed fills.jsonl line: {line[:80]}",
                      file=sys.stderr)
    return out


def _et_date(ts: str) -> Optional[str]:
    """Return the ET calendar date (YYYY-MM-DD) for an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def trades_today(path: Path = FILLS_PATH, now: Optional[datetime] = None) -> int:
    """Net entries today (submitted minus rejected/cancelled-without-fill) — drives
    the daily trade guard. A rejected order (e.g. a closing-only symbol) never
    opened a position, so it doesn't burn a trade slot."""
    if now is None:
        now = datetime.now(ET)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    entries = rejects = 0
    for rec in read_all(path):
        if _et_date(rec.get("ts", "")) != today:
            continue
        ev = rec.get("event")
        if ev in ENTRY_EVENTS:
            entries += 1
        elif ev == "entry_rejected":
            rejects += 1
    return max(0, entries - rejects)


def symbols_entered_today(path: Path = FILLS_PATH,
                          now: Optional[datetime] = None) -> set:
    """Set of symbols the bot opened an entry for today (ET).

    Used by the 15:55 forced-exit sweep so it only flattens positions the bot
    itself created — never the operator's manually-placed positions/orders.
    """
    if now is None:
        now = datetime.now(ET)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    out = set()
    for rec in read_all(path):
        if rec.get("event") in ENTRY_EVENTS and _et_date(rec.get("ts", "")) == today:
            sym = rec.get("symbol")
            if sym:
                out.add(sym)
    return out


def bot_net_shares_today(path: Path = FILLS_PATH,
                         now: Optional[datetime] = None) -> Dict[str, int]:
    """{symbol: net shares the bot still holds today} = entered − exited.

    Caps the 15:55 forced-exit sell so it can never sell more than the bot put
    on — protecting any shares of the same symbol the operator holds manually.

    NOTE: 'entered' counts the ordered quantity (entry_submitted). Until a live
    fill handler logs actual entry_fill quantities, a partial fill could make
    this an over-estimate of true bot holdings; the sweep still also caps at the
    real IB position, so it never sells more shares than actually exist.
    """
    if now is None:
        now = datetime.now(ET)
    today = now.astimezone(ET).strftime("%Y-%m-%d")
    net: Dict[str, int] = {}
    for rec in read_all(path):
        if _et_date(rec.get("ts", "")) != today:
            continue
        sym = rec.get("symbol")
        if not sym:
            continue
        shares = int(rec.get("shares") or 0)
        ev = rec.get("event")
        if ev in ENTRY_EVENTS:
            net[sym] = net.get(sym, 0) + shares
        elif ev == "exit_fill":
            net[sym] = net.get(sym, 0) - shares
    return {s: n for s, n in net.items() if n > 0}


def open_bot_positions(path: Path = FILLS_PATH) -> Dict[str, Dict[str, Any]]:
    """All symbols the bot still holds (net entries − exits > 0), across ALL days,
    each tagged with the controlling (latest) entry's metadata.

    Drives the 15:55 sweep so it can tell intraday entries (flatten today) from
    swing entries (carry overnight_hold_pct) from prior-day holds (next-day
    backstop flatten). Net shares span every day; metadata comes from the most
    recent entry for that symbol.
    """
    net: Dict[str, int] = {}
    latest_entry: Dict[str, Dict[str, Any]] = {}
    for rec in read_all(path):
        sym = rec.get("symbol")
        if not sym:
            continue
        ev = rec.get("event")
        shares = int(rec.get("shares") or 0)
        if ev in ENTRY_EVENTS:
            net[sym] = net.get(sym, 0) + shares
            prev = latest_entry.get(sym)
            if prev is None or rec.get("ts", "") >= prev.get("ts", ""):
                latest_entry[sym] = rec
        elif ev == "exit_fill":
            net[sym] = net.get(sym, 0) - shares

    out: Dict[str, Dict[str, Any]] = {}
    for sym, n in net.items():
        if n <= 0:
            continue
        e = latest_entry.get(sym, {})
        out[sym] = {
            "net_shares": n,
            "entry_date": _et_date(e.get("ts", "")),
            "session": e.get("session", "intraday"),
            "overnight_hold_pct": int(e.get("overnight_hold_pct") or 0),
            "original_shares": int(e.get("shares") or n),
            "stop": e.get("stop"),
            "target_1": e.get("target_1"),
            "target_2": e.get("target_2"),
        }
    return out


# ── public write helpers ─────────────────────────────────────────────────────

def log_entry(
    symbol: str,
    entry_limit: float,
    shares: int,
    target_1: float,
    stop: float,
    ai_confidence: Optional[float] = None,
    ai_rationale: Optional[str] = None,
    target_2: Optional[float] = None,
    event: str = "entry_submitted",
    extra: Optional[Dict[str, Any]] = None,
    path: Path = FILLS_PATH,
) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "symbol": symbol,
        "event": event,
        "entry_limit": round(float(entry_limit), 4),
        "shares": int(shares),
        "target_1": round(float(target_1), 4),
        "stop": round(float(stop), 4),
        "ai_confidence": ai_confidence,
        "ai_rationale": ai_rationale,
        "trades_today": None,  # filled below so the record is self-describing
    }
    if target_2 is not None:
        rec["target_2"] = round(float(target_2), 4)
    if extra:
        rec.update(extra)
    written = _append(rec, path)
    # Recount AFTER writing so the logged value reflects this entry inclusively.
    written["trades_today"] = trades_today(path)
    return written


def log_exit(
    symbol: str,
    exit_kind: str,
    exit_price: float,
    shares: int,
    pnl_dollars: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
    path: Path = FILLS_PATH,
) -> Dict[str, Any]:
    if exit_kind not in EXIT_KINDS:
        print(f"⚠ unknown exit_kind '{exit_kind}' (allowed: {sorted(EXIT_KINDS)})",
              file=sys.stderr)
    rec: Dict[str, Any] = {
        "symbol": symbol,
        "event": "exit_fill",
        "exit_kind": exit_kind,
        "exit_price": round(float(exit_price), 4),
        "shares": int(shares),
        "pnl_dollars": round(float(pnl_dollars), 2) if pnl_dollars is not None else None,
        "pnl_pct": round(float(pnl_pct), 4) if pnl_pct is not None else None,
    }
    if extra:
        rec.update(extra)
    return _append(rec, path)


def log_event(event: str, **fields: Any) -> Dict[str, Any]:
    """Generic escape hatch for ad-hoc events (e.g. 'entry_rejected')."""
    rec = {"event": event}
    rec.update(fields)
    return _append(rec)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Append events to fills.jsonl")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("entry", help="log a manual entry")
    e.add_argument("--symbol", required=True)
    e.add_argument("--entry-limit", type=float, required=True)
    e.add_argument("--shares", type=int, required=True)
    e.add_argument("--target-1", type=float, required=True)
    e.add_argument("--stop", type=float, required=True)
    e.add_argument("--target-2", type=float, default=None)
    e.add_argument("--confidence", type=float, default=None)
    e.add_argument("--rationale", default=None)

    x = sub.add_parser("exit", help="log a manual exit fill")
    x.add_argument("--symbol", required=True)
    x.add_argument("--kind", required=True, choices=sorted(EXIT_KINDS))
    x.add_argument("--price", type=float, required=True)
    x.add_argument("--shares", type=int, required=True)
    x.add_argument("--pnl", type=float, default=None)
    x.add_argument("--pnl-pct", type=float, default=None)

    sub.add_parser("count", help="print today's entry count")

    args = p.parse_args(argv)

    if args.cmd == "entry":
        rec = log_entry(
            symbol=args.symbol, entry_limit=args.entry_limit, shares=args.shares,
            target_1=args.target_1, stop=args.stop, target_2=args.target_2,
            ai_confidence=args.confidence, ai_rationale=args.rationale,
        )
        print(json.dumps(rec, indent=2))
    elif args.cmd == "exit":
        rec = log_exit(
            symbol=args.symbol, exit_kind=args.kind, exit_price=args.price,
            shares=args.shares, pnl_dollars=args.pnl, pnl_pct=args.pnl_pct,
        )
        print(json.dumps(rec, indent=2))
    elif args.cmd == "count":
        print(trades_today())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
