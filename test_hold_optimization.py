#!/usr/bin/env python
"""Phase 3C: Hold Time Optimization Test
Test MAX_HOLD values: 30, 40 (baseline), 50, 60 bars
"""
import sys
sys.path.insert(0, 'D:\\SCREENER PROJECT')

from dashboard import backtest
import json

print("=" * 100)
print("PHASE 3C: HOLD TIME OPTIMIZATION")
print("=" * 100)
print("\nTesting MAX_HOLD values: 30, 40 (baseline), 50, 60 bars\n")

results = {}

for hold_value in [30, 40, 50, 60]:
    print(f"Testing MAX_HOLD={hold_value} bars...")

    # Temporarily override MAX_HOLD
    original_max_hold = backtest.MAX_HOLD
    backtest.MAX_HOLD = hold_value

    try:
        # Clear cache to force fresh backtest
        backtest._load_bt_cache = lambda: None
        r = backtest.run_backtest(force=True)

        metrics = r.get('metrics', {})

        results[hold_value] = {
            'trades': metrics.get('total_trades', 0),
            'win_rate': metrics.get('win_rate', 0),
            'avg_win_r': metrics.get('avg_win_r', 0),
            'avg_loss_r': metrics.get('avg_loss_r', 0),
            'total_r': metrics.get('total_r', 0),
            'profit_factor': metrics.get('profit_factor', 0),
            'expectancy_r': metrics.get('expectancy_r', 0),
            'avg_hold_days': metrics.get('avg_hold_days', 0),
            'max_drawdown_r': metrics.get('max_drawdown_r', 0),
        }
        print(f"  OK: {metrics.get('total_trades')} trades, {metrics.get('win_rate')}% WR, {metrics.get('total_r')}R P&L\n")
    except Exception as e:
        print(f"  ERROR: {str(e)[:100]}\n")
        results[hold_value] = None
    finally:
        backtest.MAX_HOLD = original_max_hold

# Display results comparison
print("\n" + "=" * 100)
print("RESULTS COMPARISON")
print("=" * 100)

print("\n{:<15} {:<15} {:<12} {:<12} {:<12} {:<12} {:<12} {:<12} {:<12}".format(
    "MAX_HOLD", "Trades", "Win Rate %", "Avg Win R", "Avg Loss R", "Total P&L R", "Profit Factor", "Expectancy R", "Avg Hold Days"
))
print("-" * 100)

for hold_value in [30, 40, 50, 60]:
    if results[hold_value] is None:
        continue

    r = results[hold_value]
    print("{:<15} {:<15} {:<12.1f} {:<12.3f} {:<12.3f} {:<12.2f} {:<12.2f} {:<12.3f} {:<12.1f}".format(
        f"{hold_value} bars",
        r['trades'],
        r['win_rate'],
        r['avg_win_r'],
        r['avg_loss_r'],
        r['total_r'],
        r['profit_factor'],
        r['expectancy_r'],
        r['avg_hold_days'],
    ))

# Analysis
print("\n" + "=" * 100)
print("ANALYSIS")
print("=" * 100)

baseline = results[40]
if baseline:
    print(f"\nBaseline (MAX_HOLD=40 bars): {baseline['total_r']}R P&L, {baseline['win_rate']}% WR, {baseline['profit_factor']:.2f} PF")

    print("\nComparisons to baseline:")
    for hold_value in [30, 50, 60]:
        if results[hold_value] is None:
            continue

        r = results[hold_value]
        pnl_delta = r['total_r'] - baseline['total_r']
        pnl_pct_change = (pnl_delta / baseline['total_r'] * 100) if baseline['total_r'] != 0 else 0
        trade_delta = r['trades'] - baseline['trades']

        direction = "UP" if pnl_delta > 0 else "DOWN"
        print(f"\n  MAX_HOLD={hold_value}: {direction} {abs(pnl_delta):.2f}R ({pnl_pct_change:+.1f}%)")
        print(f"    Trades: {r['trades']} ({trade_delta:+d} vs baseline)")
        print(f"    Win Rate: {r['win_rate']:.1f}% ({r['win_rate'] - baseline['win_rate']:+.1f}pp)")
        print(f"    Avg Hold: {r['avg_hold_days']:.1f} days ({r['avg_hold_days'] - baseline['avg_hold_days']:+.1f}d)")

    # Find optimal
    sorted_results = sorted(
        [(hold, r) for hold, r in results.items() if r is not None],
        key=lambda x: x[1]['total_r'],
        reverse=True
    )

    best_hold = sorted_results[0][0]
    best_result = sorted_results[0][1]

    print(f"\n{'='*100}")
    print(f"RECOMMENDATION: MAX_HOLD={best_hold} bars")
    print(f"  Reason: {best_result['total_r']}R P&L ({best_result['win_rate']:.1f}% WR, {best_result['profit_factor']:.2f} PF)")
    print(f"  Improvement: {best_result['total_r'] - baseline['total_r']:+.2f}R ({((best_result['total_r'] - baseline['total_r']) / baseline['total_r'] * 100):+.1f}%) vs baseline")
    print(f"{'='*100}")

print("\nDone!")
