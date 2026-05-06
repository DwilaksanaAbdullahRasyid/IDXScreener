#!/usr/bin/env python
"""Quick hold time comparison: baseline (40) vs optimized (50)"""
import sys
sys.path.insert(0, 'D:\\SCREENER PROJECT')

# Suppress delisted stock warnings
import warnings
warnings.filterwarnings('ignore')

from dashboard import backtest

print("Hold Time Optimization: Baseline (40 bars) vs Test (50 bars)\n")

results = {}

for label, hold_value in [("Baseline", 40), ("Test +10", 50)]:
    print(f"Testing MAX_HOLD={hold_value} ({label})...")

    original_max_hold = backtest.MAX_HOLD
    backtest.MAX_HOLD = hold_value

    try:
        r = backtest.run_backtest(force=True)
        metrics = r.get('metrics', {})

        results[hold_value] = {
            'label': label,
            'trades': metrics.get('total_trades', 0),
            'win_rate': metrics.get('win_rate', 0),
            'avg_win_r': metrics.get('avg_win_r', 0),
            'avg_loss_r': metrics.get('avg_loss_r', 0),
            'total_r': metrics.get('total_r', 0),
            'profit_factor': metrics.get('profit_factor', 0),
            'avg_hold_days': metrics.get('avg_hold_days', 0),
            'max_drawdown_r': metrics.get('max_drawdown_r', 0),
            'grade_a_trades': r.get('grade_stats', {}).get('A', {}).get('trades', 0),
            'grade_a_wr': r.get('grade_stats', {}).get('A', {}).get('win_rate', 0),
            'grade_a_pnl': r.get('grade_stats', {}).get('A', {}).get('total_r', 0),
        }
        print(f"  Done: {metrics.get('total_trades')} trades, {metrics.get('win_rate')}% WR\n")
    finally:
        backtest.MAX_HOLD = original_max_hold

# Compare
baseline = results[40]
test = results[50]

print("=" * 90)
print("COMPARISON")
print("=" * 90)

metrics_labels = [
    ('Total Trades', 'trades', 'int'),
    ('Win Rate %', 'win_rate', 'float1'),
    ('Avg Win R', 'avg_win_r', 'float3'),
    ('Avg Loss R', 'avg_loss_r', 'float3'),
    ('Total P&L R', 'total_r', 'float2'),
    ('Profit Factor', 'profit_factor', 'float2'),
    ('Avg Hold Days', 'avg_hold_days', 'float1'),
    ('Max Drawdown R', 'max_drawdown_r', 'float2'),
]

print(f"\n{'Metric':<20} {'Baseline (40)':<20} {'Test +10 (50)':<20} {'Delta':<20}")
print("-" * 90)

for label, key, fmt_type in metrics_labels:
    b_val = baseline[key]
    t_val = test[key]

    if fmt_type == 'int':
        delta_str = f"{t_val - b_val:+d}"
        print(f"{label:<20} {b_val:<20} {t_val:<20} {delta_str:<20}")
    elif fmt_type == 'float1':
        delta_str = f"{t_val - b_val:+.1f}"
        print(f"{label:<20} {b_val:<20.1f} {t_val:<20.1f} {delta_str:<20}")
    elif fmt_type == 'float2':
        delta_str = f"{t_val - b_val:+.2f}"
        print(f"{label:<20} {b_val:<20.2f} {t_val:<20.2f} {delta_str:<20}")
    elif fmt_type == 'float3':
        delta_str = f"{t_val - b_val:+.3f}"
        print(f"{label:<20} {b_val:<20.3f} {t_val:<20.3f} {delta_str:<20}")

# Grade A breakdown
print(f"\n{'Grade A Only':<20} {'Baseline (40)':<20} {'Test +10 (50)':<20} {'Delta':<20}")
print("-" * 90)

for label, key in [('Grade A Trades', 'grade_a_trades'), ('Grade A WR %', 'grade_a_wr'), ('Grade A P&L R', 'grade_a_pnl')]:
    b_val = baseline[key]
    t_val = test[key]
    if 'Trades' in label:
        delta_str = f"{t_val - b_val:+d}"
        print(f"{label:<20} {b_val:<20} {t_val:<20} {delta_str:<20}")
    else:
        delta_str = f"{t_val - b_val:+.1f}"
        print(f"{label:<20} {b_val:<20.1f} {t_val:<20.1f} {delta_str:<20}")

# Recommendation
print("\n" + "=" * 90)
if test['total_r'] > baseline['total_r']:
    improvement = test['total_r'] - baseline['total_r']
    improvement_pct = (improvement / abs(baseline['total_r']) * 100) if baseline['total_r'] != 0 else 0
    print(f"RECOMMENDATION: Use MAX_HOLD=50 bars")
    print(f"  P&L improvement: +{improvement:.2f}R ({improvement_pct:+.1f}%)")
    print(f"  Expected: Better hold of trades, more time for TP3 to develop")
elif test['total_r'] < baseline['total_r']:
    decline = baseline['total_r'] - test['total_r']
    decline_pct = (decline / abs(baseline['total_r']) * 100) if baseline['total_r'] != 0 else 0
    print(f"RECOMMENDATION: Keep MAX_HOLD=40 bars (baseline)")
    print(f"  P&L better at baseline: -{decline:.2f}R ({decline_pct:+.1f}%)")
    print(f"  Reason: Longer holds allow more reversals, reducing WR")
else:
    print("RECOMMENDATION: Results are similar, keep baseline (40 bars)")

print("=" * 90)
