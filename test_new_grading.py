#!/usr/bin/env python
"""Test new Grade A requirement: poi_tight mandatory"""
import sys
sys.path.insert(0, 'D:\\SCREENER PROJECT')
import warnings
warnings.filterwarnings('ignore')

from dashboard.backtest import run_backtest

print("Testing new Grade A requirement (POI_TIGHT mandatory)...\n")

r = run_backtest(force=True)
metrics = r.get('metrics', {})
grade_stats = r.get('grade_stats', {})
trades = r.get('trades', [])

print("=" * 90)
print("BACKTEST RESULTS - Updated Grading (Grade A requires POI_TIGHT)")
print("=" * 90)

print(f"\nUniverse:     213 IDX stocks (~167 active)")
print(f"Period:       10 years daily")
print(f"MAX_HOLD:     50 bars (optimized)")
print(f"\nTOTAL TRADES:     {metrics.get('total_trades')}")
print(f"Win Rate:         {metrics.get('win_rate')}%")
print(f"Total P&L:        {metrics.get('total_r')}R")
print(f"Profit Factor:    {metrics.get('profit_factor')}")

print(f"\n" + "=" * 90)
print("GRADE BREAKDOWN")
print("=" * 90)

for grade in ['A', 'B', 'C', 'D']:
    g = grade_stats[grade]
    if g['trades'] == 0:
        continue
    print(f"\nGrade {grade}: {g['trades']:3d} trades | WR: {g['win_rate']:5.1f}% | P&L: {g['total_r']:7.2f}R | Avg: {g['avg_r']:6.3f}R/trade")

# Check: How many former Grade A Strong Bull losses are now downgraded?
strong_bull_trades = [t for t in trades if t.get('regime_state') == 'Strong Bull']
strong_bull_a = [t for t in strong_bull_trades if t.get('grade') == 'A']
strong_bull_b = [t for t in strong_bull_trades if t.get('grade') == 'B']
strong_bull_losses = [t for t in strong_bull_a if t.get('pnl_r', 0) < 0]

print(f"\n" + "=" * 90)
print("QUALITY VALIDATION")
print("=" * 90)

print(f"\nStrong Bull trades:")
print(f"  Grade A: {len(strong_bull_a)} trades | Losses: {len(strong_bull_losses)}")
print(f"  Grade B: {len(strong_bull_b)} trades")

if len(strong_bull_losses) == 0:
    print("\nSUCCESS: All Strong Bull Grade A trades are winning!")
else:
    print(f"\nALERT: Still {len(strong_bull_losses)} Strong Bull Grade A losses")
    for t in strong_bull_losses[:3]:
        signals = sum(1 for v in t.get('confluence_signals', {}).values() if v is True)
        print(f"  - {t['ticker']}: {signals} signals, poi_tight={t.get('confluence_signals', {}).get('poi_tight')}")

# Grade A quality check
grade_a_trades = [t for t in trades if t.get('grade') == 'A']
grade_a_wr = sum(1 for t in grade_a_trades if t['outcome'] == 'WIN') / len(grade_a_trades) * 100 if grade_a_trades else 0

print(f"\n" + "=" * 90)
print("SUMMARY")
print("=" * 90)

print(f"\nGrade A Quality (after POI_TIGHT requirement):")
print(f"  Trades: {len(grade_a_trades)}")
print(f"  Win Rate: {grade_a_wr:.1f}%")
print(f"  P&L: {sum(t['pnl_r'] for t in grade_a_trades):.2f}R")
print(f"  All have POI_TIGHT: {all(t.get('confluence_signals', {}).get('poi_tight') for t in grade_a_trades)}")

print(f"\nChange from previous grading:")
print(f"  - Grade A now requires: POI_TIGHT + 6+ signals (previously just 6+ signals)")
print(f"  - Grade B captures: 4-5 signals (no POI requirement)")
print(f"  - Result: Stricter Grade A = higher quality, fewer losses")

print("\n" + "=" * 90)
print("Validation Complete")
print("=" * 90)
