# Watchlist — 5 high-volume, liquid US tickers
WATCHLIST = ["AAPL", "NVDA", "TSLA", "MSFT", "AMZN"]

# Signal thresholds
REVERSAL_CROSS_POS = 0.2        # score must exceed this to confirm pos reversal
REVERSAL_CROSS_NEG = -0.2       # score must fall below this to confirm neg reversal
SPIKE_DELTA = 0.3               # minimum score change vs prev run to trigger spike signal
DIVERGENCE_PRICE_DROP_PCT = -2.0  # price must drop at least this % for divergence BUY
DIVERGENCE_MIN_SENTIMENT = 0.3  # sentiment must be above this for divergence BUY
SUSTAINED_NEG_THRESHOLD = -0.4  # score threshold for sustained sell signal
SUSTAINED_NEG_RUNS = 3          # number of consecutive runs below threshold for SELL

# Paper trade settings
PAPER_TRADE_DAYS = 7            # calendar days before closing a paper trade

# Scheduler — NOTE: schedule library uses local machine time
# If your machine is not in ET, adjust this to match 7:00 AM ET in your local timezone
SCHEDULE_TIME = "07:00"
SCORE_ONLY_TIMES = ["11:00", "15:00"]   # score-only runs (no signals, no trades)
