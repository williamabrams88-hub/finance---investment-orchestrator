import os
import json
import sqlite3
import datetime
import csv
from pathlib import Path

import yfinance as yf
import anthropic
from dotenv import load_dotenv

import config

load_dotenv()

DB_PATH = "sentiment.db"
REPORTS_DIR = Path("reports")


# ── DB ────────────────────────────────────────────────────────────────────────

def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sentiment_scores (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp   TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                score           REAL NOT NULL,
                summary         TEXT
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                open            REAL,
                high            REAL,
                low             REAL,
                close           REAL,
                volume          INTEGER,
                UNIQUE(date, ticker)
            );

            CREATE TABLE IF NOT EXISTS signals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_timestamp TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                signal          TEXT NOT NULL,
                confidence      TEXT NOT NULL,
                reason          TEXT
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_timestamp TEXT NOT NULL,
                ticker          TEXT NOT NULL,
                signal          TEXT NOT NULL,
                entry_price     REAL NOT NULL,
                exit_price      REAL,
                pnl_pct         REAL,
                outcome         TEXT DEFAULT 'OPEN'
            );
        """)
    print("DB initialized with 4 tables.")


# ── PRICE FETCH ───────────────────────────────────────────────────────────────

def fetch_prices(period="5d"):
    """Load OHLCV history for all watchlist tickers into price_history."""
    with get_conn() as conn:
        for ticker in config.WATCHLIST:
            df = yf.Ticker(ticker).history(period=period)
            if df.empty:
                print(f"  [{ticker}] No price data returned.")
                continue
            rows = []
            for date, row in df.iterrows():
                rows.append((
                    date.strftime("%Y-%m-%d"), ticker,
                    row.get("Open"), row.get("High"), row.get("Low"),
                    row.get("Close"), int(row.get("Volume", 0))
                ))
            conn.executemany(
                """INSERT OR IGNORE INTO price_history
                   (date, ticker, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows
            )
            print(f"  [{ticker}] {len(rows)} price rows loaded.")


def get_latest_close(ticker):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT close FROM price_history WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (ticker,)
        ).fetchone()
    return row[0] if row else None


def get_close_on_or_after(ticker, date_str):
    """Return the first available close price on or after a given date."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT close FROM price_history WHERE ticker=? AND date>=? ORDER BY date ASC LIMIT 1",
            (ticker, date_str)
        ).fetchone()
    return row[0] if row else None


def get_recent_closes(ticker, n=2):
    """Return last n closing prices, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT close FROM price_history WHERE ticker=? ORDER BY date DESC LIMIT ?",
            (ticker, n)
        ).fetchall()
    return [r[0] for r in rows]


# ── NEWS FETCH ────────────────────────────────────────────────────────────────

def fetch_headlines(ticker):
    """Return up to 10 recent news items (title + description + summary) for a ticker via yfinance."""
    articles = yf.Ticker(ticker).news or []
    items = []
    for a in articles[:10]:
        content     = a.get("content", {})
        title       = content.get("title") or a.get("title", "")
        description = content.get("description", "").strip()
        summary     = content.get("summary", "").strip()
        if not title:
            continue
        parts = [title]
        if description:
            parts.append(description)
        if summary and summary != description:
            parts.append(summary)
        items.append(" — ".join(parts))
    return items


# ── SENTIMENT SCORING ─────────────────────────────────────────────────────────

def score_sentiment(ticker, headlines):
    """Call Claude API with headlines; return (score: float, summary: str)."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    headlines_text = "\n".join(f"- {h}" for h in headlines)
    prompt = (
        f"Score the market sentiment for {ticker} based on these news items.\n\n"
        f"Return a JSON object with exactly two fields:\n"
        f'- "score": a float on this fixed scale:\n'
        f'    +1.0  strong positive catalyst (earnings beat, major contract, FDA approval, buyout)\n'
        f'    +0.5  moderate positive (analyst upgrade, guidance raise, new product launch)\n'
        f'     0.0  neutral or mixed — no clear directional signal\n'
        f'    -0.5  moderate negative (guidance cut, analyst downgrade, exec departure, lawsuit)\n'
        f'    -1.0  severe negative (fraud, major recall, bankruptcy risk, regulatory block)\n'
        f'    Use intermediate values (e.g. +0.3, -0.7). Weight breaking news more than older items.\n'
        f'- "summary": the single dominant catalyst in 12 words or fewer\n\n'
        f"News items:\n{headlines_text}\n\n"
        f'Respond with only valid JSON. Example: {{"score": -0.4, "summary": "CEO departure amid slowing cloud revenue growth."}}'
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )

    result = json.loads(message.content[0].text.strip())
    return float(result["score"]), result["summary"]


def store_score(run_ts, ticker, score, summary):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sentiment_scores (run_timestamp, ticker, score, summary) VALUES (?, ?, ?, ?)",
            (run_ts, ticker, score, summary)
        )


def get_previous_score(ticker, before_ts):
    """Most recent score for a ticker before a given run timestamp."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT score FROM sentiment_scores WHERE ticker=? AND run_timestamp<? ORDER BY run_timestamp DESC LIMIT 1",
            (ticker, before_ts)
        ).fetchone()
    return row[0] if row else None


def get_recent_scores(ticker, n=3):
    """Return last n scores for a ticker, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT score FROM sentiment_scores WHERE ticker=? ORDER BY run_timestamp DESC LIMIT ?",
            (ticker, n)
        ).fetchall()
    return [r[0] for r in rows]


# ── SIGNAL LOGIC ──────────────────────────────────────────────────────────────

def generate_signal(ticker, current_score, run_ts):
    """
    Reversal + divergence signals. Returns (signal, confidence, reason) or (None, None, None) for HOLD.
    Checked in order — first match wins.
    """
    prev_score = get_previous_score(ticker, run_ts)

    closes = get_recent_closes(ticker, n=2)
    price_change_pct = (
        (closes[0] - closes[1]) / closes[1] * 100 if len(closes) == 2 else 0.0
    )

    # 1. Sentiment reversal: negative → positive
    if prev_score is not None and prev_score < 0 and current_score > config.REVERSAL_CROSS_POS:
        return (
            "BUY", "High",
            f"Sentiment reversal neg→pos (prev={prev_score:.2f}, now={current_score:.2f})"
        )

    # 2. Score spikes up AND price is flat or down (news catching up to price)
    if prev_score is not None and (current_score - prev_score) >= config.SPIKE_DELTA and price_change_pct <= 0:
        return (
            "BUY", "High",
            f"Score spike +{current_score - prev_score:.2f} with price {price_change_pct:+.1f}%"
        )

    # 3. Price dropped >2% but sentiment is positive (market overreaction)
    if price_change_pct <= config.DIVERGENCE_PRICE_DROP_PCT and current_score > config.DIVERGENCE_MIN_SENTIMENT:
        return (
            "BUY", "Medium",
            f"Price dropped {price_change_pct:.1f}% but sentiment={current_score:.2f} (overreaction)"
        )

    # 4. Sentiment reversal: positive → negative
    if prev_score is not None and prev_score > 0 and current_score < config.REVERSAL_CROSS_NEG:
        return (
            "SELL", "High",
            f"Sentiment reversal pos→neg (prev={prev_score:.2f}, now={current_score:.2f})"
        )

    # 5. Score drops sharply AND price is flat or up (bad news not yet priced)
    if prev_score is not None and (prev_score - current_score) >= config.SPIKE_DELTA and price_change_pct >= 0:
        return (
            "SELL", "High",
            f"Score drop -{prev_score - current_score:.2f} with price {price_change_pct:+.1f}%"
        )

    # 6. Sustained negative: last N scores all below threshold
    recent = get_recent_scores(ticker, config.SUSTAINED_NEG_RUNS)
    if len(recent) == config.SUSTAINED_NEG_RUNS and all(s < config.SUSTAINED_NEG_THRESHOLD for s in recent):
        return (
            "SELL", "Medium",
            f"Sustained negative for {config.SUSTAINED_NEG_RUNS} runs (scores: {[f'{s:.2f}' for s in recent]})"
        )

    return None, None, None


def store_signal(signal_ts, ticker, signal, confidence, reason):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO signals (signal_timestamp, ticker, signal, confidence, reason) VALUES (?, ?, ?, ?, ?)",
            (signal_ts, ticker, signal, confidence, reason)
        )


# ── PAPER TRADES ──────────────────────────────────────────────────────────────

def open_paper_trade(entry_ts, ticker, signal, entry_price):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO paper_trades (entry_timestamp, ticker, signal, entry_price, outcome) VALUES (?, ?, ?, ?, 'OPEN')",
            (entry_ts, ticker, signal, entry_price)
        )


def close_expired_paper_trades():
    """Close paper trades whose hold period has elapsed and price data is available."""
    cutoff_ts = (
        datetime.datetime.utcnow() - datetime.timedelta(days=config.PAPER_TRADE_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%S")

    with get_conn() as conn:
        open_trades = conn.execute(
            """SELECT id, entry_timestamp, ticker, signal, entry_price
               FROM paper_trades WHERE outcome='OPEN' AND entry_timestamp<=?""",
            (cutoff_ts,)
        ).fetchall()

    for trade_id, entry_ts, ticker, signal, entry_price in open_trades:
        entry_date = entry_ts[:10]
        exit_date = (
            datetime.datetime.strptime(entry_date, "%Y-%m-%d")
            + datetime.timedelta(days=config.PAPER_TRADE_DAYS)
        ).strftime("%Y-%m-%d")

        exit_price = get_close_on_or_after(ticker, exit_date)
        if exit_price is None:
            continue  # price data not yet available; try again next run

        if signal == "BUY":
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:  # SELL (short simulation)
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        outcome = "WIN" if pnl_pct > 0 else "LOSS"
        with get_conn() as conn:
            conn.execute(
                "UPDATE paper_trades SET exit_price=?, pnl_pct=?, outcome=? WHERE id=?",
                (exit_price, pnl_pct, outcome, trade_id)
            )
        print(f"  Closed [{ticker}] {signal} — {outcome} ({pnl_pct:+.1f}%)")


# ── REPORTS ───────────────────────────────────────────────────────────────────

def export_reports():
    REPORTS_DIR.mkdir(exist_ok=True)

    with get_conn() as conn:
        signals = conn.execute(
            "SELECT signal_timestamp, ticker, signal, confidence, reason FROM signals ORDER BY signal_timestamp DESC LIMIT 50"
        ).fetchall()
        trades = conn.execute(
            "SELECT entry_timestamp, ticker, signal, entry_price, exit_price, pnl_pct, outcome FROM paper_trades ORDER BY entry_timestamp DESC"
        ).fetchall()
        scores = conn.execute(
            "SELECT run_timestamp, ticker, score, summary FROM sentiment_scores ORDER BY run_timestamp DESC LIMIT 100"
        ).fetchall()

    with open(REPORTS_DIR / "daily_digest.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "ticker", "signal", "confidence", "reason"])
        w.writerows(signals)

    with open(REPORTS_DIR / "paper_pnl.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["entry_timestamp", "ticker", "signal", "entry_price", "exit_price", "pnl_pct", "outcome"])
        w.writerows(trades)

    with open(REPORTS_DIR / "scores.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["run_timestamp", "ticker", "score", "summary"])
        w.writerows(scores)

    # Print expectancy summary
    closed = [(t[5], t[6]) for t in trades if t[6] != "OPEN"]
    if closed:
        wins  = [p for p, o in closed if o == "WIN"]
        losses = [p for p, o in closed if o == "LOSS"]
        win_rate  = len(wins) / len(closed) * 100
        avg_win   = sum(wins)   / len(wins)   if wins   else 0.0
        avg_loss  = sum(losses) / len(losses) if losses else 0.0
        expectancy = (len(wins) / len(closed)) * avg_win - (len(losses) / len(closed)) * abs(avg_loss)
        print(
            f"  Paper P&L ({len(closed)} closed) | "
            f"Win rate: {win_rate:.0f}% | "
            f"Avg win: {avg_win:+.1f}% | "
            f"Avg loss: {avg_loss:+.1f}% | "
            f"Expectancy: {expectancy:+.2f}%"
        )
    else:
        print("  No closed paper trades yet.")

    print(f"  Reports written → {REPORTS_DIR}/")


# ── MAIN PIPELINE ─────────────────────────────────────────────────────────────

def run_pipeline():
    run_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"\n=== Pipeline run: {run_ts} UTC ===")

    print("\n[1] Fetching recent prices (5d)...")
    fetch_prices(period="5d")

    print("\n[2] Closing expired paper trades...")
    close_expired_paper_trades()

    print("\n[3] Scoring sentiment...")
    for ticker in config.WATCHLIST:
        try:
            headlines = fetch_headlines(ticker)
            if not headlines:
                print(f"  [{ticker}] No headlines — skipping.")
                continue

            print(f"  [{ticker}] {len(headlines)} headlines → scoring...")
            score, summary = score_sentiment(ticker, headlines)
            store_score(run_ts, ticker, score, summary)
            print(f"  [{ticker}] Score: {score:+.2f} | {summary}")

            signal, confidence, reason = generate_signal(ticker, score, run_ts)
            if signal:
                store_signal(run_ts, ticker, signal, confidence, reason)
                entry_price = get_latest_close(ticker)
                if entry_price:
                    open_paper_trade(run_ts, ticker, signal, entry_price)
                print(f"  [{ticker}] *** {signal} ({confidence}) — {reason}")
            else:
                print(f"  [{ticker}] HOLD")

        except Exception as e:
            print(f"  [{ticker}] ERROR: {e}")

    print("\n[4] Exporting reports...")
    export_reports()

    print(f"\n=== Done ===\n")


def run_score_only():
    run_ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    print(f"\n=== Score-only run: {run_ts} UTC ===")
    fetch_prices(period="1d")
    for ticker in config.WATCHLIST:
        try:
            headlines = fetch_headlines(ticker)
            if not headlines:
                print(f"  [{ticker}] No headlines — skipping.")
                continue
            score, summary = score_sentiment(ticker, headlines)
            store_score(run_ts, ticker, score, summary)
            print(f"  [{ticker}] Score: {score:+.2f} | {summary}")
        except Exception as e:
            print(f"  [{ticker}] ERROR: {e}")
    export_reports()
    print("=== Done ===\n")
