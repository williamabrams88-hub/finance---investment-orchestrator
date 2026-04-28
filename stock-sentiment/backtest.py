#!/usr/bin/env python3
"""
backtest.py — 60-day price-divergence backtest

Phase A (0 API calls): test price-dip entry (condition 3) on raw dip days.
Phase B (5 API calls): add today's sentiment as a fixed filter.

Usage:
    python backtest.py              # Phase A only, 0 API calls
    python backtest.py --days 60   # same, explicit
    python backtest.py --score     # Phase A + B, 5 API calls
"""

import argparse
import csv
import datetime
import json
import sys
from pathlib import Path

import yfinance as yf

import config
from pipeline import fetch_headlines, score_sentiment, store_score, REPORTS_DIR

# ── Backtest parameters ────────────────────────────────────────────────────────
TAKE_PROFIT_PCT = 5.0   # exit when trade up 5%
STOP_LOSS_PCT   = 3.0   # exit when trade down 3%
MAX_HOLD_DAYS   = 21    # calendar days before forced time-stop exit
DIP_THRESHOLD   = -2.0  # price drop % to trigger entry (mirrors config.DIVERGENCE_PRICE_DROP_PCT)
SENTIMENT_FLOOR = 0.3   # min sentiment score to include ticker in Phase B


def fetch_ohlc(ticker, days):
    """Fetch OHLC history for ticker covering last `days` calendar days."""
    # Fetch extra buffer to account for weekends/holidays
    df = yf.Ticker(ticker).history(period=f"{days + 10}d")
    if df.empty:
        return []
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    rows = []
    for date, row in df.iterrows():
        if date.date() >= cutoff:
            rows.append({
                "date":  date.date(),
                "open":  row["Open"],
                "high":  row["High"],
                "low":   row["Low"],
                "close": row["Close"],
            })
    return rows  # sorted ascending by yfinance


def simulate_trades(ticker, ohlc, filter_label, sentiment_score=None):
    """
    Scan OHLC series for dip-entry signals and apply target/stop/time-stop exits.

    Args:
        ticker:          ticker symbol (for output only)
        ohlc:            list of dicts with date/open/high/low/close, ascending
        filter_label:    "price_only" or "price+sentiment"
        sentiment_score: if not None, only enter when score > SENTIMENT_FLOOR
    """
    trades = []

    for i in range(1, len(ohlc)):
        prev_close      = ohlc[i - 1]["close"]
        curr_close      = ohlc[i]["close"]
        price_change_pct = (curr_close - prev_close) / prev_close * 100

        # Entry condition: price dropped enough
        if price_change_pct > DIP_THRESHOLD:
            continue

        # Phase B filter: skip if sentiment score not above floor
        if sentiment_score is not None and sentiment_score <= SENTIMENT_FLOOR:
            continue

        entry_date  = ohlc[i]["date"]
        entry_price = curr_close
        tp_price    = entry_price * (1 + TAKE_PROFIT_PCT / 100)
        sl_price    = entry_price * (1 - STOP_LOSS_PCT   / 100)

        exit_date  = None
        exit_price = None
        exit_type  = "open"

        for j in range(i + 1, len(ohlc)):
            day       = ohlc[j]
            stop_hit  = day["low"]  <= sl_price
            target_hit = day["high"] >= tp_price
            days_held  = (day["date"] - entry_date).days

            if stop_hit and target_hit:
                # Both on same day — conservatively assume stop filled first
                exit_date  = day["date"]
                exit_price = sl_price
                exit_type  = "stop"
                break
            elif stop_hit:
                exit_date  = day["date"]
                exit_price = sl_price
                exit_type  = "stop"
                break
            elif target_hit:
                exit_date  = day["date"]
                exit_price = tp_price
                exit_type  = "target"
                break
            elif days_held >= MAX_HOLD_DAYS:
                exit_date  = day["date"]
                exit_price = day["close"]
                exit_type  = "time_stop"
                break

        # Compute P&L
        if exit_type == "stop":
            pnl_pct = -STOP_LOSS_PCT
            outcome = "LOSS"
        elif exit_type == "target":
            pnl_pct = TAKE_PROFIT_PCT
            outcome = "WIN"
        elif exit_type == "time_stop":
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            outcome = "WIN" if pnl_pct > 0 else "LOSS"
        else:
            pnl_pct = None
            outcome = "OPEN"

        trades.append({
            "ticker":           ticker,
            "entry_date":       entry_date,
            "filter":           filter_label,
            "entry_price":      round(entry_price, 4),
            "price_change_pct": round(price_change_pct, 2),
            "sentiment_score":  sentiment_score if sentiment_score is not None else "",
            "exit_date":        exit_date,
            "exit_type":        exit_type,
            "exit_price":       round(exit_price, 4) if exit_price is not None else "",
            "pnl_pct":          round(pnl_pct, 2) if pnl_pct is not None else "",
            "outcome":          outcome,
        })

    return trades


def compute_stats(trades):
    closed = [t for t in trades if t["outcome"] in ("WIN", "LOSS")]
    if not closed:
        return None
    wins   = [t["pnl_pct"] for t in closed if t["outcome"] == "WIN"]
    losses = [t["pnl_pct"] for t in closed if t["outcome"] == "LOSS"]
    win_rate   = len(wins) / len(closed) * 100
    avg_win    = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss   = sum(losses) / len(losses) if losses else 0.0
    expectancy = (len(wins) / len(closed)) * avg_win + (len(losses) / len(closed)) * avg_loss
    return {
        "n":          len(closed),
        "open":       len(trades) - len(closed),
        "win_rate":   win_rate,
        "avg_win":    avg_win,
        "avg_loss":   avg_loss,
        "expectancy": expectancy,
    }


def print_stats(label, trades):
    stats = compute_stats(trades)
    if stats is None:
        print(f"[{label:<18}]  No closed trades.")
        return
    print(
        f"[{label:<18}]  {stats['n']} trades | "
        f"Win: {stats['win_rate']:.0f}% | "
        f"Avg win: {stats['avg_win']:+.1f}% | "
        f"Avg loss: {stats['avg_loss']:+.1f}% | "
        f"Expectancy: {stats['expectancy']:+.2f}%"
    )


def write_csv(all_trades):
    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / "backtest_results.csv"
    fieldnames = [
        "ticker", "entry_date", "filter", "entry_price", "price_change_pct",
        "sentiment_score", "exit_date", "exit_type", "exit_price", "pnl_pct", "outcome",
    ]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(all_trades)
    print(f"Results written -> {out}")


def main():
    parser = argparse.ArgumentParser(description="Price-divergence backtest")
    parser.add_argument("--days",   type=int, default=60, help="Calendar days to look back (default: 60)")
    parser.add_argument("--score",  action="store_true",  help="Enable Phase B: score tickers via Anthropic API (5 calls)")
    parser.add_argument("--scores", type=str, default=None,
                        help='Enable Phase B with pre-supplied scores JSON, e.g. \'{"AAPL":-0.05,"NVDA":0.65}\'')
    args = parser.parse_args()

    print(f"\n=== Backtest: {args.days} days | exit: +{TAKE_PROFIT_PCT}% target / -{STOP_LOSS_PCT}% stop / {MAX_HOLD_DAYS}d fallback ===")
    print("NOTE: Exit logic is an OHLC approximation for backtesting only.")
    print("      Production will use real-time exits and dynamic position sizing.\n")
    print("Limitations:")
    print("  - Only tests condition 3 (price divergence entry).")
    print("    Reversal/spike signals (1,2,4,5,6) need live accumulated sentiment history.")
    print("  - Sentiment scores are today's, applied retroactively - a rough directional proxy.")
    print("  - Same-day target+stop conflicts resolved conservatively (stop wins).")
    print(f"  - {args.days}-day window is directionally useful but not statistically significant at 95% CI.")
    print()

    phase_b = args.score or args.scores

    # Phase B: get sentiment scores
    sentiment_scores = {}
    if args.scores:
        # Pre-supplied scores (no API calls)
        sentiment_scores = json.loads(args.scores)
        print("[Phase B] Using pre-supplied sentiment scores:")
        for ticker in config.WATCHLIST:
            score = sentiment_scores.get(ticker)
            flag  = "PASS" if score is not None and score > SENTIMENT_FLOOR else "SKIP"
            print(f"  [{ticker}] Score: {score:+.2f}  [{flag} floor={SENTIMENT_FLOOR}]" if score is not None
                  else f"  [{ticker}] Score: n/a  [SKIP]")
        print()
    elif args.score:
        print("[Phase B] Fetching sentiment scores (5 API calls)...")
        run_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        for ticker in config.WATCHLIST:
            try:
                headlines = fetch_headlines(ticker)
                if not headlines:
                    print(f"  [{ticker}] No headlines - skipping.")
                    sentiment_scores[ticker] = None
                    continue
                score, summary = score_sentiment(ticker, headlines)
                store_score(run_ts, ticker, score, summary)
                sentiment_scores[ticker] = score
                print(f"  [{ticker}] Score: {score:+.2f} | {summary}")
            except Exception as e:
                print(f"  [{ticker}] ERROR: {e}")
                sentiment_scores[ticker] = None
        print()

    # Fetch OHLC and run backtest per ticker
    all_trades_a = []
    all_trades_b = []
    signal_dist  = {}

    for ticker in config.WATCHLIST:
        print(f"  [{ticker}] Fetching {args.days}d OHLC...")
        ohlc = fetch_ohlc(ticker, args.days)
        if len(ohlc) < 2:
            print(f"  [{ticker}] Insufficient data — skipping.")
            signal_dist[ticker] = 0
            continue

        trades_a = simulate_trades(ticker, ohlc, "price_only")
        all_trades_a.extend(trades_a)
        signal_dist[ticker] = len(trades_a)

        if phase_b:
            score    = sentiment_scores.get(ticker)
            trades_b = simulate_trades(ticker, ohlc, "price+sentiment", sentiment_score=score)
            all_trades_b.extend(trades_b)

    # Print summary
    print()
    print_stats("price_only", all_trades_a)
    if phase_b:
        print_stats("price+sentiment", all_trades_b)

    exit_counts = {k: sum(1 for t in all_trades_a if t["exit_type"] == k)
                   for k in ("target", "stop", "time_stop", "open")}
    print(
        f"\nExit breakdown (price_only):  "
        f"target: {exit_counts['target']} | "
        f"stop: {exit_counts['stop']} | "
        f"time_stop: {exit_counts['time_stop']} | "
        f"open: {exit_counts['open']}"
    )
    print("Signal distribution: " + "  ".join(f"{t}:{n}" for t, n in signal_dist.items()))

    all_trades = all_trades_a + (all_trades_b if phase_b else [])
    write_csv(all_trades)


if __name__ == "__main__":
    main()
