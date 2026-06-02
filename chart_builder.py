"""
chart_builder.py — render a 5-day / 15-minute candlestick PNG from Polygon.

Saves to charts/{symbol}_{date}.png and returns the path. The AI call does NOT
receive the image — ai_analyst.py describes the price action in text — but the
PNG is saved so the human operator can eyeball every trigger.

Standalone test:
    py chart_builder.py HKIT --prior-close 2.74
    py chart_builder.py AAPL                     # no overlays, just the chart
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger("chart_builder")
POLYGON_BASE = "https://api.polygon.io"
CHARTS_DIR = Path(__file__).parent / "charts"


def fetch_15m_bars(symbol: str, days: int = 5) -> list[dict]:
    """5 trading days of 15-min bars. Over-fetch calendar days to cover weekends."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days + 5)
    url = (f"{POLYGON_BASE}/v2/aggs/ticker/{symbol}/range/15/minute/"
           f"{start}/{end}")
    try:
        r = requests.get(url, params={"adjusted": "true", "sort": "asc",
                                      "limit": 50000, "apiKey": config.POLYGON_API_KEY},
                         timeout=15)
        if r.status_code != 200:
            logger.warning("polygon bars → HTTP %s for %s", r.status_code, symbol)
            return []
        return r.json().get("results", []) or []
    except Exception as e:
        logger.warning("polygon bars failed for %s: %s", symbol, e)
        return []


def build_chart(
    symbol: str,
    prior_close: Optional[float] = None,
    pct_gain: Optional[float] = None,
    trigger_time_et: Optional[str] = None,
    out_dir: Path = CHARTS_DIR,
) -> Optional[str]:
    """Render the chart PNG. Returns the file path, or None if data/plot fails."""
    bars = fetch_15m_bars(symbol)
    if not bars:
        logger.warning("no bars for %s — skipping chart", symbol)
        return None

    # Import matplotlib lazily so the rest of the pipeline runs without it.
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        logger.warning("matplotlib not installed — skipping chart")
        return None

    o = [b["o"] for b in bars]
    h = [b["h"] for b in bars]
    low = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    x = list(range(len(bars)))

    plt.style.use("dark_background")
    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(9, 6), dpi=100, sharex=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.05})

    width = 0.7
    for i in x:
        up = c[i] >= o[i]
        color = "#26a69a" if up else "#ef5350"
        ax.plot([i, i], [low[i], h[i]], color=color, linewidth=0.6, zorder=1)
        body_lo = min(o[i], c[i])
        ax.add_patch(Rectangle((i - width / 2, body_lo), width, abs(c[i] - o[i]) or 1e-6,
                               facecolor=color, edgecolor=color, zorder=2))
        axv.bar(i, v[i], width=width, color=color, alpha=0.6)

    if prior_close:
        ax.axhline(prior_close, color="#888", linestyle=":", linewidth=1.0)
        ax.text(0, prior_close, f" Prior close ${prior_close:.2f}",
                color="#aaa", fontsize=8, va="bottom")
        trigger = 2.0 * prior_close
        ax.axhline(trigger, color="#ffb74d", linestyle="--", linewidth=1.2)
        ax.text(0, trigger, f" 2× trigger ${trigger:.2f}",
                color="#ffb74d", fontsize=8, va="bottom")

    date_str = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    title = f"{symbol} — {date_str}"
    if pct_gain is not None:
        title += f" | +{pct_gain*100:.0f}%"
    if trigger_time_et:
        title += f" | Triggered {trigger_time_et} ET"
    ax.set_title(title, fontsize=11, color="white")
    ax.set_ylabel("Price ($)", fontsize=9)
    axv.set_ylabel("Vol", fontsize=9)
    axv.set_xlabel("15-min bars (5 trading days)", fontsize=9)
    ax.grid(alpha=0.15)
    axv.grid(alpha=0.15)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{symbol}_{date_str}.png"
    fig.savefig(path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    logger.info("chart saved: %s", path)
    return str(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a 5-day 15-min chart PNG")
    ap.add_argument("symbol")
    ap.add_argument("--prior-close", type=float, default=None)
    ap.add_argument("--pct-gain", type=float, default=None, help="fraction, e.g. 1.47")
    ap.add_argument("--trigger-time", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    path = build_chart(args.symbol.upper(), prior_close=args.prior_close,
                       pct_gain=args.pct_gain, trigger_time_et=args.trigger_time)
    if path:
        print(f"✓ {path}")
        return 0
    print("✗ chart not generated (see warnings)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
