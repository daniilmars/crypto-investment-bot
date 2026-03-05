# Investment Bot — Complete Process Flow

## Startup

```
Application Start
├─ Initialize database (PostgreSQL or SQLite)
├─ Restore state from DB (trailing stop peaks, cooldowns)
├─ Start Telegram bot (polling mode)
│   └─ Register signal confirmation callback
└─ Launch background tasks
    ├─ bot_loop()              — main cycle, every 15 min
    ├─ status_update_loop()    — performance report, every 1h
    ├─ signal_cleanup_loop()   — expire pending confirmations, every 60s
    ├─ auto_bot_summary_loop() — auto-trading digest, every 1h
    └─ daily_digest_loop()     — macro event alerts, daily @ 8 UTC
```

---

## Main Cycle (every 15 minutes)

### Phase 1: Environment Setup

```
Load config (watch lists, thresholds, trading mode)
    │
    ▼
Dynamic Position Sizing (Kelly Criterion)
    │  if ≥10 past trades → kelly_fraction from win rate
    │  else → fixed trade_risk_percentage (default 3%)
    ▼
Macro Regime Detection (cached 14 min)
    │  Inputs: VIX, S&P 500 vs SMA200, 10Y yield trend, BTC vs SMA50
    │  Score = sum of signals (range -4 to +4)
    │
    ├─ score ≥ 2  → RISK_ON   (1.0x multiplier)
    ├─ score ≤ -2 → RISK_OFF  (0.3x multiplier, suppress BUYs)
    └─ else       → CAUTION   (0.6x multiplier)
    │
    ▼
Circuit Breaker (live trading only)
    │  5 checks: balance floor, daily loss, drawdown, consecutive losses, cooldown
    │
    ├─ TRIPPED → skip all crypto trading this cycle
    └─ OK → continue
```

### Phase 2: News & Sentiment Analysis

```
collect_and_analyze_news()
    │
    ├─ RSS Feed Collection (~80 feeds from source registry)
    │   ├─ Tier 1 (Premium): regulatory (Fed, SEC), crypto (CoinDesk)
    │   ├─ Tier 2 (Standard): financial (Reuters, Bloomberg)
    │   └─ Tier 3 (Trial): sector, KOL, Asia-Pacific
    │
    ├─ Web Scraping (12 scrapers: CoinDesk, Reuters, CNBC, etc.)
    │
    ├─ Deduplication (SHA-256 title hash)
    │
    ├─ Symbol Matching (word-boundary regex per ticker)
    │   └─ e.g., "Solana" matches SOL, but "solution" does not
    │
    ├─ Sentiment Scoring (per article)
    │   ├─ VADER (free, instant) → score -1.0 to +1.0
    │   └─ Gemini 2.0 Flash (batches of 50) → score -1.0 to +1.0
    │
    ├─ Per-Symbol Aggregation
    │   └─ avg_sentiment, volatility, positive/negative ratios
    │
    └─ Gemini Investment Assessment (per symbol with news)
        └─ Output: direction (bullish/bearish/neutral), confidence (0-1)
```

### Phase 3: Per-Symbol Trading Loop

```
FOR EACH symbol in watch_list:
    │
    ▼
┌─ Fetch Current Price (Binance batch API)
│
├─ Position Open? ──YES──► Position Monitoring
│                           │
│                           ├─ Trailing Stop: profit ≥ 2% activation
│                           │   AND drawdown from peak ≥ 1.5%
│                           │   → SELL, resolve attribution, cleanup
│                           │
│                           ├─ Stop-Loss: loss ≥ 3.5%
│                           │   → SELL, set 6h cooldown, resolve attribution
│                           │
│                           ├─ Take-Profit: gain ≥ 8%
│                           │   → SELL, resolve attribution
│                           │
│                           └─ None triggered → continue
│                           │
│                           ▼
│                       Position Analyst (every 30 min, if enabled)
│                           │  Gemini analyzes: should we hold, add, or exit?
│                           │
│                           ├─ INCREASE (confidence ≥ 75%)
│                           │   → calculate addition size, enforce 3x cap
│                           ├─ SELL (confidence ≥ 80%)
│                           │   → send for confirmation or auto-execute
│                           └─ HOLD → no action
│                           │
│                           ▼
│                       SKIP signal generation (position managed)
│
├─ Position Open? ──NO──► Signal Generation
│                           │
│                           ▼
│                       Calculate Technicals
│                           │  SMA (20-period), RSI (14-period)
│                           │
│                           ▼
│                       ┌─ signal_mode = "sentiment" (primary)
│                       │   │
│                       │   ├─ Step 1: Sentiment Trigger
│                       │   │   ├─ Gemini confidence ≥ 0.5 → bullish/bearish
│                       │   │   └─ Fallback: VADER ≥ 0.3 → bullish
│                       │   │   └─ No trigger → HOLD
│                       │   │
│                       │   ├─ Step 2: SMA Trend Filter
│                       │   │   └─ Bullish but price < SMA → HOLD
│                       │   │
│                       │   ├─ Step 3: RSI Veto
│                       │   │   └─ Bullish but RSI > 75 → HOLD
│                       │   │
│                       │   └─ All gates passed → BUY or SELL
│                       │
│                       └─ signal_mode = "scoring" (legacy)
│                           │  7 indicators vote: SMA, RSI, News,
│                           │  MACD, Bollinger, Volume, OrderBook
│                           └─ score ≥ 3 → BUY or SELL
│
│                       Record Signal Attribution
│                           └─ link signal → articles → sources → Gemini data
│
├─ Signal = HOLD → no action
│
├─ Signal = BUY ──► Pre-Trade Gates
│                     │
│                     ├─ Macro regime: RISK_OFF & suppress → BLOCKED
│                     ├─ Stoploss cooldown active? → BLOCKED
│                     ├─ Signal cooldown active? → BLOCKED
│                     ├─ Max positions reached? → BLOCKED
│                     ├─ Sector limit breached? → BLOCKED
│                     ├─ Event calendar block? → BLOCKED or reduce size
│                     └─ ALL CLEAR
│                         │
│                         ▼
│                     Calculate Quantity
│                         │  qty = (balance × risk% × macro_mult × kelly) / price
│                         │
│                         ▼
│                     ┌─ Confirmation Required?
│                     │   │
│                     │   ├─ YES → Send Telegram inline buttons
│                     │   │         [✅ Execute]  [❌ Skip]
│                     │   │         (timeout: 30 min)
│                     │   │
│                     │   └─ NO → Immediate Execution
│                     │
│                     └─ place_order(symbol, "BUY", qty, price)
│                         ├─ Paper: simulate in DB
│                         └─ Live: Binance API + OCO brackets for SL/TP
│
└─ Signal = SELL ──► Find open position → execute_sell (same confirmation flow)
```

### Phase 3b: Auto-Trading Shadow Bot (runs in parallel)

```
Same signal, same cycle — but:
  ├─ Separate position tracking (trading_strategy='auto')
  ├─ Separate cooldowns
  ├─ NO confirmation required (auto-executes)
  └─ Independent capital pool ($10k paper default)
```

### Phase 4: Stock Trading Cycle

```
Same structure as crypto, but:
  ├─ Different broker: paper_only or Alpaca
  ├─ Market hours check (skip if market closed)
  ├─ PDT rule check ($25k minimum for day trading)
  ├─ Fundamental gates: P/E ratio, earnings growth
  ├─ IPO tracking: auto-promote new tickers to watchlist
  └─ Separate capital pool
```

---

## Trade Close → Learning Feedback Loop

```
Position Closes (SL/TP/trailing/signal)
    │
    ▼
process_closed_trade(order_id, pnl, exit_reason)
    │
    ├─ Resolve signal attribution
    │   └─ Fill: PnL, PnL%, duration, exit_reason
    │
    ├─ For each source that contributed articles:
    │   ├─ Update articles_with_signals count
    │   ├─ Update profitable_signal_ratio (running avg)
    │   └─ Update avg_signal_pnl (running avg)
    │
    └─ Daily Source Review (scheduled)
        ├─ Recalculate reliability_score per source
        │   = 0.3×availability + 0.2×relevance + 0.2×uniqueness + 0.3×signal_contribution
        │
        ├─ Deactivate: consecutive_errors > 50 or score < 0.2
        ├─ Promote: trial→standard if score > 0.6, 10+ articles
        ├─ Promote: standard→premium if profitable_ratio > 55%
        └─ Log all changes to experiment_log
```

---

## Auto-Tuner (weekly)

```
run_auto_tune()
    │
    ├─ Collect last 30 days of closed trades
    │   └─ Need minimum 20 trades
    │
    ├─ Parameter sweep:
    │   ├─ stop_loss: [2%, 2.5%, 3%, 3.5%, 4%, 4.5%, 5%]
    │   ├─ take_profit: [4%, 6%, 8%, 10%, 12%]
    │   └─ confidence: [0.4, 0.45, 0.5, 0.55, 0.6]
    │
    ├─ Evaluate each: Sharpe ratio, win rate
    │
    ├─ Safety bounds: SL 1.5-6%, TP 3-15%, confidence 0.35-0.75
    │
    ├─ Require Sharpe improvement ≥ 0.1 to change
    │
    ├─ Max 2 changes per cycle
    │
    └─ Auto-revert if performance degrades >10% within 7 days
```

---

## Key Data Flow Summary

```
Market Data (Binance, Alpha Vantage, yfinance)
         +
News (80 RSS feeds + 12 web scrapers)
         │
         ▼
    Gemini Analysis → direction + confidence
         │
         ▼
    Signal Engine → BUY / SELL / HOLD
         │
         ▼
    Pre-Trade Gates → allowed / blocked
         │
         ▼
    Execution (paper or live) → position in DB
         │
         ▼
    Position Monitor (every 15 min) → SL / TP / trailing exit
         │
         ▼
    Feedback Loop → update source scores → improve next cycle
```
