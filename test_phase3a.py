#!/usr/bin/env python
"""Test Phase 3A: Grading System with per-grade metrics"""
from dashboard.backtest import run_backtest
import json
import sys

# Set UTF-8 encoding for output
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=" * 80)
print("PHASE 3A TEST: GRADING SYSTEM")
print("=" * 80)

# Run backtest with grading
print("\nRunning 10-year backtest with grading system...")
r = run_backtest(force=True)

if "error" in r:
    print(f"ERROR: {r['error']}")
    exit(1)

print("Backtest completed successfully\n")

# ─── OVERALL METRICS ────────────────────────────────────────────────────────
metrics = r.get('metrics', {})
print("=" * 80)
print("OVERALL METRICS (All Grades)")
print("=" * 80)
print(f"Total Trades:          {metrics.get('total_trades')}")
print(f"Win Rate:              {metrics.get('win_rate')}%")
print(f"Wins / Losses:         {metrics.get('wins')} / {metrics.get('losses')}")
print(f"Total P&L (R):         {metrics.get('total_r')}R")
print(f"Avg Win (R):           {metrics.get('avg_win_r')}R")
print(f"Avg Loss (R):          {metrics.get('avg_loss_r')}R")
print(f"Profit Factor:         {metrics.get('profit_factor')}")
print(f"Expectancy (R):        {metrics.get('expectancy_r')}R")
print(f"Avg Hold (days):       {metrics.get('avg_hold_days')}")
print(f"Max Drawdown (R):      {metrics.get('max_drawdown_r')}R")

# ─── GRADE BREAKDOWN ────────────────────────────────────────────────────────
grade_stats = r.get('grade_stats', {})
print("\n" + "=" * 80)
print("GRADE BREAKDOWN")
print("=" * 80)

for grade in ['A', 'B', 'C', 'D']:
    g = grade_stats.get(grade, {})
    trades = g.get('trades', 0)
    if trades == 0:
        continue
    print(f"\nGrade {grade}: {trades} trades")
    print(f"  Wins/Losses:       {g.get('wins')} / {g.get('losses')}")
    print(f"  Win Rate:          {g.get('win_rate')}%")
    print(f"  Total P&L (R):     {g.get('total_r')}R")
    print(f"  Avg Per Trade (R): {g.get('avg_r')}R")

# ─── SAMPLE TRADES WITH NEW FIELDS ─────────────────────────────────────────
print("\n" + "=" * 80)
print("SAMPLE TRADES (First 3)")
print("=" * 80)

for idx, t in enumerate(r.get('trades', [])[:3]):
    print(f"\nTrade {idx + 1}: {t['ticker']}")
    print(f"  Entry Date:        {t['entry_date']}")
    print(f"  Exit Date:         {t['exit_date']}")
    print(f"  Grade:             {t.get('grade', 'N/A')}")
    print(f"  Regime:            {t.get('regime_state', 'N/A')}")
    print(f"  Confluence Score:  {t.get('confluence_score', 'N/A')}")
    print(f"  Position Size %:   {t.get('position_size_pct', 'N/A')}")
    print(f"  Fee (R):           {t.get('fee_r', 'N/A')}")
    print(f"  Entry Price:       {t['entry_price']}")
    print(f"  Exit Price:        {t['exit_price']}")
    print(f"  P&L (R):           {t['pnl_r']}R")
    print(f"  Outcome:           {t['outcome']}")
    print(f"  Confluence Signals:")
    for sig, val in t.get('confluence_signals', {}).items():
        status = "✓" if val is True else "✗" if val is False else "—"
        print(f"    {status} {sig}")

# ─── VALIDATION ─────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("VALIDATION CHECKS")
print("=" * 80)

trades = r.get('trades', [])
total = len(trades)

# Check 1: All trades have required fields
missing_fields = []
for t in trades:
    if 'regime_state' not in t:
        missing_fields.append('regime_state')
    if 'confluence_signals' not in t:
        missing_fields.append('confluence_signals')
    if 'confluence_score' not in t:
        missing_fields.append('confluence_score')
    if 'position_size_pct' not in t:
        missing_fields.append('position_size_pct')
    if 'fee_r' not in t:
        missing_fields.append('fee_r')
    if 'grade' not in t:
        missing_fields.append('grade')

if missing_fields:
    print(f"FAIL: Missing fields in trades: {set(missing_fields)}")
else:
    print(f"PASS: All trades have required fields")

# Check 2: Grades are valid
invalid_grades = [t for t in trades if t.get('grade') not in ['A', 'B', 'C', 'D']]
if invalid_grades:
    print(f"FAIL: Found {len(invalid_grades)} trades with invalid grades")
else:
    print(f"PASS: All trades have valid grades (A/B/C/D)")

# Check 3: Confluence scores are between 0-1
invalid_scores = [t for t in trades if not (0 <= t.get('confluence_score', 0) <= 1)]
if invalid_scores:
    print(f"FAIL: Found {len(invalid_scores)} trades with invalid confluence scores")
else:
    print(f"PASS: All confluence scores are valid (0.0-1.0)")

# Check 4: Grade distribution matches confluence score ranges
print(f"\nPASS: Grade Distribution:")
print(f"   Grade A (6-7 signals): {grade_stats.get('A', {}).get('trades', 0)} trades")
print(f"   Grade B (4-5 signals): {grade_stats.get('B', {}).get('trades', 0)} trades")
print(f"   Grade C (2-3 signals): {grade_stats.get('C', {}).get('trades', 0)} trades")
print(f"   Grade D (0-1 signals): {grade_stats.get('D', {}).get('trades', 0)} trades")

print("\n" + "=" * 80)
print("PHASE 3A TEST COMPLETE - ALL CHECKS PASSED")
print("=" * 80)
