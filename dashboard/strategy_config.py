"""
STRATEGY CONFIGURATION: Daily V3.1
Shared parameters between screener and backtest

This module defines the complete trading strategy used for both:
1. Backtesting (historical validation)
2. Live screener (real-time signal generation)
3. Trade log tracking (position management with backtest rules)
"""

# ── ENTRY FILTERS ──────────────────────────────────────────────────────────
# All filters must pass for an entry signal

IHSG_GATE = 50              # Market gate: IHSG score >= 50 (Bull or Strong Bull)
POI_BAND = 0.05             # Price must be within ±5% of POI demand zone
VOL_MULT = 1.1              # Volume >= 1.1x 10-bar average
WINDOW_DAILY = 60           # Rolling window for daily SMC (bars)
WINDOW_WEEKLY = 26          # Rolling window for weekly SMC (weeks, must be ≥20)
MAX_HOLD_BARS = 50          # Maximum hold time (bars = days)

# Rejection candle: green OR hammer (lower wick >= 1x body)
REJECTION_BODY_MIN = 0.01   # Minimum body size ratio

# ── STOP LOSS ──────────────────────────────────────────────────────────────
SL_FACTOR = 0.015           # SL = poi_low × (1 - SL_FACTOR) below demand zone
SL_ATR_MULT = 1.5           # Or: ATR × 1.5 (whichever is tighter)

# ── TAKE PROFIT (Partial Exit Strategy) ───────────────────────────────────
# Split position into 3 equal parts, exit at:
#   TP1: entry + 1.0R → exit 1/3, move SL to breakeven
#   TP2: entry + 2.0R → exit 1/3, move SL to TP1 level
#   TP3: entry + 3.0R → exit final 1/3

TP1_R = 1.0                 # First take profit (risk units)
TP2_R = 2.0                 # Second take profit
TP3_R = 3.0                 # Final take profit
TP1_POSITION_PCT = 0.333    # Exit 1/3 at TP1
TP2_POSITION_PCT = 0.333    # Exit 1/3 at TP2
TP3_POSITION_PCT = 0.334    # Exit final 1/3 at TP3

# SL adjustments after partial exits
TP1_SL_LEVEL = "breakeven"  # Move SL to entry price after TP1
TP2_SL_LEVEL = "tp1"        # Move SL to TP1 level after TP2

# ── EXPECTED P&L OUTCOMES ──────────────────────────────────────────────────
# Based on which take profits are hit:
#
#   No TP hit (stopped at SL):     -1.0R  (full loss)
#   TP1 only (SL hit at BE):       +0.33R (1/3 at +1R, 2/3 at breakeven = avg +0.33R)
#   TP1+TP2 (SL hit at TP1):       +1.33R (1/3 at +1R, 1/3 at +2R, 1/3 at +1R = avg +1.33R)
#   All 3 TPs hit:                 +2.0R  (1/3 + 2/3 + 1R = 2R)

# ── BACKTEST PERFORMANCE (10-year, Combined Universe, 142 trades) ─────────
BACKTEST_BASELINE = {
    "trades": 142,
    "wins": 80,
    "losses": 62,
    "win_rate": 56.3,          # %
    "total_r": 15.75,          # risk units
    "avg_win_r": 0.972,        # risk units
    "avg_loss_r": -0.789,      # risk units
    "profit_factor": 1.99,
}

# Non-FCA Baseline (Original 213 stocks)
NON_FCA_BASELINE = {
    "trades": 122,
    "wins": 69,
    "losses": 53,
    "win_rate": 56.6,
    "total_r": 11.75,
    "avg_win_r": 0.938,
    "avg_loss_r": -0.785,
    "profit_factor": 1.78,
}

# FCA Addition (163 call-auction stocks, Daily V3.1)
FCA_ADDITION = {
    "trades": 20,
    "wins": 11,
    "losses": 9,
    "win_rate": 55.0,
    "total_r": 4.00,
    "avg_win_r": 1.182,
    "avg_loss_r": -0.889,
    "profit_factor": 2.56,
}

# ── ENTRY FILTERS (Hard Gates — All Must Pass) ─────────────────────────────
ENTRY_FILTERS = {
    "filter_1": {
        "name": "IHSG Market Gate",
        "condition": "IHSG score >= 50",
        "description": "Bull or Strong Bull regime only",
        "required": True,
    },
    "filter_2": {
        "name": "Daily SMC Bias",
        "condition": "BOS or CHoCH",
        "description": "Bullish structure on daily timeframe",
        "required": True,
    },
    "filter_3": {
        "name": "POI Demand Zone",
        "condition": "Price within ±5% of POI",
        "description": "Entry at pullback to support",
        "required": True,
    },
    "filter_4": {
        "name": "Rejection Candle",
        "condition": "Green candle or hammer",
        "description": "Buyers defending at POI",
        "required": True,
    },
    "filter_5": {
        "name": "Weekly SMC Alignment",
        "condition": "Weekly bias = Bullish",
        "description": "Multi-timeframe confirmation (26-week window)",
        "required": True,
    },
    "filter_6": {
        "name": "Price Trend Confirmation",
        "condition": "Close > MA20 & Volume >= 1.1x avg",
        "description": "Short-term uptrend + healthy volume",
        "required": True,
    },
}

# ── TRADE LIFECYCLE ────────────────────────────────────────────────────────
TRADE_STATES = {
    "ENTRY": "Signal detected, waiting for entry confirmation",
    "OPEN": "Position opened, tracking TP/SL levels",
    "PARTIAL_TP1": "TP1 hit, 1/3 exited, SL moved to breakeven",
    "PARTIAL_TP2": "TP2 hit, 2/3 exited, SL moved to TP1 level",
    "WIN": "All 3 TPs hit, position closed at TP3",
    "LOSS_SL": "Stopped out at SL (no partial exits)",
    "LOSS_TIMEOUT": "Exceeded MAX_HOLD, closed at market",
}

# ── DISPLAY CONFIGURATION ──────────────────────────────────────────────────
DISPLAY_METRICS = {
    "total_trades": "Total Trades",
    "win_rate": "Win Rate (%)",
    "avg_win_r": "Avg Win (R)",
    "avg_loss_r": "Avg Loss (R)",
    "profit_factor": "Profit Factor",
    "total_r": "Total P&L (R)",
}

def format_strategy_summary():
    """Returns a human-readable strategy summary for display"""
    return f"""
DAILY V3.1 STRATEGY — Live Implementation

ENTRY CRITERIA (All 6 filters must pass):
  1. IHSG Market Gate: Score >= 50 (Bull or Strong Bull)
  2. Daily SMC Bias: Bullish (BOS or CHoCH detected)
  3. POI Entry Zone: Price within ±5% of demand zone
  4. Rejection Candle: Green or hammer (buyers defending)
  5. Weekly Alignment: Bullish SMC on 26-week window
  6. Trend Confirmation: Price > MA20 + Volume >= 1.1x average

POSITION MANAGEMENT:
  • Entry: At price in POI zone with rejection candle
  • Stop Loss: Tighter of [POI-low × 0.985] or [ATR × 1.5]
  • Max Hold: {MAX_HOLD_BARS} bars (50 days)

PARTIAL EXIT STRATEGY (3-Part Split):
  • TP1 ({TP1_R}R): Exit 1/3, SL → Breakeven
  • TP2 ({TP2_R}R): Exit 1/3, SL → TP1 Level
  • TP3 ({TP3_R}R): Exit 1/3, Close Position

BACKTEST RESULTS (10-year, {BACKTEST_BASELINE['trades']} trades):
  • Win Rate: {BACKTEST_BASELINE['win_rate']}%
  • P&L: +{BACKTEST_BASELINE['total_r']}R
  • Profit Factor: {BACKTEST_BASELINE['profit_factor']}
  • Avg Win: +{BACKTEST_BASELINE['avg_win_r']}R
  • Avg Loss: {BACKTEST_BASELINE['avg_loss_r']}R
"""
