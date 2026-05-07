"""
DUAL-STRATEGY BACKTEST: FCA vs Non-FCA
- Non-FCA: Daily V3.1 strategy on original ~185 stock baseline
- FCA: Daily V3.1 strategy on 163 FCA stocks
- Combined: Shows universe expansion improvement
"""

import json
import time
from pathlib import Path

from .analysis import IDX_UNIVERSE_NONFCA, IDX_UNIVERSE_FCA
from .backtest import run_backtest

BASE_DIR = Path(__file__).resolve().parent.parent
BT_CACHE_DUAL = BASE_DIR / "backtest_dual_cache.json"

print("=" * 120)
print("DUAL-STRATEGY BACKTEST: Non-FCA Baseline + FCA Expansion")
print("=" * 120)
print(f"\nUniverse Split:")
print(f"  Non-FCA (Original Baseline): {len(IDX_UNIVERSE_NONFCA)} stocks")
print(f"  FCA (New Expansion): {len(IDX_UNIVERSE_FCA)} stocks")
print(f"  Total: {len(IDX_UNIVERSE_NONFCA) + len(IDX_UNIVERSE_FCA)} stocks")
print(f"\nOriginal Baseline (Daily V3.1 on ~185 non-FCA):")
print(f"  Expected: ~114 trades, 56.4% WR, +11.42R P&L")


def run_dual_backtest(force=False):
    """
    Run separate backtests on Non-FCA and FCA universes, then combine.

    Non-FCA: Uses original ~185 stock universe (Daily V3.1 baseline)
    FCA: Uses 163 FCA stocks (Daily V3.1 applied to call-auction stocks)
    """

    # Check cache
    if not force and BT_CACHE_DUAL.exists():
        try:
            with open(BT_CACHE_DUAL, "r") as f:
                cached = json.load(f)
                if time.time() - cached.get("timestamp", 0) < 86400:
                    print("\n[CACHE HIT] Loading dual backtest results from cache...")
                    return cached
        except Exception:
            pass

    print("\n[RUNNING] Backtest 1: Non-FCA baseline (~185 stocks)...")
    non_fca_result = run_backtest(tickers=IDX_UNIVERSE_NONFCA, force=True)
    non_fca_trades = non_fca_result.get("trades", [])

    print("\n[RUNNING] Backtest 2: FCA expansion (163 call-auction stocks)...")
    fca_result = run_backtest(tickers=IDX_UNIVERSE_FCA, force=True)
    fca_trades = fca_result.get("trades", [])

    # Combine all trades
    all_trades = non_fca_trades + fca_trades
    all_trades.sort(key=lambda x: x.get("entry_date", ""))

    # Calculate per-group metrics
    def calc_metrics(trades):
        if not trades:
            return {
                "trades": 0, "wins": 0, "losses": 0, "wr": 0,
                "total_r": 0, "avg_win": 0, "avg_loss": 0, "pf": 0
            }

        wins = [t for t in trades if t["outcome"] == "WIN"]
        losses = [t for t in trades if t["outcome"] == "LOSS"]
        total_r = sum(t["pnl_r"] for t in trades)
        total_wins = sum(t["pnl_r"] for t in wins) if wins else 0
        total_losses = sum(t["pnl_r"] for t in losses) if losses else 0

        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "wr": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "total_r": round(total_r, 2),
            "avg_win": round(total_wins / len(wins), 3) if wins else 0,
            "avg_loss": round(total_losses / len(losses), 3) if losses else 0,
            "pf": round(total_wins / abs(total_losses), 2) if total_losses < 0 else 0,
        }

    non_fca_stats = calc_metrics(non_fca_trades)
    fca_stats = calc_metrics(fca_trades)
    combined_stats = calc_metrics(all_trades)

    # Print detailed results
    print("\n" + "=" * 120)
    print("RESULTS: Dual-Strategy Backtest")
    print("=" * 120)

    print(f"\n1. NON-FCA BASELINE (Original ~185 stocks with Daily V3.1):")
    print(f"   Trades: {non_fca_stats['trades']}")
    print(f"   Wins/Losses: {non_fca_stats['wins']}/{non_fca_stats['losses']}")
    print(f"   Win Rate: {non_fca_stats['wr']}%")
    print(f"   Total P&L: {non_fca_stats['total_r']:+.2f}R")
    print(f"   Avg Win/Loss: {non_fca_stats['avg_win']:+.3f}R / {non_fca_stats['avg_loss']:+.3f}R")
    print(f"   Profit Factor: {non_fca_stats['pf']}")

    print(f"\n2. FCA EXPANSION (163 call-auction stocks with Daily V3.1):")
    print(f"   Trades: {fca_stats['trades']}")
    print(f"   Wins/Losses: {fca_stats['wins']}/{fca_stats['losses']}")
    print(f"   Win Rate: {fca_stats['wr']}%")
    print(f"   Total P&L: {fca_stats['total_r']:+.2f}R")
    print(f"   Avg Win/Loss: {fca_stats['avg_win']:+.3f}R / {fca_stats['avg_loss']:+.3f}R")
    print(f"   Profit Factor: {fca_stats['pf']}")

    print(f"\n3. COMBINED (Non-FCA + FCA, ~350 stocks):")
    print(f"   Trades: {combined_stats['trades']}")
    print(f"   Wins/Losses: {combined_stats['wins']}/{combined_stats['losses']}")
    print(f"   Win Rate: {combined_stats['wr']}%")
    print(f"   Total P&L: {combined_stats['total_r']:+.2f}R")
    print(f"   Avg Win/Loss: {combined_stats['avg_win']:+.3f}R / {combined_stats['avg_loss']:+.3f}R")
    print(f"   Profit Factor: {combined_stats['pf']}")

    # Comparison
    print(f"\n4. COMPARISON TO ORIGINAL BASELINE (114 trades, 56.4% WR, +11.42R):")
    baseline_pnl = 11.42
    if non_fca_stats["trades"] > 0:
        non_fca_diff = non_fca_stats["total_r"] - baseline_pnl
        non_fca_pct = (non_fca_diff / baseline_pnl * 100) if baseline_pnl != 0 else 0
        print(f"   Non-FCA vs Baseline: {non_fca_stats['total_r']:+.2f}R ({non_fca_diff:+.2f}R, {non_fca_pct:+.1f}%)")

    if fca_stats["trades"] > 0:
        print(f"   FCA Addition: +{fca_stats['total_r']:.2f}R from {fca_stats['trades']} trades")

    combined_diff = combined_stats["total_r"] - baseline_pnl
    combined_pct = (combined_diff / baseline_pnl * 100) if baseline_pnl != 0 else 0
    print(f"   Combined vs Baseline: {combined_stats['total_r']:+.2f}R ({combined_diff:+.2f}R, {combined_pct:+.1f}%)")

    result = {
        "timestamp": time.time(),
        "metrics": {
            "non_fca": non_fca_stats,
            "fca": fca_stats,
            "combined": combined_stats,
            "baseline": {"trades": 114, "wr": 56.4, "total_r": 11.42},
        },
        "trades": {
            "non_fca": non_fca_trades,
            "fca": fca_trades,
            "combined": all_trades,
        },
        "status": "COMPLETE: Separate non-FCA baseline + FCA expansion backtest"
    }

    # Cache result
    try:
        with open(BT_CACHE_DUAL, "w") as f:
            json.dump(result, f, indent=2, default=str)
    except Exception:
        pass

    return result


# Execute when run as main
if __name__ == "__main__":
    result = run_dual_backtest(force=True)

    print("\n" + "=" * 120)
    print("BACKTEST COMPLETE")
    print("=" * 120)
    print(f"\nResults cached to: {BT_CACHE_DUAL}")
    print("\n✓ Restored original non-FCA baseline")
    print("✓ Evaluated FCA universe separately")
    print("✓ Measured universe expansion benefit")
    print("\nNext Steps:")
    print("  1. FCA Tier 1: Integrate Elliott Wave + weekly confirmation")
    print("  2. FCA Tier 2: Optimize 20-day MA + ±7% POI for daily entries")
    print("  3. Target: FCA trades should exceed daily strategy performance")
    print("\n" + "=" * 120)
