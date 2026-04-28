# Stock Sentiment MVP — Build Plan

## Overview

A system that scrapes financial news from free sources, scores sentiment for a watchlist of stocks using AI, stores everything in a local SQLite database on a recurring schedule, and over time builds a model that predicts stock price movements and suggests buy/sell opportunities.

---

## Goals

1. Collect news sentiment data for a stock watchlist at regular intervals (6h, 12h, or daily)
2. Store structured sentiment scores and summaries in a local SQLite database
3. Log actual stock prices alongside sentiment to create a ground truth dataset
4. After 7+ days of data, backtest whether high positive sentiment predicts price increases
5. Build a prediction model that outputs buy/sell signals based on sentiment trends

---

## Why SQLite (Not CSV or Google Sheets)

- **Zero setup** — SQLite is a single `.db` file, built into Python, no server or account needed
- **Queryable** — use SQL to filter, aggregate, and trend data across time intervals instantly
- **Scalable** — migrates cleanly to PostgreSQL when volume demands it, with minimal code changes
- **Reliable** — handles concurrent writes, no risk of corrupted rows like CSVs
- **Better for the model** — the prediction model can run SQL queries like `SELECT AVG(score) WHERE ticker = 'AAPL' AND timestamp > NOW() - 7 days` rather than parsing flat files

### Upgrade path
```
SQLite (MVP, local) → PostgreSQL (production, cloud) → add REST API layer if needed
```
The only code change when upgrading is the database connection string. All queries stay the same.

---

## Architecture Overview

```
[NewsAPI / RSS Feeds]       [Alpha Vantage]
        |                         |
        v                         v
 news_fetcher.py          price_fetcher.py
        |                         |
        v                         |
 sentiment_scorer.py              |
  (Claude API: -1 to +1)          |
        |_________________________|
                    |
                    v
            sentiment.db (SQLite)
          ┌──────────────────────────────┐
          │ table: sentiment_scores      │
          │ table: price_history         │
          │ table: signals               │
          └──────────────────────────────┘
                    |
                    v
         signal_generator.py
                    |
                    v
         reports/daily_digest.csv   ← human-readable export
         (optional, generated on demand)
```

---

## Database Schema

### Table: `sentiment_scores`
One row per stock per pipeline run.

```sql
CREATE TABLE sentiment_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,        -- ISO format: 2025-04-27T06:00:00
    ticker        TEXT NOT NULL,        -- e.g. 'AAPL'
    score         REAL NOT NULL,        -- -1.0 to +1.0
    summary       TEXT,                 -- one-sentence AI summary
    article_count INTEGER,              -- how many articles scored
    run_id        TEXT                  -- groups all tickers from same run
);
```

### Table: `price_history`
One row per stock per day.

```sql
CREATE TABLE price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT NOT NULL,        -- YYYY-MM-DD
    ticker      TEXT NOT NULL,
    close_price REAL NOT NULL,
    open_price  REAL,
    volume      INTEGER
);
```

### Table: `signals`
Output of the prediction model. One row per stock per run.

```sql
CREATE TABLE signals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    signal       TEXT NOT NULL,        -- 'BUY', 'SELL', or 'HOLD'
    confidence   TEXT NOT NULL,        -- 'High', 'Medium', 'Low'
    score_now    REAL,                 -- latest sentiment score
    score_avg_7d REAL,                 -- 7-day rolling average
    trend        TEXT                  -- 'Rising', 'Falling', 'Flat'
);
```

### Example query — 7-day sentiment trend for AAPL
```sql
SELECT timestamp, score
FROM sentiment_scores
WHERE ticker = 'AAPL'
ORDER BY timestamp DESC
LIMIT 14;
```

### Example query — backtest: did positive sentiment predict price gain?
```sql
SELECT
    s.ticker,
    s.score AS sentiment_day1,
    p1.close_price AS price_day1,
    p7.close_price AS price_day7,
    ROUND((p7.close_price - p1.close_price) / p1.close_price * 100, 2) AS pct_change
FROM sentiment_scores s
JOIN price_history p1 ON s.ticker = p1.ticker AND DATE(s.timestamp) = p1.date
JOIN price_history p7 ON s.ticker = p7.ticker
WHERE p7.date = DATE(p1.date, '+7 days')
ORDER BY s.timestamp DESC;
```

---

## Stock Watchlist (Starting Point)

Begin with 10–15 high-coverage US stocks. High news volume = more reliable sentiment signals.

Suggested starting watchlist:
- `AAPL` — Apple
- `MSFT` — Microsoft
- `NVDA` — NVIDIA
- `TSLA` — Tesla
- `AMZN` — Amazon
- `GOOGL` — Alphabet
- `META` — Meta
- `JPM` — JPMorgan Chase
- `NFLX` — Netflix
- `AMD` — Advanced Micro Devices

Expand to ASX stocks once system is validated.

---

## APIs & Tools Required

| Tool | Purpose | Cost | Sign Up |
|------|---------|------|---------|
| NewsAPI | Fetch news articles by ticker | Free (100 req/day) | newsapi.org |
| Alpha Vantage | Daily stock closing prices | Free (25 req/day) | alphavantage.co |
| Claude API | Sentiment scoring (-1 to +1) | Pay-per-use (~$0.01–0.05/run) | console.anthropic.com |
| SQLite | Local database storage | Free, built into Python | — |
| Python `schedule` | Run scripts on a timed cadence | Free (pip install) | — |

---

## Python Scripts

### `db_setup.py`
Run once at the start to create `sentiment.db` and all three tables. Safe to re-run — uses `CREATE TABLE IF NOT EXISTS`.

### `news_fetcher.py`
- Queries NewsAPI for each ticker in the watchlist
- Falls back to RSS feeds (Reuters, Yahoo Finance, BBC Business) if API limit is reached
- Returns top 5 articles per stock per run (headline + body snippet)

```python
# Key libraries: requests, feedparser
# NewsAPI endpoint: https://newsapi.org/v2/everything?q={ticker}&sortBy=publishedAt
```

### `sentiment_scorer.py`
- Receives article list from news_fetcher
- Sends each batch to Claude API with a structured prompt
- Prompt template:
  ```
  You are a financial analyst. Given the following news articles about {TICKER},
  return a JSON object with:
  - "score": a sentiment score from -1.0 (very negative) to +1.0 (very positive)
  - "summary": one sentence explaining the key sentiment driver for the stock price

  Focus on what this news means for the stock price specifically, not the company generally.

  Articles: {articles}
  ```
- Returns score + summary per ticker

```python
# Key libraries: anthropic
# Model: claude-sonnet-4-20250514
```

### `price_fetcher.py`
- Queries Alpha Vantage for the latest closing price per ticker
- Inserts one row per ticker into `price_history`
- Runs once daily (price data does not need 6h cadence)

```python
# Key libraries: requests
# Endpoint: https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={ticker}
```

### `db_writer.py`
- Handles all database writes
- Functions: `write_sentiment_row()`, `write_price_row()`, `write_signal_row()`
- Uses Python's built-in `sqlite3` library — no extra install needed

```python
import sqlite3

def write_sentiment_row(ticker, score, summary, article_count, run_id):
    conn = sqlite3.connect('sentiment.db')
    conn.execute("""
        INSERT INTO sentiment_scores (timestamp, ticker, score, summary, article_count, run_id)
        VALUES (datetime('now'), ?, ?, ?, ?, ?)
    """, (ticker, score, summary, article_count, run_id))
    conn.commit()
    conn.close()
```

### `signal_generator.py`
- Reads from `sentiment_scores` table
- Calculates rolling 7-day average and trend direction per stock
- Applies rule-based signal logic (see Prediction Model section below)
- Writes results to `signals` table
- Optionally exports a human-readable `reports/daily_digest.csv`

### `scheduler.py`
- Entry point — runs all scripts in sequence on a schedule
- Recommended: daily at market open (9:30 AM ET), optionally again at close (4:00 PM ET)

```python
import schedule, time

def run_pipeline():
    news = fetch_news(WATCHLIST)
    scores = score_sentiment(news)
    write_sentiment(scores)
    fetch_and_write_prices(WATCHLIST)
    generate_signals()

schedule.every().day.at("09:30").do(run_pipeline)

while True:
    schedule.run_pending()
    time.sleep(60)
```

---

## Prediction Model (Phase 2)

Once 7+ days of data are collected, build a simple model to generate buy/sell signals.

### Approach — Rule-Based First (No ML Required)
Start with clear thresholds before adding complexity:

| Condition | Signal | Confidence |
|-----------|--------|-----------|
| Score > 0.6 AND 7d avg rising | BUY | High |
| Score > 0.4 AND 7d avg rising | BUY | Medium |
| Score < -0.6 AND 7d avg falling | SELL | High |
| Score < -0.4 AND 7d avg falling | SELL | Medium |
| Everything else | HOLD | Low |

### Scoring inputs for the model
- Latest sentiment score
- 7-day rolling average (smooths out noise from single articles)
- Trend direction: slope of the last 3 scores (rising, flat, or falling)
- Score velocity: how fast the score is changing run-to-run

### Upgrade path (optional — post MVP)
Once rule-based signals are validated against backtest results:
- **Logistic regression** (scikit-learn) — predicts probability of price increase
- Features: sentiment score, trend slope, news article volume, price momentum
- Training data: `sentiment_scores` + `price_history` joined after 30+ days of collection

---

## Backtesting Logic (7-day)

**Question:** Did stocks with sentiment score > 0.5 on Day 1 actually go up by Day 7?

**Method:** SQL query across `sentiment_scores` and `price_history` tables (see example query in Database Schema section above).

**Accuracy threshold:** > 55% is meaningful (random chance baseline = 50%)

**Output:** `reports/backtest_results.csv` with columns:
`ticker | sentiment_day1 | signal | price_day1 | price_day7 | pct_change | correct`

---

## Build Order (Recommended Sequence for Claude Code)

1. `db_setup.py` — create `sentiment.db` with all three tables
2. `db_writer.py` — build all write functions and test with dummy data
3. `news_fetcher.py` — test with 3 tickers, confirm articles are returned
4. `sentiment_scorer.py` — test Claude API prompt, confirm JSON output with score + summary
5. `price_fetcher.py` — test Alpha Vantage connection, confirm price returned
6. `scheduler.py` — wire all scripts together, run one full pipeline end-to-end manually
7. Run daily for 7 days, confirm data is accumulating cleanly in the database
8. `signal_generator.py` — build rule-based signals, write to `signals` table
9. Add backtest query and export to `reports/backtest_results.csv`
10. Validate accuracy. If > 55%, begin upgrading signal logic or adding ML layer

---

## Project File Structure

```
stock-sentiment/
├── sentiment.db              # SQLite database (auto-created, do not commit)
├── .env                      # API keys (never commit to git)
├── .gitignore                # include: .env, sentiment.db, reports/
├── requirements.txt
├── db_setup.py               # run once to initialise database
├── db_writer.py              # all database write functions
├── news_fetcher.py
├── sentiment_scorer.py
├── price_fetcher.py
├── signal_generator.py
├── scheduler.py              # main entry point
├── config.py                 # watchlist, thresholds, cadence settings
└── reports/                  # auto-generated CSV exports
    ├── daily_digest.csv
    └── backtest_results.csv
```

---

## Environment Setup

```bash
pip install anthropic requests feedparser schedule python-dotenv
```

SQLite and `sqlite3` are built into Python — no extra install needed.

Required environment variables (store in `.env`):
```
NEWSAPI_KEY=your_key_here
ALPHAVANTAGE_KEY=your_key_here
ANTHROPIC_API_KEY=your_key_here
```

---

## Key Constraints & Notes

- **NewsAPI free tier:** 100 requests/day. At 10 stocks = 10 requests per run, allows up to 10 runs/day.
- **Alpha Vantage free tier:** 25 requests/day. Fetch prices once daily only.
- **Claude API cost:** ~$0.01–0.05 per full pipeline run depending on article volume.
- **SQLite concurrency:** Safe for a single automated pipeline. If multiple processes write simultaneously in future, enable WAL mode: `conn.execute("PRAGMA journal_mode=WAL")`.
- **The prompt quality matters most:** Iterate on the sentiment prompt before iterating on the model. A sharper prompt = sharper signals.
- **No ML needed for MVP:** Rule-based signals are enough to validate the concept before adding complexity.

---

## Success Criteria for MVP

- [ ] Pipeline runs automatically without manual intervention
- [ ] 7 days of sentiment + price data accumulated cleanly in `sentiment.db`
- [ ] Backtest query returns results with > 55% directional accuracy
- [ ] At least one clear BUY and one clear SELL signal generated in the `signals` table
- [ ] `daily_digest.csv` exports correctly and is human-readable
