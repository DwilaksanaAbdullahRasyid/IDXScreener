#!/usr/bin/env python
"""Quick test of grading system"""
from dashboard.backtest import run_backtest
import json

print("Running 10-year backtest with grading system...")
r = run_backtest(force=True)

print("\n=== GRADE STATS ===")
print(json.dumps(r.get('grade_stats'), indent=2))

print(f"\n=== BACKTEST SUMMARY ===")
print(f"Total Trades: {len(r['trades'])}")
print(f"Grade Distribution: A={r['grade_stats']['A']['trades']}, B={r['grade_stats']['B']['trades']}, C={r['grade_stats']['C']['trades']}, D={r['grade_stats']['D']['trades']}")

if r['trades']:
    print(f"\n=== SAMPLE TRADE (with new fields) ===")
    t = r['trades'][0]
    print(f"Ticker: {t['ticker']}")
    print(f"Regime: {t.get('regime_state', 'N/A')}")
    print(f"Confluence Score: {t.get('confluence_score', 'N/A')}")
    print(f"Grade: {t.get('grade', 'N/A')}")
    print(f"Position Size %: {t.get('position_size_pct', 'N/A')}")
    print(f"Fee R: {t.get('fee_r', 'N/A')}")
    print(f"Confluence Signals:")
    for sig, val in t.get('confluence_signals', {}).items():
        print(f"  {sig}: {val}")

print(f"\n=== METRICS ===")
metrics = r.get('metrics', {})
print(f"Total Trades: {metrics.get('total_trades')}")
print(f"Win Rate: {metrics.get('win_rate')}%")
print(f"Avg Win: {metrics.get('avg_win_r')}R")
print(f"Profit Factor: {metrics.get('profit_factor')}")
print(f"Total P&L: {metrics.get('total_r')}R")
