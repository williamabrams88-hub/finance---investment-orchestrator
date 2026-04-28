"""
Entry point for the stock sentiment pipeline.

Usage:
  python run.py --setup          Init DB (run once on first use)
  python run.py --fetch          Load 1yr price history + verify news access
  python run.py --score TICKER   Score a single ticker (e.g. --score AAPL)
  python run.py --once           Run the full pipeline once and exit
  python run.py                  Start the daily scheduler (7:00 AM per config)
"""

import argparse
import schedule
import time

import config
from pipeline import (
    init_db, fetch_prices, fetch_headlines,
    score_sentiment, store_score, run_pipeline, run_score_only
)
import datetime


def cmd_setup():
    init_db()


def cmd_fetch():
    print("Loading 1yr price history...")
    fetch_prices(period="1y")
    print("\nChecking news access...")
    for ticker in config.WATCHLIST:
        headlines = fetch_headlines(ticker)
        print(f"  [{ticker}] {len(headlines)} headlines available")
    print("\nFetch complete.")


def cmd_score(ticker):
    headlines = fetch_headlines(ticker)
    if not headlines:
        print(f"No headlines found for {ticker}.")
        return
    run_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    score, summary = score_sentiment(ticker, headlines)
    store_score(run_ts, ticker, score, summary)
    print(f"[{ticker}] Score: {score:+.2f}")
    print(f"Summary: {summary}")


def start_scheduler():
    print(f"Scheduler running. Pipeline fires daily at {config.SCHEDULE_TIME} (local time).")
    print("Press Ctrl+C to stop.\n")
    schedule.every().day.at(config.SCHEDULE_TIME).do(run_pipeline)
    for t in config.SCORE_ONLY_TIMES:
        schedule.every().day.at(t).do(run_score_only)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Sentiment Pipeline")
    parser.add_argument("--setup", action="store_true", help="Initialize the database")
    parser.add_argument("--fetch", action="store_true", help="Load 1yr prices + verify news")
    parser.add_argument("--score", metavar="TICKER", help="Score a single ticker")
    parser.add_argument("--once", action="store_true", help="Run the full pipeline once")
    parser.add_argument("--score-only", action="store_true",
                        help="Score all tickers (no signals/trades)")
    args = parser.parse_args()

    if args.setup:
        cmd_setup()
    elif args.fetch:
        cmd_fetch()
    elif args.score:
        cmd_score(args.score.upper())
    elif args.once:
        run_pipeline()
    elif args.score_only:
        run_score_only()
    else:
        start_scheduler()
