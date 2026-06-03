"""
retrospective.py — end-of-day review of the AI's decisions.

For every name the AI ruled on today (from decisions.jsonl), it pulls the
post-trigger price action from Polygon, simulates what the decision would have
produced, scores it, writes a human-readable report, and asks Claude to distill
concrete lessons. Lessons land in playbook_pending.md (REVIEW-GATED) — they only
influence live trading after you approve them into playbook.md.

This is in-context learning, not weight training: the approved playbook is fed
into future decision prompts (see ai_analyst._load_playbook).

Usage:
    py retrospective.py                 # review today (ET), write report + pending lessons
    py retrospective.py --date 2026-06-03
    py retrospective.py --no-ai          # score + report only, skip the Claude lesson step
    py retrospective.py --approve        # merge playbook_pending.md → playbook.md (then clears pending)

Scheduled nightly at 21:00 via retrospective.ps1 + Task Scheduler.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
import requests

import config
import decision_log

logger = logging.getLogger("retrospective")
ET = pytz.timezone("America/New_York")
HERE = Path(__file__).parent
REPORTS_DIR = HERE / "reports"
PLAYBOOK = HERE / "playbook.md"
PLAYBOOK_PENDING = HERE / "playbook_pending.md"

TARGET_GAIN = 0.20    # NO-GO "missed winner" threshold
AVOID_LOSS = 0.20     # NO-GO "correctly avoided" threshold


# ── Polygon ───────────────────────────────────────────────────────────────────

def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_minute_bars(symbol: str, start: datetime, end: datetime) -> List[dict]:
    """1-minute bars between two tz-aware datetimes (uses ms timestamps)."""
    url = (f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/minute/"
           f"{_ms(start)}/{_ms(end)}")
    try:
        r = requests.get(url, params={"adjusted": "true", "sort": "asc",
                                      "limit": 50000, "apiKey": config.POLYGON_API_KEY},
                         timeout=20)
        if r.status_code != 200:
            logger.warning("bars %s → HTTP %s", symbol, r.status_code)
            return []
        return r.json().get("results", []) or []
    except Exception as e:
        logger.warning("bars %s failed: %s", symbol, e)
        return []


# ── windows ───────────────────────────────────────────────────────────────────

def _next_trading_day(d: datetime) -> datetime:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def window_for(trigger_et: datetime) -> datetime:
    """Holding-window end: today 15:55 for an intraday (pre-noon) trigger, next
    trading day 15:55 for a swing (>= noon) trigger — matches the live rule."""
    is_swing = trigger_et.timetz().replace(tzinfo=None) >= dtime(12, 0)
    base = _next_trading_day(trigger_et) if is_swing else trigger_et
    return base.replace(hour=15, minute=55, second=0, microsecond=0)


# ── simulation ──────────────────────────────────────────────────────────────

def simulate_go(bars: List[dict], entry_limit: float, target_1: float,
                stop: float) -> Dict[str, Any]:
    """First-touch sim of a GO. Ignores scale-out (scores on first target/stop)."""
    fill_idx = next((i for i, b in enumerate(bars) if b["l"] <= entry_limit), None)
    if fill_idx is None:
        return {"filled": False, "score": "NO_FILL"}
    mfe = mae = entry_limit
    exit_kind, exit_price = "time", bars[-1]["c"]
    for b in bars[fill_idx:]:
        mfe = max(mfe, b["h"])
        mae = min(mae, b["l"])
        hit_t = b["h"] >= target_1
        hit_s = b["l"] <= stop
        if hit_s and hit_t:           # same bar straddles both → assume stop (conservative)
            exit_kind, exit_price = "stop", stop
            break
        if hit_t:
            exit_kind, exit_price = "target_1", target_1
            break
        if hit_s:
            exit_kind, exit_price = "stop", stop
            break
    pnl = exit_price / entry_limit - 1.0
    score = "WIN" if (exit_kind == "target_1" or (exit_kind == "time" and pnl > 0)) else \
            ("SCRATCH" if abs(pnl) < 1e-6 else "LOSS")
    return {"filled": True, "exit_kind": exit_kind, "exit_price": round(exit_price, 4),
            "pnl_pct": round(pnl, 4), "mfe_pct": round(mfe / entry_limit - 1, 4),
            "mae_pct": round(mae / entry_limit - 1, 4), "score": score}


def evaluate_nogo(bars: List[dict], trigger_price: float) -> Dict[str, Any]:
    """Counterfactual for a NO-GO: did avoiding it dodge a loss or miss a run?"""
    up, down = trigger_price * (1 + TARGET_GAIN), trigger_price * (1 - AVOID_LOSS)
    mfe = mae = trigger_price
    verdict = "NEUTRAL"
    for b in bars:
        mfe = max(mfe, b["h"])
        mae = min(mae, b["l"])
        if b["h"] >= up:
            verdict = "MISS"          # would have hit +20% → NO-GO arguably wrong
            break
        if b["l"] <= down:
            verdict = "AVOID"         # cratered first → NO-GO correct
            break
    return {"score": verdict, "mfe_pct": round(mfe / trigger_price - 1, 4),
            "mae_pct": round(mae / trigger_price - 1, 4)}


def evaluate(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Pull bars for one decision and score it."""
    sym = rec["symbol"]
    try:
        trig_et = datetime.fromisoformat(rec["ts"])
        if trig_et.tzinfo is None:
            trig_et = ET.localize(trig_et)
    except (ValueError, TypeError):
        return {"score": "NO_DATA", "note": "bad timestamp"}
    bars = fetch_minute_bars(sym, trig_et, window_for(trig_et))
    bars = [b for b in bars if b.get("t", 0) >= _ms(trig_et)]  # only post-trigger
    if not bars:
        return {"score": "NO_DATA", "note": "no post-trigger bars"}

    if rec.get("decision") == "GO" and rec.get("entry_limit"):
        return simulate_go(bars, float(rec["entry_limit"]),
                           float(rec["target_1"]), float(rec["stop"]))
    return evaluate_nogo(bars, float(rec.get("last_price") or bars[0]["o"]))


# ── report ────────────────────────────────────────────────────────────────────

def build_report(date_str: str, rows: List[Dict[str, Any]]) -> str:
    go = [r for r in rows if r["dec"].get("decision") == "GO"]
    nogo = [r for r in rows if r["dec"].get("decision") == "NO-GO"]
    go_scored = [r for r in go if r["out"].get("score") in ("WIN", "LOSS", "SCRATCH")]
    wins = [r for r in go_scored if r["out"]["score"] == "WIN"]
    nogo_dec = [r for r in nogo if r["out"].get("score") in ("AVOID", "MISS")]
    avoids = [r for r in nogo_dec if r["out"]["score"] == "AVOID"]
    whatif = sum(r["out"].get("pnl_pct", 0) or 0 for r in go_scored)

    L = [f"# AI Momentum Trader — Retrospective {date_str}", ""]
    L.append(f"Names reviewed: {len(rows)}  |  GO: {len(go)}  |  NO-GO: {len(nogo)}")
    if go_scored:
        L.append(f"GO win rate: {len(wins)}/{len(go_scored)} "
                 f"({100*len(wins)/len(go_scored):.0f}%)  |  "
                 f"summed what-if return: {whatif*100:+.1f}%")
    if nogo_dec:
        L.append(f"NO-GO accuracy: {len(avoids)}/{len(nogo_dec)} correctly avoided "
                 f"({100*len(avoids)/len(nogo_dec):.0f}%)")
    L += ["", "| Symbol | Decision | Conf | Outcome | Exit | P&L% | MFE | MAE | Rationale |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        d, o = r["dec"], r["out"]
        L.append("| {sym} | {dec} | {conf} | {score} | {ek} | {pnl} | {mfe} | {mae} | {why} |".format(
            sym=r["symbol"], dec=d.get("decision"),
            conf=f"{d.get('confidence')}" if d.get("confidence") is not None else "—",
            score=o.get("score", "—"), ek=o.get("exit_kind", "—"),
            pnl=f"{o['pnl_pct']*100:+.0f}%" if o.get("pnl_pct") is not None else "—",
            mfe=f"{o['mfe_pct']*100:+.0f}%" if o.get("mfe_pct") is not None else "—",
            mae=f"{o['mae_pct']*100:+.0f}%" if o.get("mae_pct") is not None else "—",
            why=(d.get("rationale") or d.get("reject_reason") or "")[:80].replace("|", "/")))
    return "\n".join(L) + "\n"


# ── Claude lesson distillation ─────────────────────────────────────────────────

LESSON_SYSTEM = """\
You are a trading-desk reviewer improving an AI's intraday small-cap momentum
decisions. You are given today's GO/NO-GO decisions and what actually happened.
Extract 3-7 CONCRETE, GENERALIZABLE lessons that would improve future decisions —
each tied to evidence from today, phrased as an actionable rule (not a platitude).
Prefer rules about when to GO vs NO-GO, entry/stop/target sizing, and which
setups (vol/ADV, catalyst, extension, session) worked or failed. Output a short
markdown bullet list only."""


def distill_lessons(date_str: str, rows: List[Dict[str, Any]]) -> Optional[str]:
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic not installed — skipping lesson distillation")
        return None
    lines = []
    for r in rows:
        d, o = r["dec"], r["out"]
        lines.append(
            f"- {r['symbol']}: {d.get('decision')} (conf {d.get('confidence')}); "
            f"entry {d.get('entry_limit')}, T1 {d.get('target_1')}, stop {d.get('stop')}, "
            f"overnight {d.get('overnight_hold_pct')}%; rationale: {d.get('rationale') or d.get('reject_reason')}; "
            f"OUTCOME: {o.get('score')} exit={o.get('exit_kind')} "
            f"pnl={o.get('pnl_pct')} mfe={o.get('mfe_pct')} mae={o.get('mae_pct')}")
    user = f"Date {date_str}. Decisions and outcomes:\n" + "\n".join(lines)
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=getattr(config, "AI_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=700, temperature=0,
            system=LESSON_SYSTEM, messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text").strip()
    except Exception as e:
        logger.warning("lesson distillation failed: %s", e)
        return None


def append_pending(date_str: str, lessons: str) -> None:
    with PLAYBOOK_PENDING.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## Pending from {date_str}\n{lessons}\n")
    logger.info("pending lessons written → %s (review, then --approve)", PLAYBOOK_PENDING.name)


def approve() -> int:
    """Merge pending lessons into the active playbook, then clear pending."""
    if not PLAYBOOK_PENDING.exists() or not PLAYBOOK_PENDING.read_text(encoding="utf-8").strip():
        print("No pending lessons to approve.")
        return 0
    pending = PLAYBOOK_PENDING.read_text(encoding="utf-8").strip()
    stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    with PLAYBOOK.open("a", encoding="utf-8") as fh:
        fh.write(f"\n<!-- approved {stamp} -->\n{pending}\n")
    PLAYBOOK_PENDING.write_text("", encoding="utf-8")
    print(f"✓ approved pending lessons → {PLAYBOOK.name}; pending cleared.")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────

def run(date_str: str, use_ai: bool) -> int:
    decisions = decision_log.read_for_date(date_str)
    if not decisions:
        print(f"No decisions recorded for {date_str} — nothing to review.")
        return 0
    logger.info("reviewing %d decision(s) for %s", len(decisions), date_str)

    rows = []
    for d in decisions:
        out = evaluate(d)
        rows.append({"symbol": d["symbol"], "dec": d, "out": out})
        logger.info("  %-6s %-5s → %s", d["symbol"], d.get("decision"), out.get("score"))

    report = build_report(date_str, rows)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"retro_{date_str}.md"
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nReport saved → {report_path}")

    if use_ai:
        lessons = distill_lessons(date_str, rows)
        if lessons:
            append_pending(date_str, lessons)
            print(f"\nPending lessons (review then `py retrospective.py --approve`):\n{lessons}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="End-of-day AI decision retrospective")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today ET)")
    ap.add_argument("--no-ai", action="store_true", help="skip Claude lesson distillation")
    ap.add_argument("--approve", action="store_true",
                    help="merge playbook_pending.md into playbook.md")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s  %(message)s")

    if args.approve:
        return approve()
    date_str = args.date or datetime.now(ET).strftime("%Y-%m-%d")
    return run(date_str, use_ai=not args.no_ai)


if __name__ == "__main__":
    raise SystemExit(main())
