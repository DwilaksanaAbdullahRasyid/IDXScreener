#!/usr/bin/env python
"""Simple test of JSON serialization"""
import sys
sys.path.insert(0, 'D:\\SCREENER PROJECT')
import json
import warnings
warnings.filterwarnings('ignore')

from dashboard.backtest import run_backtest

print("Testing JSON serialization...\n")

try:
    r = run_backtest(force=False)
    print("Backtest completed")

    # Try to serialize
    json_str = json.dumps(r)
    print(f"SUCCESS: Serialized {len(r['trades'])} trades to JSON")
    print(f"JSON size: {len(json_str) / 1024:.1f} KB")

    # Check trades have confluence_signals
    t = r['trades'][0]
    print(f"\nFirst trade has confluence_signals: {'confluence_signals' in t}")
    print(f"Confluence signals type: {type(t.get('confluence_signals'))}")

except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
