"""
order_router.py — submit IB bracket orders on a GO decision, and run the
15:55 ET forced-exit sweep.

Order structure (per CLAUDE.md):
  • Parent     : LMT BUY  @ entry_limit, tif=DAY, transmit=False
  • OCA group  : attached, transmits with the parent
      - LMT SELL @ target_1_price for target_1_pct_shares% of shares
      - STP SELL @ stop_loss_price for 100% of shares
      - (optional) LMT SELL @ target_2_price for the residual after target_1
  • If trail_after_target_1: once target_1 fills, the residual is replaced with a
    TRAIL order at a 15% offset (handled by attach_trail_on_target_fill()).

Holding window (set by entry time vs 12:00 ET):
  • intraday (before noon) : DAY brackets, flattened at today's 15:55 sweep.
  • swing-eligible (≥ noon) : GTC brackets; at 15:55 the position is sold down to
    the AI's overnight_hold_pct of original size and the carry re-armed on a GTC
    stop. Carried shares are force-closed by the NEXT day's 15:55 sweep.

Safety: every submission is gated by the account guard, the 2-trade/day limit,
and (intraday only) a "too late to enter" cutoff (≥30 min before 15:55). Swing
entries may enter right up to the close.

This module places REAL orders against whatever IB session it's connected to.
It refuses to run against a live account unless allow_live=True is passed.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, time as dtime
from typing import Any, Dict, List, Optional

import pytz

try:
    from ib_insync import IB, Stock, LimitOrder, StopOrder, MarketOrder, Order
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    IB = Stock = LimitOrder = StopOrder = MarketOrder = Order = object  # type: ignore

import config
import fill_logger

logger = logging.getLogger("order_router")
ET = pytz.timezone("America/New_York")

FORCED_EXIT = dtime(15, 55)
NOON_ET = dtime(12, 0)
MIN_MINUTES_TO_ENTER = 30   # intraday only; swing entries have no same-day exit need
TRAIL_PERCENT = 15.0
# Per-trade $ size = settled_cash / TRADE_DIVISOR; at most TRADE_DIVISOR trades/day,
# so total deployment ≤ settled cash (cash account: no trading on unsettled funds).
MAX_TRADES_PER_DAY = int(getattr(config, "TRADE_DIVISOR", 8))


def _is_swing(now_et: datetime) -> bool:
    """Entered at/after 12:00 ET → overnight carry allowed."""
    return now_et.timetz().replace(tzinfo=None) >= NOON_ET


def shares_for(entry_limit: float, dollar_size: float) -> int:
    """floor(dollar_size / entry) — never more. dollar_size = settled_cash/8."""
    return int(math.floor(dollar_size / entry_limit)) if entry_limit > 0 else 0


def _minutes_to_close(now_et: Optional[datetime] = None) -> int:
    if now_et is None:
        now_et = datetime.now(ET)
    close = now_et.replace(hour=FORCED_EXIT.hour, minute=FORCED_EXIT.minute,
                           second=0, microsecond=0)
    return int((close - now_et).total_seconds() // 60)


def _is_live_account(ib: IB) -> bool:
    return any(a and not a.startswith("DU") for a in ib.managedAccounts())


def _acct() -> str:
    """The configured trading account id ('' = the connection default)."""
    return getattr(config, "IB_ACCOUNT", "") or ""


async def submit_orders(
    ib: IB,
    symbol: str,
    decision: Dict[str, Any],
    dollar_size: float,
    now_et: Optional[datetime] = None,
    allow_live: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Validate guards, build the bracket, transmit. Returns the logged entry
    record on success, or None if the order was rejected (reason logged).

    dollar_size = settled_cash / TRADE_DIVISOR (the per-trade ceiling).
    """
    if decision.get("decision") != "GO":
        logger.info("not a GO for %s — nothing to submit", symbol)
        return None

    # ── account guard ──
    if _is_live_account(ib) and not allow_live:
        logger.error("LIVE account detected and allow_live=False — refusing to trade %s",
                     symbol)
        return None

    if dollar_size <= 0:
        logger.warning("per-trade size is %.2f (settled cash unknown?) — rejecting %s",
                       dollar_size, symbol)
        return None

    # ── daily limit (TRADE_DIVISOR trades/day) ──
    n = fill_logger.trades_today()
    if n >= MAX_TRADES_PER_DAY:
        logger.warning("Daily trade limit reached (%d/%d) — rejecting %s",
                       n, MAX_TRADES_PER_DAY, symbol)
        return None

    if now_et is None:
        now_et = datetime.now(ET)
    swing = _is_swing(now_et)
    session = "swing" if swing else "intraday"
    child_tif = "GTC" if swing else "DAY"  # GTC so swing protection survives overnight

    # Outside-RTH flag: a limit/stop won't fill in pre/post market without it.
    _t = now_et.timetz().replace(tzinfo=None)
    outside_rth = _t < dtime(9, 30) or _t >= dtime(16, 0)

    # ── time cutoff ── only for intraday entries, which must exit same day.
    # Swing-eligible (after-noon) entries can enter right up to the close — that's
    # the whole point of not eliminating late-day entries.
    mins = _minutes_to_close(now_et)
    if not swing and mins < MIN_MINUTES_TO_ENTER:
        logger.warning("Too late for intraday entry %s — %d min to forced exit",
                       symbol, mins)
        return None

    entry = float(decision["entry_limit"])
    qty = shares_for(entry, dollar_size)
    if qty < 1:
        logger.warning("entry %.2f too high for $%.0f per-trade size — 0 shares, skipping %s",
                       entry, dollar_size, symbol)
        return None

    t1 = float(decision["target_1_price"])
    t1_pct = int(decision["target_1_pct_shares"])
    stop = float(decision["stop_loss_price"])
    t2 = decision.get("target_2_price")
    qty_t1 = max(1, int(round(qty * t1_pct / 100.0)))
    qty_t1 = min(qty_t1, qty)
    qty_residual = qty - qty_t1

    contract = Stock(symbol, "SMART", "USD")
    await ib.qualifyContractsAsync(contract)

    oca_group = f"OCA_{symbol}_{int(_minutes_to_close(now_et))}"
    acct = getattr(config, "IB_ACCOUNT", "") or ""   # target the configured account

    # Parent buy — hold transmit until all children are attached.
    parent = LimitOrder("BUY", qty, round(entry, 2),
                        orderId=ib.client.getReqId(), tif="DAY", transmit=False,
                        outsideRth=outside_rth, account=acct)

    children: List[Order] = []

    # Target 1 (partial), OCA.
    tgt1 = LimitOrder("SELL", qty_t1, round(t1, 2),
                      orderId=ib.client.getReqId(), tif=child_tif,
                      parentId=parent.orderId, ocaGroup=oca_group, ocaType=1,
                      transmit=False, outsideRth=outside_rth, account=acct)
    children.append(tgt1)

    # Optional target 2 on the residual.
    if t2 and qty_residual > 0:
        tgt2 = LimitOrder("SELL", qty_residual, round(float(t2), 2),
                          orderId=ib.client.getReqId(), tif=child_tif,
                          parentId=parent.orderId, ocaGroup=oca_group, ocaType=1,
                          transmit=False, outsideRth=outside_rth, account=acct)
        children.append(tgt2)

    # Stop on 100% of shares, OCA — transmit=True on the last child fires the batch.
    stp = StopOrder("SELL", qty, round(stop, 2),
                    orderId=ib.client.getReqId(), tif=child_tif,
                    parentId=parent.orderId, ocaGroup=oca_group, ocaType=1,
                    transmit=True, outsideRth=outside_rth, account=acct)
    children.append(stp)

    trades = [ib.placeOrder(contract, parent)]
    for child in children:
        trades.append(ib.placeOrder(contract, child))

    overnight_pct = int(decision.get("overnight_hold_pct") or 0)
    logger.info("submitted %s bracket %s: %d sh @ %.2f | T1 %.2f x%d | stop %.2f "
                "| T2 %s | overnight %d%% (%s)",
                session, symbol, qty, entry, t1, qty_t1, stop, t2, overnight_pct, child_tif)

    rec = fill_logger.log_entry(
        symbol=symbol, entry_limit=entry, shares=qty, target_1=t1, stop=stop,
        target_2=float(t2) if t2 else None,
        ai_confidence=decision.get("confidence"),
        ai_rationale=decision.get("rationale"),
        event="entry_submitted",
        extra={
            "oca_group": oca_group, "qty_target_1": qty_t1,
            "qty_residual": qty_residual,
            "trail_after_target_1": bool(decision.get("trail_after_target_1")),
            "session": session, "overnight_hold_pct": overnight_pct,
            "child_tif": child_tif,
            "status": "pending_entry", "account_type": config.ACCOUNT_TYPE,
        },
    )
    return rec


async def attach_trail_on_target_fill(ib: IB, symbol: str, residual_qty: int) -> None:
    """
    Replace the residual exit with a TRAIL order after target_1 fills.
    Call this from a fill handler when trail_after_target_1 was set.
    """
    if residual_qty <= 0:
        return
    contract = Stock(symbol, "SMART", "USD")
    await ib.qualifyContractsAsync(contract)
    trail = Order(orderType="TRAIL", action="SELL", totalQuantity=residual_qty,
                  trailingPercent=TRAIL_PERCENT, tif="DAY",
                  orderId=ib.client.getReqId(), transmit=True, account=_acct())
    ib.placeOrder(contract, trail)
    logger.info("trail attached for %s: %d sh @ %.0f%% offset",
                symbol, residual_qty, TRAIL_PERCENT)


def _cancel_symbol_orders(ib: IB, symbol: str) -> None:
    """Cancel every resting order for one symbol (leaves other symbols alone)."""
    for trade in ib.openTrades():
        if trade.contract.symbol != symbol:
            continue
        try:
            ib.cancelOrder(trade.order)
        except Exception:
            logger.exception("cancel failed for %s", symbol)


def _market_sell(ib: IB, contract, qty: int, real_position: int) -> None:
    if qty < real_position:
        logger.warning("%s: selling %d of %d total shares (manual/carry preserved)",
                       contract.symbol, qty, real_position)
    ib.placeOrder(contract, MarketOrder("SELL", qty, tif="DAY",
                                        orderId=ib.client.getReqId(), account=_acct()))
    logger.warning("market-sell %s x%d (forced)", contract.symbol, qty)


def _log_forced_exit(symbol: str, pos, qty: int, note: str) -> Dict[str, Any]:
    return fill_logger.log_exit(
        symbol=symbol, exit_kind="timeout_forced",
        exit_price=float(pos.avgCost or 0), shares=qty,
        extra={"avg_cost": float(pos.avgCost or 0), "note": note})


def _arm_overnight_bracket(ib: IB, contract, qty: int,
                           stop_price: Optional[float],
                           target_price: Optional[float]) -> None:
    """Re-arm a GTC OCA stop (+ optional target) on the carried shares so the
    overnight hold stays protected after the day's DAY orders are cancelled."""
    if qty <= 0 or not stop_price:
        return
    oca = f"ON_{contract.symbol}_{qty}"
    # outsideRth so the overnight stop/target can also act in pre/post market.
    stp = StopOrder("SELL", qty, round(float(stop_price), 2),
                    orderId=ib.client.getReqId(), tif="GTC",
                    ocaGroup=oca, ocaType=1, transmit=(target_price is None),
                    outsideRth=True, account=_acct())
    ib.placeOrder(contract, stp)
    if target_price:
        tgt = LimitOrder("SELL", qty, round(float(target_price), 2),
                         orderId=ib.client.getReqId(), tif="GTC",
                         ocaGroup=oca, ocaType=1, transmit=True, outsideRth=True,
                         account=_acct())
        ib.placeOrder(contract, tgt)


def forced_exit_sweep(ib: IB, allow_live: bool = False,
                      now_et: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """
    15:55 ET sweep — scoped to BOT-OWNED long-stock positions, honoring the
    intraday-vs-swing holding rule. Three cases per open bot position:

      • prior-day overnight hold  → flatten 100% (next-day backstop)
      • intraday entry today      → flatten 100%
      • swing entry today         → sell down to overnight_hold_pct of the
                                     original size, re-arm a GTC stop (+target)
                                     on the carry so it's protected overnight

    The operator's manual positions/orders (options, other stocks) are never
    touched. Sells are capped at the bot's own net shares AND the real position.
    """
    if _is_live_account(ib) and not allow_live:
        logger.error("forced_exit_sweep on LIVE account without allow_live — aborting")
        return []

    if now_et is None:
        now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")

    open_pos = fill_logger.open_bot_positions()
    if not open_pos:
        logger.info("forced-exit sweep: no open bot positions — nothing to do")
        return []

    # Index the configured account's real LONG STOCK positions by symbol.
    acct = _acct()
    ib_long = {}
    for pos in ib.positions():
        if acct and getattr(pos, "account", "") != acct:
            continue
        c = pos.contract
        if getattr(c, "secType", "STK") == "STK" and pos.position > 0:
            ib_long[c.symbol] = pos

    logger.warning("FORCED EXIT SWEEP (15:55 ET) — evaluating bot positions: %s",
                   sorted(open_pos))

    exits: List[Dict[str, Any]] = []
    for sym, info in open_pos.items():
        pos = ib_long.get(sym)
        if pos is None:
            logger.info("skip %s — bot record open but no live long-stock position", sym)
            continue
        contract = pos.contract
        real = int(pos.position)
        sellable = min(real, int(info["net_shares"]))  # never exceed bot's own / reality
        if sellable <= 0:
            continue
        held_from = info["entry_date"]
        session = info["session"]
        carry_pct = int(info["overnight_hold_pct"] or 0)

        # CASE 1 — prior-day overnight hold → backstop flatten.
        if held_from is not None and held_from < today:
            _cancel_symbol_orders(ib, sym)
            _market_sell(ib, contract, sellable, real)
            exits.append(_log_forced_exit(sym, pos, sellable, "next-day backstop"))
            continue

        # CASE 2 — intraday entry today → flatten.
        if session != "swing":
            _cancel_symbol_orders(ib, sym)
            _market_sell(ib, contract, sellable, real)
            exits.append(_log_forced_exit(sym, pos, sellable, "intraday 15:55"))
            continue

        # CASE 3 — swing entry today → sell down to carry, re-arm GTC on carry.
        carry = min(int(round(carry_pct / 100.0 * int(info["original_shares"]))), sellable)
        excess = sellable - carry
        _cancel_symbol_orders(ib, sym)
        if excess > 0:
            _market_sell(ib, contract, excess, real)
            exits.append(_log_forced_exit(
                sym, pos, excess, f"swing day-portion ({carry_pct}% carried overnight)"))
        if carry > 0:
            _arm_overnight_bracket(ib, contract, carry, info.get("stop"), info.get("target_1"))
            logger.warning("%s: carrying %d sh overnight on GTC stop %.2f "
                           "(%d%% of %d original)", sym, carry,
                           float(info.get("stop") or 0), carry_pct,
                           int(info["original_shares"]))
            fill_logger.log_event("carry_overnight", symbol=sym, shares=carry,
                                  stop=info.get("stop"), target_1=info.get("target_1"),
                                  overnight_hold_pct=carry_pct)
    return exits
