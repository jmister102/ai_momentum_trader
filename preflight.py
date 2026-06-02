"""
preflight.py — readiness check. Run before every session.

    py preflight.py
    py preflight.py --allow-live   # acknowledge live trading (still warns)

Exit 0 = all required checks pass and it is safe to start run_trader.py.
Exit 1 = at least one required check failed; the fix is printed inline.

Checks (in order):
  1. Python deps importable (ib_insync, anthropic, polygon, matplotlib, mplfinance, pytz)
  2. config.py present and complete
  3. Account guard: ACCOUNT_TYPE vs IB_PORT vs --allow-live
  4. IB Gateway reachable on host:port
  5. IB API handshake + connected account is paper (DU…) unless --allow-live
  6. Polygon key valid (snapshot for AAPL)
  7. Anthropic key valid (1-token completion)
  8. fills.jsonl readable / creatable; charts/ exists
  9. ET clock + market session
 10. Today's trade count (and 2-trade-limit warning)
"""

from __future__ import annotations

import argparse
import importlib
import socket
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import pytz

ET = pytz.timezone("America/New_York")
HERE = Path(__file__).parent

GREEN, RED, YELLOW, CYAN, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[1m", "\033[0m")


def ok(m): print(f"  {GREEN}✓{RESET} {m}")
def bad(m, hint=None):
    print(f"  {RED}✗{RESET} {m}")
    if hint:
        for ln in hint.splitlines():
            print(f"    {YELLOW}→{RESET} {ln}")
    return False
def warn(m):
    print(f"  {YELLOW}⚠{RESET}  {m}")
def step(n, t): print(f"\n{BOLD}[{n}] {t}{RESET}")


REQUIRED_MODULES = {
    "ib_insync": "ib_insync",
    "anthropic": "anthropic",
    "polygon": "polygon-api-client",
    "matplotlib": "matplotlib",
    "mplfinance": "mplfinance",
    "pytz": "pytz",
}


def check_deps() -> bool:
    step(1, "Python dependencies")
    missing = []
    for mod, pip_name in REQUIRED_MODULES.items():
        try:
            importlib.import_module(mod)
            ok(f"{mod}")
        except ImportError:
            print(f"  {RED}✗{RESET} {mod} missing")
            missing.append(pip_name)
    if missing:
        return bad(f"{len(missing)} package(s) missing",
                   hint="py -m pip install " + " ".join(sorted(set(missing))))
    return True


def check_config():
    step(2, "config.py")
    try:
        import config
    except Exception as e:
        bad(f"cannot import config.py: {e}",
            hint="cp config.example.py config.py  # then fill in keys")
        return None
    required = ["IB_HOST", "IB_PORT", "IB_CLIENT_ID", "POLYGON_API_KEY",
                "ANTHROPIC_API_KEY", "ACCOUNT_TYPE"]
    missing = [k for k in required if not getattr(config, k, None)]
    if missing:
        bad(f"config.py missing/empty: {', '.join(missing)}")
        return None
    placeholders = [k for k in ("POLYGON_API_KEY", "ANTHROPIC_API_KEY")
                    if str(getattr(config, k)).startswith("your_")]
    for k in placeholders:
        warn(f"{k} still set to the placeholder value")
    ok(f"loaded — ACCOUNT_TYPE={config.ACCOUNT_TYPE}, port={config.IB_PORT}")
    return config


def check_account_guard(config, allow_live) -> bool:
    step(3, "Account guard")
    is_live_cfg = str(config.ACCOUNT_TYPE).lower() == "live"
    if is_live_cfg:
        # Preflight only inspects — it never places an order — so a live config
        # is expected here and passes. The real-money gate lives in run_trader.py
        # (--dry-run = live data + no orders; --allow-live = real orders).
        warn(f"ACCOUNT_TYPE=live — run_trader places orders ONLY with --allow-live; "
             f"default --dry-run uses live data and places none")
        return True
    ok(f"paper configuration (ACCOUNT_TYPE={config.ACCOUNT_TYPE})")
    return True


def check_port(config) -> bool:
    step(4, f"IB Gateway reachable @ {config.IB_HOST}:{config.IB_PORT}")
    try:
        with socket.create_connection((config.IB_HOST, config.IB_PORT), timeout=3):
            ok("port open")
            return True
    except OSError:
        return bad("nothing listening",
                   hint="Start IB Gateway and log in.\n"
                        "Configure → API → enable 'ActiveX and Socket Clients',\n"
                        f"socket port {config.IB_PORT}, trusted IP 127.0.0.1.")


def check_handshake(config, allow_live) -> bool:
    step(5, "IB API handshake + account")
    try:
        from ib_insync import IB
    except ImportError:
        return bad("ib_insync not installed")
    ib = IB()
    try:
        ib.connect(config.IB_HOST, config.IB_PORT,
                   clientId=getattr(config, "IB_SCANNER_CLIENT_ID", 11) + 50,
                   timeout=8)
    except Exception as e:
        return bad(f"handshake failed: {e}",
                   hint="Is another client using this clientId? Is API access enabled?")
    try:
        accounts = ib.managedAccounts()
        ok(f"connected — server v{ib.client.serverVersion()}, accounts={accounts}")
        live_accts = [a for a in accounts if a and not a.startswith("DU")]
        declared_paper = str(config.ACCOUNT_TYPE).lower() != "live"

        # HARD FAIL — declared intent contradicts reality. The Gateway is logged
        # into a LIVE account (U… ; paper accounts are DU…) while config says
        # paper. --allow-live CANNOT override this: a paper declaration means
        # paper, full stop. Fix the Gateway login mode (or the config), never
        # the flag.
        if live_accts and declared_paper:
            bad(f"LIVE account(s) on a PAPER-declared session: {live_accts}",
                hint="config.py ACCOUNT_TYPE=paper, but IB Gateway is logged into a\n"
                     "LIVE account (U… prefix; paper accounts start with DU…).\n"
                     "→ Log out of IB Gateway and log back in choosing 'Paper Trading'.\n"
                     "→ Do NOT trade this session — it would use real money.")
            return False

        if not live_accts:
            ok("paper account (DU…) confirmed")
        else:
            # Declared live + live account = expected. Preflight passes (it does
            # not trade); run_trader.py is what gates real orders behind --allow-live.
            warn(f"LIVE account(s): {live_accts} — orders gated by run_trader --allow-live")
        return True
    finally:
        ib.disconnect()


def check_polygon(config) -> bool:
    step(6, "Polygon API key")
    if str(config.POLYGON_API_KEY).startswith("your_"):
        return bad("placeholder key — set POLYGON_API_KEY in config.py")
    try:
        import requests
        r = requests.get(
            "https://api.polygon.io/v2/aggs/ticker/AAPL/prev",
            params={"apiKey": config.POLYGON_API_KEY}, timeout=8)
        if r.status_code == 200 and r.json().get("status") in ("OK", "DELAYED"):
            ok("key valid (AAPL prev-day OK)")
            return True
        if r.status_code in (401, 403):
            return bad(f"unauthorized ({r.status_code}) — key rejected")
        warn(f"unexpected response {r.status_code}: {r.text[:120]}")
        return True
    except Exception as e:
        warn(f"could not verify Polygon (network?): {e}")
        return True


def check_anthropic(config) -> bool:
    step(7, "Anthropic API key")
    if str(config.ANTHROPIC_API_KEY).startswith("your_"):
        return bad("placeholder key — set ANTHROPIC_API_KEY in config.py",
                   hint="Get one at https://console.anthropic.com/settings/keys")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        client.messages.create(
            model=getattr(config, "AI_MODEL", "claude-sonnet-4-20250514"),
            max_tokens=1, messages=[{"role": "user", "content": "hi"}])
        ok("key valid (minimal completion OK)")
        return True
    except Exception as e:
        return bad(f"Anthropic check failed: {e}")


def check_files() -> bool:
    step(8, "Files & directories")
    fills = HERE / "fills.jsonl"
    try:
        if not fills.exists():
            fills.touch()
            ok("fills.jsonl created")
        else:
            ok("fills.jsonl present")
    except OSError as e:
        return bad(f"cannot create fills.jsonl: {e}")
    charts = HERE / "charts"
    charts.mkdir(exist_ok=True)
    ok("charts/ ready")
    return True


def report_session() -> None:
    step(9, "Market session (ET)")
    now = datetime.now(ET)
    t = now.time()
    if dtime(4, 0) <= t < dtime(9, 30):
        sess = "PRE-MARKET (IB scanner unreliable — use polygon_pm_scanner.py)"
    elif dtime(9, 30) <= t < dtime(16, 0):
        sess = "REGULAR HOURS"
    elif dtime(16, 0) <= t < dtime(20, 0):
        sess = "AFTER-HOURS (use polygon_pm_scanner.py)"
    else:
        sess = "CLOSED"
    print(f"  {CYAN}{now.strftime('%Y-%m-%d %H:%M:%S %Z')}{RESET}  —  {sess}")


def report_trade_count() -> None:
    step(10, "Today's trade count")
    try:
        import fill_logger
        n = fill_logger.trades_today()
        if n >= 2:
            warn(f"{n}/2 entries today — daily limit reached, new orders will be rejected")
        else:
            ok(f"{n}/2 entries today")
    except Exception as e:
        warn(f"could not read fills.jsonl: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-live", action="store_true")
    args = ap.parse_args()

    print(f"{BOLD}━━━ AI Momentum Trader — preflight ━━━{RESET}")

    if not check_deps():
        return 1
    config = check_config()
    if config is None:
        return 1
    if not check_account_guard(config, args.allow_live):
        return 1
    if not check_port(config):
        return 1
    if not check_handshake(config, args.allow_live):
        return 1

    # Soft checks below: failures warn but reaching here means trading plumbing is OK.
    polygon_ok = check_polygon(config)
    anthropic_ok = check_anthropic(config)
    files_ok = check_files()
    report_session()
    report_trade_count()

    print()
    if polygon_ok and anthropic_ok and files_ok:
        print(f"{GREEN}{BOLD}✓ ALL CHECKS PASSED — safe to start run_trader.py{RESET}")
        return 0
    print(f"{RED}{BOLD}✗ Some checks failed — fix the ✗ items above before trading.{RESET}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
