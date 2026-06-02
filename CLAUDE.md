# AI Momentum Trader — Claude Code Instructions

## Project Mission

Build an automated intraday trading system that detects US small-cap stocks that have gained **≥100% on the day**, enriches each candidate with market context, plots recent price history, and delegates the **entry/exit decision entirely to Claude Sonnet AI**. No hard-coded exit logic. The AI owns the trade thesis.

Paper-trade first. Live deployment only after 2 weeks of logged what-if fills with positive profit factor.

---

## Architecture Overview

```
IB Gateway (scanner + orders)
        │
        ▼
  trigger_monitor.py        ← polls IB scanner watchlist for ≥+100% movers
        │
        ▼
  enricher.py               ← pulls ADV, market cap, float via IB fundamental data
        │
        ▼
  chart_builder.py          ← fetches 5-day 15-min OHLCV from Polygon, saves PNG
        │
        ▼
  ai_analyst.py             ← calls Claude Sonnet with full context, gets decision
        │
        ▼
  order_router.py           ← submits IB bracket orders if AI says GO
        │
        ▼
  fill_logger.py            ← records all fills to fills.jsonl
  perf.py                   ← reads fills.jsonl, prints P&L report
```

All components are Python scripts. Run natively on Windows (not WSL2). IB Gateway must be running and logged in before any script starts.

---

## Runtime Environment

- **Python**: 3.10+ native Windows
- **IB Gateway**: running on `127.0.0.1:4002` (paper) or `127.0.0.1:4001` (live)
- **IB library**: `ib_insync` (`pip install ib_insync`)
- **Polygon**: REST API for historical OHLCV (`pip install polygon-api-client`)
- **Anthropic**: Claude API (`pip install anthropic`)
- **Charting**: `matplotlib` for PNG chart generation
- **Config**: all secrets in `config.py` (never committed)

### config.py (create manually, gitignored)
```python
IB_HOST = "127.0.0.1"
IB_PORT = 4002          # 4002 = paper, 4001 = live
IB_CLIENT_ID = 1
POLYGON_API_KEY = "your_polygon_key"
ANTHROPIC_API_KEY = "your_anthropic_key"
ACCOUNT_TYPE = "paper"  # "paper" or "live" — safety guard reads this
```

---

## Safety Rules (non-negotiable, enforce in every file)

1. **Account guard**: on startup, read `ACCOUNT_TYPE` from `config.py`. If `"live"`, require `--allow-live` CLI flag or abort. Log a warning to console either way.
2. **Max 2 trades per calendar day**: `order_router.py` reads `fills.jsonl`, counts entries for today. If count ≥ 2, reject all new orders silently and log `"Daily trade limit reached"`.
3. **Position size**: always `floor(200 / entry_price)` shares. Never more.
4. **Holding window keyed to entry time** (updated 2026-06-02, supersedes the original intraday-only rule):
   - **Entry before 12:00 ET (intraday)**: closed by 15:55 ET the same day. `overnight_hold_pct` MUST be 0. DAY brackets.
   - **Entry at/after 12:00 ET (swing-eligible)**: the AI may carry `overnight_hold_pct` (0–100%) of the position overnight. Carried shares ride a **GTC** stop/target and are **force-closed by the NEXT day's 15:55 sweep** (max one overnight). Late-day entries are NOT eliminated — the ≥30-min-before-close cutoff applies to intraday entries only.
5. **No shorting**: long only.
6. **Overnight holds are bounded, not banned**: only swing (after-noon) entries may carry, only the AI-chosen fraction, only on a GTC stop, and never past the next day's 15:55 sweep. The sweep is scoped to bot-owned long-stock positions — it never touches the operator's manual positions/orders, and caps each sell at the bot's own net shares.

---

## IB Scanner Integration

### Scanner Setup (done once in TWS/Gateway UI)
Create a saved scanner named `"MOMENTUM_100"` with these parameters:
- Instrument: STK, US exchanges
- Scan: `TOP_PERC_GAIN` (percent change from prior close)
- Filters: Price ≥ 1.00, Volume ≥ 50,000
- Max results: 25

The scanner is saved as a watchlist. `trigger_monitor.py` polls it via `reqScannerSubscription`.

### trigger_monitor.py — What to Build

- Connect to IB Gateway via `ib_insync`
- Subscribe to the `MOMENTUM_100` scanner
- On each scanner update, iterate results
- For each ticker: calculate `pct_gain = (last_price - prior_close) / prior_close`
- If `pct_gain >= 1.00` (i.e. ≥100%): fire the enrichment + AI pipeline
- Debounce: once a ticker has been sent to the pipeline today, skip it on subsequent scanner ticks (use an in-memory set `triggered_today`)
- Log all triggers to `triggers.jsonl` with timestamp, symbol, last_price, pct_gain

---

## Enrichment — enricher.py

When a ticker triggers, collect the following via IB and/or Polygon before calling the AI:

| Field | Source | Notes |
|---|---|---|
| `symbol` | IB scanner | — |
| `last_price` | IB market data | reqMktData snapshot |
| `prior_close` | IB market data | `PREV_CLOSE` generic tick |
| `pct_gain_today` | calculated | `(last - prior) / prior` |
| `volume_today` | IB market data | `VOLUME` generic tick |
| `adv_20` | Polygon `/v2/aggs` | 20-day avg dollar volume: mean of `(close × volume)` over last 20 trading days |
| `market_cap` | IB fundamental data | `ReqFundamentalData` with `ReportSnapshot`; parse `MKTCAP` from XML |
| `shares_outstanding` | IB fundamental data | same XML, `SHARESOUT` field |
| `52w_high` | IB market data | `HIGH_52` generic tick |
| `52w_low` | IB market data | `LOW_52` generic tick |
| `sector` | IB contract details | `reqContractDetails` → `industry` field |
| `news_headlines` | Polygon `/v2/reference/news` | last 3 headlines for the symbol, title + published timestamp only |

Return a Python dict. Any field that fails to fetch should be `None` with a warning logged — never crash the pipeline over a missing metric.

---

## Chart Builder — chart_builder.py

Fetch **5 trading days of 15-minute OHLCV bars** from Polygon for the symbol. Use the `/v2/aggs/ticker/{symbol}/range/15/minute/{from}/{to}` endpoint.

Generate a clean chart PNG saved to `charts/{symbol}_{date}.png`:

- Candlestick chart (use `mplfinance` or manual matplotlib patches)
- Volume bars subplot below price
- Mark the trigger price (2× prior_close) as a horizontal dashed line labeled `"Entry limit"`
- Mark today's prior_close as a horizontal dotted line labeled `"Prior close"`
- Title: `"{SYMBOL} — {date} | +{pct_gain:.0f}% | Triggered {trigger_time} ET"`
- Keep it readable at 900×600px, dark background preferred

Return the file path. The AI call will reference this chart description in text (do not send the image to the API — describe it in the prompt).

---

## AI Analyst — ai_analyst.py

This is the core of the system. Call `claude-sonnet-4-20250514` with a structured prompt containing all enrichment data. The AI returns a structured JSON decision.

### Prompt Template

```python
SYSTEM_PROMPT = """
You are an expert intraday trading analyst specializing in small-cap momentum stocks.
You will be given data about a stock that has just triggered a +100% gain alert.
Your job is to make a binary GO / NO-GO entry decision and, if GO, specify exact exit levels.

HARD RULES YOU MUST FOLLOW (non-negotiable):
- Trade size is fixed at $200 regardless of your confidence.
- Limit sell targets must represent at least +20% gain from entry.
- Stop loss must be no worse than -50% from entry (can be tighter).
- Holding window depends on entry time: intraday (before 12:00 ET) closes at 15:55 ET (overnight_hold_pct=0); swing-eligible (≥12:00 ET) may carry overnight_hold_pct (0–100%) overnight on a GTC stop, force-closed by the next day's 15:55. (See ai_analyst.py for the live prompt; this template is illustrative.)
- Max 2 trades per day — if you see "trades_today: 2" you MUST output NO-GO.

Respond ONLY with a valid JSON object. No preamble, no explanation outside the JSON.
Schema:
{
  "decision": "GO" | "NO-GO",
  "confidence": 0.0-1.0,
  "entry_limit": <float, must equal 2.0 × prior_close>,
  "target_1_price": <float, ≥ entry × 1.20>,
  "target_1_pct_shares": <int, 25-75>,
  "target_2_price": <float | null, > target_1 if present>,
  "target_2_pct_shares": <int | null>,
  "stop_loss_price": <float, ≥ entry × 0.50>,
  "trail_after_target_1": <bool>,
  "rationale": "<2-3 sentences explaining the key factors driving the decision>",
  "risk_factors": ["<factor 1>", "<factor 2>"],
  "reject_reason": "<string if NO-GO, else null>"
}
"""

USER_PROMPT_TEMPLATE = """
TRIGGER ALERT — {symbol}

## Market Context
- Current price: ${last_price:.2f}
- Prior close: ${prior_close:.2f}
- Gain today: +{pct_gain_today:.1%}
- Entry limit (2× prior close): ${entry_limit:.2f}
- Volume today: {volume_today:,} shares
- 20-day avg dollar volume (ADV): ${adv_20:,.0f}
- Today's volume vs ADV ratio: {vol_adv_ratio:.1f}x

## Fundamentals
- Market cap: ${market_cap}
- Shares outstanding: {shares_outstanding}
- Sector: {sector}
- 52-week high: ${high_52w:.2f}
- 52-week low: ${low_52w:.2f}

## Recent News (last 3 headlines)
{news_block}

## Chart Summary
5-day 15-min chart saved to: {chart_path}
Price action description: stock was trading near ${prior_close:.2f} yesterday close, 
opened today and has surged {pct_gain_today:.1%} intraday. 
Current price ${last_price:.2f} is {price_vs_entry} the 2× entry limit of ${entry_limit:.2f}.

## Session Context
- Trades taken today: {trades_today} / 2 max
- Current ET time: {et_time}
- Time remaining until forced exit (15:55 ET): {minutes_remaining} minutes
"""
```

### ai_analyst.py Logic
1. Build the prompt by filling the template with enrichment data
2. Call the Anthropic API (model: `claude-sonnet-4-20250514`, max_tokens: 800, temperature: 0)
3. Parse the JSON response
4. Validate the response: confirm entry_limit == 2× prior_close ± 0.01, target_1 ≥ entry × 1.20, stop ≥ entry × 0.50. If validation fails, log the error and treat as NO-GO
5. Return the parsed dict

---

## Order Router — order_router.py

Only executes if `ai_decision["decision"] == "GO"`.

### On GO:
1. Check daily trade count (fills.jsonl) — if already 2, abort and log
2. Check time — for **intraday** (before-noon) entries only, if < 30 minutes remain before 15:55 forced exit, abort and log `"Too late to enter"`. Swing-eligible (after-noon) entries may enter up to the close.
3. Calculate shares: `floor(200 / entry_limit)`
4. Submit to IB Gateway via `ib_insync`:
   - **Parent**: `LMT BUY` at `entry_limit`, `tif=DAY`
   - **OCA group** attached to parent fill:
     - `LMT SELL` at `target_1_price` for `target_1_pct_shares`% of shares
     - `STP SELL` at `stop_loss_price` for 100% of shares (OCA, cancels target if hit)
   - If `target_2_price` is set: additional `LMT SELL` for remaining shares after target_1
   - If `trail_after_target_1 == true`: after target_1 fills, submit a `TRAIL` order for residual at 15% trail offset
5. Log the submission to `fills.jsonl` with status `"pending_entry"`

### Forced Exit Sweep (15:55 ET)
- Query all open IB positions for today's symbols
- Cancel any remaining open orders for those symbols
- Submit `MKT SELL DAY` for all open shares
- Log exits to `fills.jsonl` with `exit_kind: "timeout_forced"`

---

## Fill Logger — fill_logger.py

All events write to `fills.jsonl` (one JSON object per line).

### Entry record
```json
{
  "ts": "2026-06-01T09:31:00-04:00",
  "symbol": "HKIT",
  "event": "entry_submitted",
  "entry_limit": 2.74,
  "shares": 72,
  "target_1": 3.29,
  "stop": 1.37,
  "ai_confidence": 0.78,
  "ai_rationale": "...",
  "trades_today": 1
}
```

### Exit record
```json
{
  "ts": "2026-06-01T11:14:00-04:00",
  "symbol": "HKIT",
  "event": "exit_fill",
  "exit_kind": "target_1" | "target_2" | "stop" | "trail" | "timeout_forced",
  "exit_price": 3.29,
  "shares": 36,
  "pnl_dollars": 39.60,
  "pnl_pct": 0.20
}
```

---

## Performance Reporter — perf.py

Read `fills.jsonl` and print a summary. Accept optional `--since YYYY-MM-DD` flag.

Output:
```
=== AI Momentum Trader Performance ===
Period: 2026-06-01 to 2026-06-02
Trades: 5 entries, 5 fully closed
Win rate: 60.0% (3W / 2L)
Gross P&L: +$312.40
Avg winner: +$156.20 (+38.4%)
Avg loser:  -$78.10  (-22.1%)
Profit factor: 2.00
Largest win:  HKIT  +$111 (+72%)
Largest loss: GNTA  -$43  (-18%)

Exit breakdown:
  target_1:        2
  target_2:        1
  stop:            1
  timeout_forced:  1
```

---

## Main Runner — run_trader.py

Entry point. Wires everything together in an event loop.

```python
# Pseudocode structure
async def main():
    ib = connect_ib()
    scanner = subscribe_scanner(ib, "MOMENTUM_100")
    register_fill_handler(ib)          # live fill callbacks → fill_logger
    schedule_forced_exit_sweep(15, 55) # fires at 15:55 ET daily
    
    async for update in scanner:
        for contract in update.contractDetails:
            symbol = contract.contract.symbol
            pct_gain = compute_gain(contract)
            if pct_gain >= 1.00 and symbol not in triggered_today:
                triggered_today.add(symbol)
                asyncio.create_task(run_pipeline(ib, symbol, contract))

async def run_pipeline(ib, symbol, contract):
    enrichment = await enrich(ib, symbol)
    chart_path = build_chart(symbol, enrichment)
    decision   = call_ai(enrichment, chart_path)
    log_decision(symbol, decision)
    print_decision_summary(symbol, decision)   # human-readable console output
    if decision["decision"] == "GO":
        submit_orders(ib, symbol, decision, enrichment)
```

### Console Output on Each Trigger

Print a clear block so the operator can see what's happening:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 TRIGGER: HKIT  +147%  $2.74 → $6.79  09:14 ET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ADV (20d):     $412,000   Vol ratio: 8.3×
  Mkt Cap:       $28M       Sector: Technology
  News:          "Company announces FDA clearance" (06-01 07:32)
  Chart:         charts/HKIT_2026-06-01.png

  AI DECISION:   ✅ GO  (confidence: 0.82)
  Entry limit:   $2.74
  Target 1:      $3.56  (+30%)  50% shares → trail remainder
  Stop loss:     $1.92  (-30%)
  Rationale:     "High volume surge on catalyst news with confirmed ADV..."
  Risk factors:  ["Micro-cap, wide spread risk", "Extended from VWAP"]

  ORDER SENT → 72 shares  |  Trade 1 of 2 today
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## File Structure

```
trader/
├── config.py                  ← secrets (gitignored)
├── run_trader.py              ← main entry point
├── trigger_monitor.py         ← IB scanner subscription
├── enricher.py                ← IB + Polygon data fetch
├── chart_builder.py           ← Polygon OHLCV → matplotlib PNG
├── ai_analyst.py              ← Claude Sonnet API call + JSON parse
├── order_router.py            ← IB bracket order submission
├── fill_logger.py             ← fills.jsonl read/write
├── perf.py                    ← P&L report CLI
├── preflight.py               ← connection + config sanity check
├── fills.jsonl                ← all trade events (gitignored)
├── triggers.jsonl             ← all scanner triggers (gitignored)
├── charts/                    ← generated chart PNGs
└── CLAUDE.md                  ← this file
```

---

## Preflight Checks — preflight.py

Run before every session. Check and report:
- [ ] IB Gateway reachable at configured host:port
- [ ] Account type matches config (paper vs live)
- [ ] Polygon API key valid (test with a known ticker)
- [ ] Anthropic API key valid (test with a minimal completion)
- [ ] `fills.jsonl` readable (or create if missing)
- [ ] `charts/` directory exists (create if missing)
- [ ] Current ET time and market session (pre-market / RTH / after-hours / closed)
- [ ] Today's trade count from `fills.jsonl`

Exit with code 0 if all pass. Exit with code 1 and print failures if any check fails.

---

## IB Scanner Watchlist — Technical Notes

- Use `ib_insync`'s `reqScannerSubscription` with a `ScannerSubscription` object
- Set `scanCode = "TOP_PERC_GAIN"`, `instrument = "STK"`, `locationCode = "STK.US.MAJOR"`
- Set `abovePrice = 1.0`, `aboveVolume = 50000`
- The scanner returns `ScanData` items with `.contractDetails` and `.distance` (rank)
- Poll interval: IB pushes updates every ~30 seconds automatically
- Handle `Error 162` (scanner data farm disconnect) by reconnecting after 10 seconds
- For PM/AH: IB's TOP_PERC_GAIN scanner **does not work** in pre-market or after-hours (returns Error 162 consistently). Mitigation: supplement with a Polygon snapshot poll during PM/AH hours (see below)

### PM/AH Fallback — polygon_pm_scanner.py

During 04:00–09:29 ET and 16:00–20:00 ET, IB scanner is unreliable. Run a parallel Polygon-based scanner:

- Every 60 seconds: call Polygon `/v2/snapshot/locale/us/markets/stocks/tickers` with `include_otc=false`
- Filter: `ticker.day.c` (close) / `ticker.prevDay.c` ≥ 2.0 AND `ticker.prevDay.c` ≥ 1.00 AND `ticker.day.v * ticker.day.c` ≥ 200,000 (dollar volume filter)
- For matches not already in `triggered_today`: fire the enrichment pipeline
- During RTH (09:30–16:00): defer to IB scanner as primary

---

## Development Order

Build and test in this sequence:

1. `preflight.py` — validate all connections before writing any logic
2. `fill_logger.py` — the simplest file; needed by everything else
3. `enricher.py` — test with a hardcoded symbol first (e.g. `python enricher.py AAPL`)
4. `chart_builder.py` — test standalone, open the PNG and verify it looks right
5. `ai_analyst.py` — test with a hardcoded enrichment dict; print raw AI response before parsing
6. `trigger_monitor.py` — test with IB connected; just print triggers, don't run pipeline yet
7. `order_router.py` — test in paper mode only; verify bracket order structure in TWS
8. `run_trader.py` — wire everything together
9. `perf.py` — last, since it needs real fill data

---

## Known Gotchas

- **IB `reqMktData` in paper mode**: generic ticks like `PREV_CLOSE` (tick type 9) sometimes return 0 for thinly-traded stocks. Fallback: use Polygon `/v2/aggs/ticker/{symbol}/prev` for prior close.
- **Scanner vs live price discrepancy**: the scanner's `distance` field is the pct gain at scan time, not necessarily the current price. Always re-fetch live price before computing entry_limit.
- **IB order IDs**: use `ib.client.getReqId()` for each order. Bracket orders need the parent ID to be the child's `parentId`. `ib_insync`'s `bracketOrder()` helper handles this.
- **Polygon rate limits**: free tier is 5 req/min. Paid tier is unlimited. If on free tier, add `time.sleep(0.2)` between Polygon calls in enricher.
- **JSON parse failure from AI**: wrap the Anthropic response parse in try/except. If JSON is malformed, log the raw response and treat as NO-GO. Do not retry — one shot per trigger.
- **ib_insync event loop on Windows**: use `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` at the top of `run_trader.py`.

---

## Session Startup Command

```powershell
cd D:\dev\claude\trader

# Verify everything is wired
py preflight.py

# Start the trader (paper mode — no flag needed)
py run_trader.py

# If you need live mode (after 2-week paper validation):
py run_trader.py --allow-live
```

---

## What Success Looks Like (Phase 1 Gate)

Before enabling live execution:
- [ ] 10+ trading days of trigger logs with AI decisions recorded
- [ ] 10+ "what-if" fills logged in `fills.jsonl` with manual entry via `fill_logger.py`
- [ ] `perf.py` shows profit factor ≥ 1.5 on the what-if sample
- [ ] AI NO-GO rate is between 40%–80% (if AI says GO to everything, the filter isn't working)
- [ ] Zero forced-exit trades triggered by the 15:55 sweep in paper (means entry timing is solid)
- [ ] Slippage audit: compare `entry_limit` (model) vs actual fill prices from paper account
