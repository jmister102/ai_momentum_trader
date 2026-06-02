"""
perf.py — P&L report from fills.jsonl.

    py perf.py
    py perf.py --since 2026-06-01

A "trade" is one entry event. Its P&L is the sum of all exit_fill records for
that symbol on that ET date. A trade is "fully closed" when its exited share
count reaches the entry share count. Win/loss is decided on summed P&L.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

import pytz

import fill_logger

ET = pytz.timezone("America/New_York")


def _date_of(rec: Dict[str, Any]) -> Optional[str]:
    ts = rec.get("ts", "")
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def build_report(records: List[Dict[str, Any]], since: Optional[str]) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    exits_by_key: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)

    for rec in records:
        d = _date_of(rec)
        if since and (d is None or d < since):
            continue
        ev = rec.get("event")
        if ev in fill_logger.ENTRY_EVENTS:
            rec["_date"] = d
            entries.append(rec)
        elif ev == "exit_fill":
            exits_by_key[(rec.get("symbol"), d)].append(rec)

    trades = []
    for e in entries:
        key = (e.get("symbol"), e.get("_date"))
        exs = exits_by_key.get(key, [])
        pnl = sum(x.get("pnl_dollars") or 0.0 for x in exs)
        exited_shares = sum(x.get("shares") or 0 for x in exs)
        entry_shares = e.get("shares") or 0
        fully_closed = exited_shares >= entry_shares > 0
        # entry cost basis for pct
        cost = (e.get("entry_limit") or 0) * entry_shares
        pnl_pct = (pnl / cost) if cost else None
        trades.append({
            "symbol": e.get("symbol"), "date": e.get("_date"),
            "pnl": pnl, "pnl_pct": pnl_pct, "fully_closed": fully_closed,
            "exits": exs,
        })

    closed = [t for t in trades if t["fully_closed"]]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross = sum(t["pnl"] for t in closed)
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)

    exit_breakdown: Dict[str, int] = defaultdict(int)
    for t in closed:
        for x in t["exits"]:
            exit_breakdown[x.get("exit_kind", "?")] += 1

    return {
        "entries": len(entries),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "gross": gross,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_win_pct": (sum(t["pnl_pct"] or 0 for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "avg_loss_pct": (sum(t["pnl_pct"] or 0 for t in losses) / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf"),
        "largest_win": max(closed, key=lambda t: t["pnl"], default=None),
        "largest_loss": min(closed, key=lambda t: t["pnl"], default=None),
        "exit_breakdown": dict(exit_breakdown),
        "first_date": min((t["date"] for t in trades if t["date"]), default=None),
        "last_date": max((t["date"] for t in trades if t["date"]), default=None),
    }


def _fmt_trade(t: Optional[Dict[str, Any]]) -> str:
    if not t:
        return "—"
    pct = f" ({t['pnl_pct']*100:+.0f}%)" if t.get("pnl_pct") is not None else ""
    return f"{t['symbol']}  {t['pnl']:+.0f}{pct}"


def print_report(r: Dict[str, Any]) -> None:
    print("=== AI Momentum Trader Performance ===")
    print(f"Period: {r['first_date'] or '—'} to {r['last_date'] or '—'}")
    print(f"Trades: {r['entries']} entries, {r['closed']} fully closed")
    if r["closed"]:
        print(f"Win rate: {r['win_rate']*100:.1f}% ({r['wins']}W / {r['losses']}L)")
    print(f"Gross P&L: {r['gross']:+.2f}")
    print(f"Avg winner: {r['avg_win']:+.2f} ({r['avg_win_pct']*100:+.1f}%)")
    print(f"Avg loser:  {r['avg_loss']:+.2f} ({r['avg_loss_pct']*100:+.1f}%)")
    pf = r["profit_factor"]
    print(f"Profit factor: {'∞' if pf == float('inf') else f'{pf:.2f}'}")
    print(f"Largest win:  {_fmt_trade(r['largest_win'])}")
    print(f"Largest loss: {_fmt_trade(r['largest_loss'])}")
    if r["exit_breakdown"]:
        print("\nExit breakdown:")
        for kind, n in sorted(r["exit_breakdown"].items()):
            print(f"  {kind+':':<16} {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="P&L report from fills.jsonl")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD")
    args = ap.parse_args()
    records = fill_logger.read_all()
    if not records:
        print("No fills recorded yet (fills.jsonl is empty).")
        return 0
    print_report(build_report(records, args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
