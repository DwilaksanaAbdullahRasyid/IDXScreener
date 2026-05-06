#!/usr/bin/env python
"""Test if backtest data can be serialized to JSON"""
import sys
sys.path.insert(0, 'D:\\SCREENER PROJECT')
import json
import warnings
warnings.filterwarnings('ignore')

from dashboard.backtest import run_backtest

print("Testing JSON serialization of backtest data...\n")

try:
    r = run_backtest(force=False)  # Use cache if available
    print("✓ Backtest completed\n")

    # Try to serialize to JSON
    print("Attempting JSON serialization...")
    json_str = json.dumps(r, indent=2)
    print("✓ JSON serialization successful\n")

    # Check size
    size_mb = len(json_str) / (1024 * 1024)
    print(f"JSON size: {size_mb:.2f} MB")

    # Parse back to verify
    parsed = json.loads(json_str)
    print(f"✓ JSON parsing successful")
    print(f"✓ Trades in parsed data: {len(parsed.get('trades', []))}")

except json.JSONDecodeError as e:
    print(f"✗ JSON encoding error: {e}")
    print(f"  At line {e.lineno}, column {e.colno}: {e.msg}")
except Exception as e:
    print(f"✗ Error: {e}")
    import traceback
    traceback.print_exc()
