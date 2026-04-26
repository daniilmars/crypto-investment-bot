# Crypto + Stock Investment Bot

Automated multi-strategy trading bot that combines real-time news scraping, Gemini-powered catalyst analysis, and technical signals to generate trade decisions for cryptocurrencies and equities.

Currently runs in **paper trading** mode by default. Live trading wired for Binance (crypto); stock trading is signal-generation only — execution is a separate decision (see `memory/project_live_deployment_strategy.md`).

---

## What it does

1. **Scrapes news every cycle** from 113 RSS feeds + 6 web scrapers (CoinDesk, CoinTelegraph, Decrypt, CNBC, AP News, TechCrunch AI)
2. **Scores each article via Gemini** (`gemini-2.5-flash`) for trading impact, freshness, hype-vs-fundamental, catalyst type
3. **Per-symbol assessment via Gemini grounded search** (`gemini-2.5-flash` with Google Search tool, free tier) — produces direction, confidence, key_headline, impact_rank, risk_factors
4. **Sector-aware confidence ranking** caps secondary-beneficiary stocks when one news event hits multiple symbols (defends against e.g. Iran-news → 14 oil stocks all bullish at 0.7)
5. **Signal engine** combines Gemini sentiment with SMA, RSI, P/E, multi-timeframe trend alignment per symbol
6. **Per-strategy execution** — auto / conservative / longterm strategies share the news pipeline but apply their own weights, thresholds, and risk parameters
7. **Position management** — dynamic ATR-based SL/TP, trailing stops, time-stop for slow-drift positions, flash-analyst exits on adverse news
8. **Telegram + Mini App** — alerts on trade events, full rationale per position (entry catalyst, headline, sources, risks), live PnL dashboard

## Architecture overview

```
                 ┌──────────────────────────┐
                 │  RSS feeds + web scrapers│
                 │  (15-min cycle)          │
                 └────────┬─────────────────┘
                          │
                          ▼
         ┌────────────────────────────────────┐
         │ Per-article Gemini scoring         │
         │ (gemini-2.5-flash, 45-min cache)   │
         └────────┬───────────────────────────┘
                  │
                  ▼
   ┌─────────────────────────────────────────────────┐
   │ Per-symbol Gemini grounded search               │
   │  → direction, confidence, impact_rank, headline │
   │  → headline scrub (drop mismatched)             │
   │  → sector_ranking caps (shadow / live)          │
   │  → grounding URLs captured for attribution      │
   └────────┬────────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────┐
   │ Signal engine (sentiment + technicals)      │
   │  per strategy:                              │
   │    auto         (broad, Kelly-sized)        │
   │    conservative (high-conviction only)      │
   │    longterm     (thesis stocks only)        │
   └────────┬────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────┐
   │ Pre-trade gates                             │
   │  cooldown · max positions · sector limits   │
   │  event calendar · macro regime · CB         │
   └────────┬────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────┐
   │ Execution                                   │
   │  Binance (crypto, paper or live)            │
   │  Stocks: paper-only (signal generation)     │
   └────────┬────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────┐
   │ Position monitor                            │
   │  dynamic SL/TP · trailing · time-stop       │
   │  flash analyst exits · rotation             │
   └────────┬────────────────────────────────────┘
            │
            ▼
   ┌─────────────────────────────────────────────┐
   │ Telegram alerts + Mini App rationale        │
   └─────────────────────────────────────────────┘
```

## Strategies

Each strategy gets a **shared $10k capital pool** (configurable in `settings.strategies.<name>.paper_trading_initial_capital`). All strategies read the same Gemini pipeline but apply their own weights and gates.

| Strategy | Pool | Max positions | Min signal | Notes |
|---|---|---|---|---|
| `auto` | $10k | 20 | 0.65 | Kelly-sized, broad watchlist |
| `conservative` | $10k | 5 | 0.80 | High-conviction filter, fundamental weighting |
| `longterm` | $10k | 10 | 0.70 | Thesis-stocks-only, longer holds |
| `momentum` | — | — | — | Retired (kept in code for back-compat) |
| `manual` | $10k | — | — | Telegram-driven, exit-only for new entries |

## Repository layout

```
src/
├── analysis/                    # Signal engines, technical indicators, Gemini wrappers
│   ├── signal_engine.py         # Crypto signal generation
│   ├── stock_signal_engine.py   # Stock signal generation
│   ├── gemini_news_analyzer.py  # Per-article + per-symbol Gemini calls
│   ├── headline_validator.py    # Drops mismatched key_headlines
│   ├── sector_ranking.py        # Caps secondary-beneficiary confidence
│   ├── signal_attribution.py    # Trade → article linkage
│   ├── dynamic_risk.py          # ATR-based SL/TP
│   └── ...
├── orchestration/
│   ├── cycle_runner.py          # Main 15-min cycle
│   ├── trade_executor.py        # BUY/SELL paths, rotation
│   ├── position_monitor.py      # SL/TP/trailing/time-stop
│   ├── position_analyst.py      # Per-position Gemini analyst
│   └── pre_trade_gates.py       # Cooldown, sector, event-calendar gates
├── execution/
│   ├── binance_trader.py        # Paper + live order routing
│   ├── stock_trader.py          # Stock paper trading
│   └── circuit_breaker.py
├── collectors/
│   ├── web_news_scraper.py      # 6 scrapers
│   ├── news_data.py             # 113 RSS feeds
│   ├── binance_data.py          # Klines, prices
│   └── alpha_vantage_data.py    # Stock prices
├── notify/
│   ├── telegram_bot.py          # Commands + alerts
│   ├── telegram_chat.py         # AI chat (Gemini-driven)
│   └── telegram_live_dashboard.py
├── api/
│   └── miniapp_queries.py       # Mini App data layer
├── database.py                  # Dual SQLite/Postgres
└── config.py                    # YAML loader + business descriptions

config/
├── settings.yaml                # All tunables
├── watch_list.yaml              # 97 crypto + ~325 stocks
├── business_descriptions.yaml   # Per-ticker context for Gemini
└── sector_groups.yaml           # Sector-limit definitions

static/miniapp/                  # Telegram WebApp UI

tests/                           # 1,350 tests
.claude/skills/                  # Operational skills (check-bot, check-pnl, etc.)
```

## Setup

### Prerequisites
- Python 3.12 (in `.venv/`)
- Docker (for production deployment)
- API keys: Gemini (Vertex AI or consumer), Binance, Telegram, Alpha Vantage (optional, stocks)

### Local install
```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config/settings.yaml.example config/settings.yaml  # then edit
.venv/bin/python main.py
```

### Tests
```bash
.venv/bin/python -m pytest tests/ -q
```

## Deployment

Production runs on **GCE e2-micro in Frankfurt** (`europe-west3-a`), not Cloud Run. Why: avoiding Binance's HTTP 451 geo-block on US regions, plus persistent SQLite storage on the VM disk.

Deploy via push to `main`:
```bash
git push origin main   # GitHub Actions → tar+SCP → docker build on VM → docker run
```

Operational skills live in `.claude/skills/`:
- `/check-bot` — health + log dump
- `/check-pnl` — per-strategy realized + open positions
- `/check-news` — 9-layer news-pipeline diagnosis
- `/verify-changes` — post-PR validation
- `/assess-quality` — comprehensive bot quality scorecard
- `/check-costs` — GCP + Gemini spend
- `/backtest` — sync prod data, run sweeps

## Configuration highlights

Key settings under `config/settings.yaml`:

| Block | Purpose |
|---|---|
| `paper_trading: true` | Master switch — keep `true` until you're ready for live |
| `strategies.<name>.*` | Per-strategy capital, max positions, weights, risk params |
| `dynamic_risk.*` | ATR-based SL/TP (currently SL=7-15%, TP=10-50%) |
| `sector_ranking.enabled` | PR-C feature flag (default `false` — shadow mode) |
| `notifications.*` | Per-channel Telegram toggles (background msgs default off) |
| `event_calendar.*` | Block BUYs around earnings / FOMC / CPI |
| `sector_limits.enabled` | Per-sector position cap per strategy |

## What this bot is NOT

- **Not a trading-as-a-service product.** Single-user setup; multi-tenant would need significant work.
- **Not a HFT bot.** 15-minute cycle. Designed around news catalysts, not microstructure.
- **Not financial advice.** Paper-mode is the safe default. Live trading is opt-in via three independent config switches.

## Disclaimer

For educational and research purposes. Trading carries significant risk of loss. Past paper-trading results do not guarantee live performance. Test thoroughly before risking real capital. The author makes no warranty as to the accuracy or completeness of any signal generated by this bot.
