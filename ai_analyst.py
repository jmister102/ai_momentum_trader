"""
ai_analyst.py — the decision core. Hands full context to Claude Sonnet and
gets back a structured GO / NO-GO entry decision with exit levels.

No hard-coded entry/exit logic lives here beyond the non-negotiable guardrails
that are *validated* after the model responds (entry = 2× prior close,
target ≥ +20%, stop ≥ −50%). If the model violates a guardrail or returns
malformed JSON, the decision is forced to NO-GO. One shot per trigger — no retry.

Standalone test (uses a baked-in sample enrichment dict):
    py ai_analyst.py
    py ai_analyst.py --raw       # print the raw model text before parsing
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, Optional

import pytz

import config

logger = logging.getLogger("ai_analyst")
ET = pytz.timezone("America/New_York")

MAX_ENTRY_DISTANCE = 0.05   # entry_limit must be within ±5% of current price
MIN_TARGET_MULT = 1.20      # target_1 ≥ entry × 1.20
MIN_STOP_MULT = 0.50        # stop ≥ entry × 0.50

SYSTEM_PROMPT = """\
You are an expert intraday trading analyst specializing in small-cap momentum stocks.
You will be given data about a stock that has just triggered a +100% gain alert.
Your job is to make a binary GO / NO-GO entry decision and, if GO, specify exact exit levels.

By the time this alert fires, the stock is already well above 2× its prior close —
chasing it blindly is how you get trapped at the top. Be selective.

You may trade in pre-market and post-market as well as regular hours. Extended-hours
liquidity is thinner, so weigh spread/fill risk — but do not refuse a trade merely
because the regular session is closed.

HARD RULES YOU MUST FOLLOW (non-negotiable):
- Trade size is fixed at $200 regardless of your confidence.
- Entry: choose an entry_limit at or just below the CURRENT price so a limit BUY
  fills promptly. It MUST be within 5% of the current price (no more than 5% above
  and no more than 5% below "Current price"). Do NOT anchor to 2× prior close.
- Limit sell targets must represent at least +20% gain from your entry_limit.
- Stop loss must be no worse than -50% from your entry_limit (can be tighter).
- HOLDING WINDOW (depends on entry time, given as "Entry session" below):
    • intraday (entered BEFORE 12:00 ET): position is fully closed at 15:55 ET
      the same day. overnight_hold_pct MUST be 0.
    • swing-eligible (entered AT/AFTER 12:00 ET): you MAY carry part or all of the
      position overnight. Set overnight_hold_pct (0-100) = the % of shares to hold
      past today's close; the rest is sold into the close. Anything carried is
      protected by a GTC stop and force-closed by the NEXT day's 15:55 ET sweep.
- Max 2 trades per day — if you see "trades_today: 2" you MUST output NO-GO.

Respond ONLY with a valid JSON object. No preamble, no explanation outside the JSON.
Schema:
{
  "decision": "GO" | "NO-GO",
  "confidence": 0.0-1.0,
  "entry_limit": <float, within 5% of the current price>,
  "target_1_price": <float, ≥ entry × 1.20>,
  "target_1_pct_shares": <int, 25-75>,
  "target_2_price": <float | null, > target_1 if present>,
  "target_2_pct_shares": <int | null>,
  "stop_loss_price": <float, ≥ entry × 0.50>,
  "trail_after_target_1": <bool>,
  "overnight_hold_pct": <int 0-100, MUST be 0 for an intraday entry>,
  "rationale": "<2-3 sentences explaining the key factors driving the decision>",
  "risk_factors": ["<factor 1>", "<factor 2>"],
  "reject_reason": "<string if NO-GO, else null>"
}\
"""

USER_PROMPT_TEMPLATE = """\
TRIGGER ALERT — {symbol}

## Market Context
- Current price: ${last_price}   ← anchor your entry_limit within ±5% of THIS
- Prior close: ${prior_close}
- Gain today: +{pct_gain_today}
- 2× prior close: ${entry_limit}  (the trigger level — informational only, NOT the entry)
- Volume today: {volume_today} shares
- 20-day avg dollar volume (ADV): ${adv_20}
- Today's volume vs ADV ratio: {vol_adv_ratio}

## Fundamentals
- Market cap: {market_cap}
- Shares outstanding: {shares_outstanding}
- Sector: {sector}
- 52-week high: {high_52w}
- 52-week low: {low_52w}

## Recent News (last 3 headlines)
{news_block}

## Chart Summary
5-day 15-min chart saved to: {chart_path}
Price action description: stock was trading near ${prior_close} yesterday close,
opened today and has surged +{pct_gain_today} intraday to ${last_price}.
It is already {price_vs_entry} 2× prior close — you are entering an extended move,
so weigh how much room is left vs. reversal risk before committing.

## Session Context
- Trades taken today: {trades_today} / 2 max
- Current ET time: {et_time}  ({clock_session})
- Extended hours: pre-market (04:00–09:30) and post-market (16:00–20:00) entries
  ARE allowed and orders are routed to fill outside regular hours. Liquidity and
  spreads are thinner then — factor that into the setup, but it is NOT a reason
  to auto-decline.
- Entry session: {entry_session}
- Exit timing: {forced_exit_desc}
"""


def _fmt(v: Any, kind: str = "num") -> str:
    """Render a possibly-None value for the prompt without crashing on None."""
    if v is None:
        return "unknown"
    if kind == "pct":
        return f"{v*100:.1f}%"
    if kind == "money":
        return f"{v:,.0f}"
    if kind == "price":
        return f"{v:.2f}"
    if kind == "ratio":
        return f"{v:.1f}x"
    if kind == "int":
        return f"{int(v):,}"
    return str(v)


NOON_ET = dtime(12, 0)


def is_swing_entry(now_et: datetime) -> bool:
    """True if entering at/after 12:00 ET (overnight carry allowed)."""
    return now_et.timetz().replace(tzinfo=None) >= NOON_ET


def _minutes_to_close(now_et: datetime) -> int:
    close = now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    return max(0, int((close - now_et).total_seconds() // 60))


def clock_session(now_et: datetime) -> str:
    """The current trading session by ET clock (independent of the noon rule)."""
    t = now_et.timetz().replace(tzinfo=None)
    if dtime(4, 0) <= t < dtime(9, 30):
        return "PRE-MARKET"
    if dtime(9, 30) <= t < dtime(16, 0):
        return "REGULAR HOURS"
    if dtime(16, 0) <= t < dtime(20, 0):
        return "POST-MARKET"
    return "CLOSED"


def _forced_exit_dt(now_et: datetime, swing: bool) -> datetime:
    """When this entry must be flat. Intraday → today 15:55; swing → the next
    trading day's 15:55 (the overnight backstop). Weekends roll to Monday."""
    if not swing:
        return now_et.replace(hour=15, minute=55, second=0, microsecond=0)
    nxt = now_et + timedelta(days=1)
    while nxt.weekday() >= 5:  # Sat=5, Sun=6
        nxt += timedelta(days=1)
    return nxt.replace(hour=15, minute=55, second=0, microsecond=0)


def _forced_exit_desc(now_et: datetime, swing: bool) -> str:
    deadline = _forced_exit_dt(now_et, swing)
    hrs = max(0.0, (deadline - now_et).total_seconds() / 3600.0)
    when = deadline.strftime("%a %H:%M ET")
    if swing:
        return (f"shares NOT carried are sold into the nearest regular close; "
                f"carried shares (overnight_hold_pct) are held until {when} "
                f"(~{hrs:.0f}h from now), the hard backstop")
    return f"ALL shares flat by {when} (~{hrs:.0f}h from now)"


def build_user_prompt(
    enrichment: Dict[str, Any],
    chart_path: Optional[str],
    trades_today: int,
    now_et: Optional[datetime] = None,
) -> str:
    if now_et is None:
        now_et = datetime.now(ET)
    e = enrichment
    last = e.get("last_price")
    entry = e.get("entry_limit")
    price_vs_entry = "unknown vs"
    if last and entry:
        price_vs_entry = "above" if last >= entry else "below"

    news = e.get("news_headlines") or []
    if news:
        news_block = "\n".join(
            f"- \"{n.get('title')}\" ({n.get('published','')})" for n in news)
    else:
        news_block = "- (no recent headlines found)"

    return USER_PROMPT_TEMPLATE.format(
        symbol=e.get("symbol", "?"),
        last_price=_fmt(last, "price"),
        prior_close=_fmt(e.get("prior_close"), "price"),
        pct_gain_today=_fmt(e.get("pct_gain_today"), "pct"),
        entry_limit=_fmt(entry, "price"),
        volume_today=_fmt(e.get("volume_today"), "int"),
        adv_20=_fmt(e.get("adv_20"), "money"),
        vol_adv_ratio=_fmt(e.get("vol_adv_ratio"), "ratio"),
        market_cap=_fmt(e.get("market_cap"), "money") if e.get("market_cap") else "unknown",
        shares_outstanding=_fmt(e.get("shares_outstanding"), "int"),
        sector=e.get("sector") or "unknown",
        high_52w=_fmt(e.get("high_52w"), "price"),
        low_52w=_fmt(e.get("low_52w"), "price"),
        news_block=news_block,
        chart_path=chart_path or "(chart unavailable)",
        price_vs_entry=price_vs_entry,
        trades_today=trades_today,
        et_time=now_et.strftime("%H:%M:%S %Z"),
        clock_session=clock_session(now_et),
        entry_session=("swing-eligible (after 12:00 ET — overnight_hold_pct may be 0-100)"
                       if is_swing_entry(now_et)
                       else "intraday (before 12:00 ET — overnight_hold_pct MUST be 0)"),
        forced_exit_desc=_forced_exit_desc(now_et, is_swing_entry(now_et)),
    )


def _no_go(reason: str, raw: Optional[str] = None) -> Dict[str, Any]:
    return {
        "decision": "NO-GO", "confidence": 0.0, "entry_limit": None,
        "target_1_price": None, "target_1_pct_shares": None,
        "target_2_price": None, "target_2_pct_shares": None,
        "stop_loss_price": None, "trail_after_target_1": False,
        "overnight_hold_pct": 0,
        "rationale": reason, "risk_factors": [], "reject_reason": reason,
        "_raw": raw,
    }


def _validate(decision: Dict[str, Any], last_price: Optional[float],
              now_et: Optional[datetime] = None) -> Dict[str, Any]:
    """Enforce the non-negotiable guardrails. Demote to NO-GO on any breach."""
    if decision.get("decision") != "GO":
        return decision
    if not last_price:
        return _no_go("validation: current price unknown, cannot verify entry")

    entry = decision.get("entry_limit")
    t1 = decision.get("target_1_price")
    stop = decision.get("stop_loss_price")

    # Entry must sit within ±MAX_ENTRY_DISTANCE of the current price so the limit
    # fills promptly and we don't chase far above market or rest far below it.
    if entry is None or abs(entry - last_price) > last_price * MAX_ENTRY_DISTANCE + 1e-6:
        lo = last_price * (1 - MAX_ENTRY_DISTANCE)
        hi = last_price * (1 + MAX_ENTRY_DISTANCE)
        return _no_go(f"validation: entry_limit {entry} outside ±{MAX_ENTRY_DISTANCE:.0%} "
                      f"of current {last_price:.2f} (allowed {lo:.2f}–{hi:.2f})",
                      raw=decision.get("_raw"))
    if t1 is None or t1 < entry * MIN_TARGET_MULT - 1e-6:
        return _no_go(f"validation: target_1 {t1} < entry×1.20 ({entry*MIN_TARGET_MULT:.2f})",
                      raw=decision.get("_raw"))
    if stop is None or stop < entry * MIN_STOP_MULT - 1e-6:
        return _no_go(f"validation: stop {stop} < entry×0.50 ({entry*MIN_STOP_MULT:.2f})",
                      raw=decision.get("_raw"))
    t2 = decision.get("target_2_price")
    if t2 is not None and t2 <= t1:
        return _no_go(f"validation: target_2 {t2} must be > target_1 {t1}",
                      raw=decision.get("_raw"))

    # Overnight carry: clamp to 0-100, and force 0 for intraday (before-noon)
    # entries — they must be flat by today's 15:55 regardless of what the AI said.
    pct = decision.get("overnight_hold_pct") or 0
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        pct = 0
    pct = max(0, min(100, pct))
    if now_et is not None and not is_swing_entry(now_et):
        pct = 0
    decision["overnight_hold_pct"] = pct
    return decision


def call_ai(
    enrichment: Dict[str, Any],
    chart_path: Optional[str],
    trades_today: int,
    now_et: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build prompt → call Claude → parse → validate. Always returns a dict."""
    if now_et is None:
        now_et = datetime.now(ET)
    # Pre-empt the daily limit without spending a token.
    if trades_today >= 2:
        return _no_go("trades_today is 2 — daily limit, forced NO-GO")

    user_prompt = build_user_prompt(enrichment, chart_path, trades_today, now_et)

    try:
        import anthropic
    except ImportError:
        return _no_go("anthropic SDK not installed")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=getattr(config, "AI_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=800,
            temperature=0,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Cache the fixed system prompt — it's identical on every trigger.
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        logger.exception("Anthropic call failed")
        return _no_go(f"AI call failed: {e}")

    raw = "".join(block.text for block in resp.content
                  if getattr(block, "type", None) == "text").strip()

    # Strip a markdown fence if the model wrapped the JSON.
    text = raw
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        decision = json.loads(text)
    except json.JSONDecodeError:
        logger.error("AI returned non-JSON:\n%s", raw)
        return _no_go("AI returned malformed JSON", raw=raw)

    decision["_raw"] = raw
    return _validate(decision, enrichment.get("last_price"), now_et)


# ── standalone test ───────────────────────────────────────────────────────────

_SAMPLE = {
    "symbol": "HKIT", "last_price": 6.79, "prior_close": 2.74,
    "pct_gain_today": 1.478, "entry_limit": 5.48, "volume_today": 3_400_000,
    "adv_20": 412_000, "vol_adv_ratio": 8.3, "market_cap": 28_000_000,
    "shares_outstanding": 9_500_000, "sector": "Technology",
    "high_52w": 9.10, "low_52w": 1.20,
    "news_headlines": [
        {"title": "Company announces FDA clearance", "published": "2026-06-01T07:32:00Z"},
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Test the AI analyst with a sample dict")
    ap.add_argument("--raw", action="store_true", help="print raw model text too")
    ap.add_argument("--trades-today", type=int, default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    decision = call_ai(_SAMPLE, "charts/HKIT_sample.png", args.trades_today)
    if args.raw and decision.get("_raw"):
        print("─── RAW ───")
        print(decision["_raw"])
        print("───────────")
    decision.pop("_raw", None)
    print(json.dumps(decision, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
