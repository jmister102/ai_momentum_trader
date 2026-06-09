"""
account_info.py — print cash balances for every IB account.

Run this on Windows (with IB Gateway logged in) to find your Roth IRA account id
and its SETTLED CASH, which is the daily baseline the trader sizes against
(per-trade = settled_cash / 8, up to 8 trades/day).

    py account_info.py
    py account_info.py --client-id 88

Copy the Roth IRA account id (e.g. U2848700) into config.py as IB_ACCOUNT.
"""

from __future__ import annotations

import argparse
import asyncio

import account as acct
import config

DIVISOR = getattr(config, "SIZE_DIVISOR", 8)


async def _amain(client_id: int) -> int:
    try:
        from ib_insync import IB
    except ImportError:
        print("ib_insync not installed: py -m pip install ib_insync")
        return 1

    ib = IB()
    await ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=client_id)
    try:
        managed = ib.managedAccounts()
        rows = await acct.account_summary_rows(ib)
    finally:
        pass

    table = acct.by_account(rows)
    cfg_acct = getattr(config, "IB_ACCOUNT", "") or ""

    print(f"\nManaged accounts: {managed}")
    print(f"config IB_ACCOUNT = {cfg_acct or '(unset)'}\n")
    show = ("AccountType", "SettledCash", "TotalCashValue", "AvailableFunds",
            "BuyingPower", "NetLiquidation")
    for account in managed:
        tags = table.get(account, {})
        marker = "  ← config IB_ACCOUNT" if account == cfg_acct else ""
        print(f"━━━ {account}{marker} ━━━")
        for tag in show:
            print(f"  {tag:<16} {tags.get(tag, '—')}")
        sc = acct.value_for(rows, account, "SettledCash")
        if sc is not None:
            print(f"  {'→ per-trade':<16} {sc/DIVISOR:,.2f}   "
                  f"(settled_cash / {DIVISOR})")
        print()

    ib.disconnect()
    if len(managed) > 1 and not cfg_acct:
        print("⚠ Multiple accounts — set IB_ACCOUNT in config.py to the Roth IRA id "
              "so the bot trades the right one.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Print IB account cash balances")
    ap.add_argument("--client-id", type=int, default=88)
    args = ap.parse_args()
    try:
        return asyncio.run(_amain(args.client_id))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
