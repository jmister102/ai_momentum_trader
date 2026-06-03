"""
decision_log.py — persist EVERY AI decision (GO and NO-GO, dry-run included) to
decisions.jsonl, so the nightly retrospective can score how the AI did.

Without this, NO-GO and dry-run decisions only existed in the console log and
couldn't be evaluated. One JSON object per line; the trigger price + prior close
are stored alongside the decision so the retrospective needs nothing else.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz

ET = pytz.timezone("America/New_York")
DECISIONS_PATH = Path(__file__).parent / "decisions.jsonl"


def log_decision(
    symbol: str,
    trigger_ts_iso: str,
    session: str,
    last_price: Optional[float],
    prior_close: Optional[float],
    decision: Dict[str, Any],
    mode: str,
    path: Path = DECISIONS_PATH,
) -> Dict[str, Any]:
    rec = {
        "ts": trigger_ts_iso,           # trigger time, ET ISO
        "symbol": symbol,
        "session": session,
        "last_price": last_price,       # price at trigger (entry reference)
        "prior_close": prior_close,
        "decision": decision.get("decision"),
        "confidence": decision.get("confidence"),
        "entry_limit": decision.get("entry_limit"),
        "target_1": decision.get("target_1_price"),
        "target_1_pct_shares": decision.get("target_1_pct_shares"),
        "target_2": decision.get("target_2_price"),
        "stop": decision.get("stop_loss_price"),
        "overnight_hold_pct": decision.get("overnight_hold_pct", 0),
        "rationale": decision.get("rationale"),
        "risk_factors": decision.get("risk_factors"),
        "reject_reason": decision.get("reject_reason"),
        "mode": mode,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")
        fh.flush()
    return rec


def _et_date(ts: str) -> Optional[str]:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = ET.localize(dt)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def read_for_date(date_str: str, path: Path = DECISIONS_PATH) -> List[Dict[str, Any]]:
    """All decisions whose ET trigger date == date_str."""
    out: List[Dict[str, Any]] = []
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
            if _et_date(rec.get("ts", "")) == date_str:
                out.append(rec)
    return out
