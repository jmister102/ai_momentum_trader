"""
account.py — IB account cash helpers (settled cash etc.).

The strategy sizes each trade at settled_cash / TRADE_DIVISOR (8) and caps the
day at TRADE_DIVISOR trades, so total deployment never exceeds settled cash —
important for a cash account (no trading on unsettled funds → no good-faith
violations).

`SettledCash` (IB account-summary tag) is exactly "cash recognized at settlement
less purchases at trade time, commissions, taxes, fees" — the right baseline.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

logger = logging.getLogger("account")

# Account-summary tags we care about (SettledCash drives sizing; the rest are
# shown by account_info.py for context).
SUMMARY_TAGS = ("AccountType,SettledCash,TotalCashValue,AvailableFunds,"
                "BuyingPower,NetLiquidation,CashBalance")

_PREFERRED_CCY = ("USD", "BASE", "")


async def account_summary_rows(ib) -> List:
    """List[AccountValue(account, tag, value, currency)] across all accounts."""
    try:
        await ib.reqAccountSummaryAsync()
    except TypeError:
        # Older ib_insync: sync request, then read the cache.
        ib.reqAccountSummary()
        await ib.sleep(2.5) if hasattr(ib, "sleep") else None
    return list(ib.accountSummary())


def value_for(rows: List, account: str, tag: str,
              currencies=_PREFERRED_CCY) -> Optional[float]:
    """First numeric value matching account+tag in a preferred currency."""
    # Pass 1: preferred currencies in order; Pass 2: any currency.
    for ccy in currencies:
        for v in rows:
            if v.account == account and v.tag == tag and v.currency == ccy:
                try:
                    return float(v.value)
                except (ValueError, TypeError):
                    return None
    for v in rows:
        if v.account == account and v.tag == tag:
            try:
                return float(v.value)
            except (ValueError, TypeError):
                return None
    return None


def by_account(rows: List) -> Dict[str, Dict[str, str]]:
    """{account: {tag: value}} for display (preferred-currency value per tag)."""
    out: Dict[str, Dict[str, str]] = {}
    for v in rows:
        acct = out.setdefault(v.account, {})
        # keep first preferred-currency hit per tag
        if v.tag not in acct and v.currency in _PREFERRED_CCY:
            acct[v.tag] = v.value
    # backfill any tags only present in other currencies
    for v in rows:
        out.setdefault(v.account, {}).setdefault(v.tag, v.value)
    return out


async def settled_cash(ib, account: str) -> Optional[float]:
    """Settled cash for one account, or None if unavailable."""
    rows = await account_summary_rows(ib)
    return value_for(rows, account, "SettledCash")
