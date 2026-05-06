#!/usr/bin/env python
"""Find Grade A Strong Bull trades that lost money"""
import sys
sys.path.insert(0, 'D:\\SCREENER PROJECT')
import warnings
warnings.filterwarnings('ignore')

from dashboard.backtest import run_backtest
import json

print("=" * 100)
print("COUNCIL ANALYSIS: Grade A Strong Bull Losses")
print("=" * 100)

# Load backtest results
print("\nLoading backtest results...")
r = run_backtest(force=False)

trades = r.get('trades', [])
print(f"Total trades: {len(trades)}\n")

# Filter: Grade A + Strong Bull regime + Losing
problem_trades = [
    t for t in trades
    if t.get('grade') == 'A'
    and t.get('regime_state') == 'Strong Bull'
    and t.get('pnl_r', 0) < 0
]

print(f"Grade A Strong Bull LOSSES: {len(problem_trades)} trades\n")

if not problem_trades:
    print("OK: No Grade A Strong Bull losses found! Strategy is working correctly.\n")
else:
    print(f"ALERT: Found {len(problem_trades)} losing Grade A Strong Bull trades\n")

    # Analyze each problem trade
    print("=" * 100)
    print("DETAILED ANALYSIS")
    print("=" * 100)

    for idx, t in enumerate(problem_trades, 1):
        print(f"\n[{idx}] {t['ticker']} — {t['entry_date']} to {t['exit_date']}")
        print(f"    Grade: {t['grade']} | Regime: {t['regime_state']}")
        print(f"    Entry: {t['entry_price']:.0f} | Exit: {t['exit_price']:.0f} | SL: {t['sl']:.0f}")
        print(f"    P&L: {t['pnl_r']}R ({t['pnl_pct']:.2f}%)")
        print(f"    Hold: {t['hold_days']} days")
        print(f"    TP Hits: TP1={t['tp1_hit']}, TP2={t['tp2_hit']}, TP3={t['tp3_hit']}")

        # Analyze confluence signals
        signals = t.get('confluence_signals', {})
        true_signals = [k for k, v in signals.items() if v is True]
        print(f"    Confluence Score: {t.get('confluence_score', 0):.1f} ({len(true_signals)}/10 signals)")
        print(f"    True Signals: {', '.join(true_signals) if true_signals else 'NONE'}")

    # Summary statistics
    print("\n" + "=" * 100)
    print("SUMMARY STATISTICS")
    print("=" * 100)

    total_loss = sum(t['pnl_r'] for t in problem_trades)
    avg_loss = total_loss / len(problem_trades) if problem_trades else 0
    worst = min(problem_trades, key=lambda t: t['pnl_r']) if problem_trades else None

    print(f"\nTotal Loss: {total_loss:.2f}R")
    print(f"Avg Loss per Trade: {avg_loss:.2f}R")
    print(f"Worst Trade: {worst['ticker']} ({worst['pnl_r']}R)" if worst else "N/A")

    # Identify root causes
    print("\n" + "=" * 100)
    print("ROOT CAUSE ANALYSIS")
    print("=" * 100)

    # Check: Are these trades hitting SL frequently?
    sl_hits = sum(1 for t in problem_trades if t['outcome'] == 'LOSS' and not t['tp1_hit'])
    print(f"\nTrades stopped at SL (no TP1): {sl_hits}/{len(problem_trades)}")

    # Check: Do they have low confluence despite being Grade A?
    low_confluence = [t for t in problem_trades if t.get('confluence_score', 0) < 0.5]
    print(f"Trades with low confluence (<0.5): {len(low_confluence)}/{len(problem_trades)}")

    # Check: Which signals are missing in problem trades?
    all_signals = {}
    for t in problem_trades:
        for sig, val in t.get('confluence_signals', {}).items():
            all_signals[sig] = all_signals.get(sig, 0) + (1 if val else 0)

    print(f"\nSignal presence in problem trades:")
    for sig, count in sorted(all_signals.items(), key=lambda x: x[1]):
        pct = (count / len(problem_trades)) * 100
        print(f"  {sig}: {count}/{len(problem_trades)} ({pct:.0f}%)")

    # Recommendations
    print("\n" + "=" * 100)
    print("RECOMMENDATIONS")
    print("=" * 100)

    missing_signals = [sig for sig, count in all_signals.items() if count < len(problem_trades) * 0.5]

    if missing_signals:
        print(f"\n1. Signals frequently missing in losses:")
        for sig in missing_signals:
            print(f"   - {sig}: Consider requiring this signal for Grade A trades")

    if sl_hits > len(problem_trades) * 0.7:
        print(f"\n2. SL hits are common ({sl_hits}/{len(problem_trades)}):")
        print(f"   - Consider widening SL (currently {r['params'].get('sl_factor')} of POI)")
        print(f"   - Or tightening POI band (currently ±{r['params'].get('poi_band')*100:.0f}%)")

    print(f"\n3. Consider re-grading: Some Grade A trades have low confluence")
    print(f"   - Adjust grade thresholds if needed")

print("\n" + "=" * 100)
print("Analysis Complete")
print("=" * 100)
