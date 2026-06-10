#!/usr/bin/env python
"""
Pre-generate backtest cache on first setup.

This script should be run once after initial installation to generate
the backtest cache file. This allows the server to start instantly
without needing to recompute the 10-year backtest on first load.

Usage:
    python setup_backtest_cache.py

The script will:
1. Initialize Django
2. Run the full 10-year dual backtest (Non-FCA + FCA)
3. Save results to backtest_dual_cache.json
4. Display summary metrics

This typically takes 30-60 seconds to complete.
"""

import sys
import os

# Set up Django environment
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'screener.settings')

import django
django.setup()

from dashboard.backtest_dual import run_dual_backtest

def main():
    print("=" * 70)
    print("STIX Backtest Cache Initialization")
    print("=" * 70)
    print()
    print("Generating initial backtest cache...")
    print("This will take 30-60 seconds. Please wait...")
    print()

    try:
        result = run_dual_backtest(force=True)

        print()
        print("✓ Backtest cache generated successfully!")
        print()
        print("Combined Strategy Results:")
        print("-" * 70)

        m = result['metrics']['combined']
        print(f"  Total Trades:    {m['trades']}")
        print(f"  Wins / Losses:   {m['wins']} / {m['losses']}")
        print(f"  Win Rate:        {m['wr']:.1f}%")
        print(f"  Total P&L:       +{m['total_r']:.2f}R")
        print(f"  Profit Factor:   {m.get('pf', 0):.2f}")
        print(f"  Avg Win:         +{m['avg_win']:.2f}R")
        print(f"  Avg Loss:        -{abs(m['avg_loss']):.2f}R")
        print()

        print("Non-FCA Universe Results:")
        print("-" * 70)
        m_non_fca = result['metrics']['non_fca']
        print(f"  Total Trades:    {m_non_fca['trades']}")
        print(f"  Win Rate:        {m_non_fca['wr']:.1f}%")
        print(f"  Total P&L:       +{m_non_fca['total_r']:.2f}R")
        print()

        print("FCA Universe Results:")
        print("-" * 70)
        m_fca = result['metrics']['fca']
        print(f"  Total Trades:    {m_fca['trades']}")
        print(f"  Win Rate:        {m_fca['wr']:.1f}%")
        print(f"  Total P&L:       +{m_fca['total_r']:.2f}R")
        print()

        print("=" * 70)
        print("Setup complete! You can now start the Django server.")
        print("The landing page will load instantly using the cached backtest.")
        print("=" * 70)

    except Exception as e:
        print()
        print("✗ Error during backtest generation:")
        print(f"  {str(e)}")
        print()
        print("Please check your configuration and try again.")
        sys.exit(1)


if __name__ == '__main__':
    main()
