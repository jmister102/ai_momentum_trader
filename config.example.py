# AI Momentum Trader — config template.
# Copy to config.py (gitignored) and fill in real values.
#
#   cp config.example.py config.py
#
# config.py is read by every script. Never commit it.

IB_HOST = "127.0.0.1"
IB_PORT = 4002              # 4002 = IB Gateway paper, 4001 = IB Gateway live
IB_CLIENT_ID = 1           # order_router / run_trader uses this
IB_SCANNER_CLIENT_ID = 11  # trigger_monitor uses a separate id so both can share one Gateway session

POLYGON_API_KEY = "your_polygon_key"
ANTHROPIC_API_KEY = "your_anthropic_key"

ACCOUNT_TYPE = "paper"     # "paper" or "live" — the account guard reads this

# Claude model used by ai_analyst.py
AI_MODEL = "claude-sonnet-4-20250514"
