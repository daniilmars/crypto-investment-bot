---
name: quality-assessment
description: Comprehensive quality assessment of the investment bot — critical bugs, component scores, and prioritized fix plan (2026-03-15)
type: project
---

# Investment Bot — Quality Assessment (2026-03-15)

## Architecture: 8/10 — Well-designed, production-grade

Layered architecture: data collection → analysis → signal generation → gate checks → execution → monitoring → feedback. 884 tests pass. Deployment automated via GitHub Actions. Cost: ~$6.13/mo.

---

## Critical Issues (money-losing bugs)

| # | Issue | Component | Risk |
|---|-------|-----------|------|
| 1 | **Paper PnL double-counts fees** — deducts buy+sell fees at exit, ~100bps worse than reality | `binance_trader.py` | Understates strategy performance |
| 2 | **Circuit breaker ignores unrealized PnL** — drawdown check uses available balance only, not open position losses | `circuit_breaker.py` | Allows 30%+ drawdown when limit is 15% |
| 3 | **OCO failure leaves position naked** — if Binance API is down after BUY fill, no server-side SL/TP protection | `binance_trader.py` | Unlimited downside during outage |
| 4 | **Trailing stop peaks lost on restart** — in-memory only, resets to current price after crash | `position_monitor.py` | Early/late trailing stop triggers |
| 5 | **Signal strength mismatch** — scoring mode produces 0.43 strength at threshold=3, but quality gate requires 0.65 | `signal_engine.py` | Scoring mode signals silently blocked |

## High Priority Issues

| # | Issue | Component |
|---|-------|-----------|
| 6 | Auto-tuner Sharpe calculation is non-standard (variable annualization factor) | `auto_tuner.py` |
| 7 | No crypto-specific events in calendar (halving, protocol upgrades, regulatory) | `event_calendar.py` |
| 8 | Source uniqueness hardcoded to 0.5 — feedback loop can't distinguish original vs reprint sources | `feedback_loop.py` |
| 9 | Connection pool (max 10) can exhaust under peak load with 324 symbols | `database.py` |
| 10 | No graceful shutdown handler — SIGTERM kills mid-query | `main.py` |
| 11 | Add-to-position live path has race condition — OCO cancelled before new one placed | `binance_trader.py` |

## Moderate Issues

| # | Issue | Component |
|---|-------|-----------|
| 12 | `'pending' in dir()` variable scope hack in pre-trade gates | `pre_trade_gates.py` |
| 13 | Rotation sets cooldown even when sell fails | `trade_executor.py` |
| 14 | Macro regime suppresses buys but doesn't accelerate exits in risk-off | `macro_regime.py` |
| 15 | Auto-tuner doesn't persist parameters — lost on restart | `auto_tuner.py` |
| 16 | Health check doesn't detect hung tasks or pool exhaustion | `main.py` |
| 17 | No Dockerfile HEALTHCHECK instruction | `Dockerfile` |
| 18 | FOMC/CPI dates hardcoded for 2026, will go stale in 2027 | `event_calendar.py` |

## What's Working Well

- **Gemini prompt engineering** — Position analyst, article scoring, and news assessment prompts are well-structured with calibration guidance
- **Multi-layer risk management** — Circuit breaker + SL/TP + trailing stop + sector limits + macro regime + event calendar
- **Cost efficiency** — $6.13/mo total, grounded search within free tier (1,344/day of 1,500 limit)
- **News pipeline** — RSS + web scraping + deep enrichment + per-article scoring + fuzzy dedup + per-sector batched grounded search
- **Test coverage** — 884 tests, comprehensive for core paths
- **Autonomous systems** — Feedback loop, auto-tuner, source discovery all functional with safety bounds
- **Signal confirmation** — Telegram approval flow with TOCTOU guard on execution
- **Dynamic risk** — ATR-based per-symbol SL/TP, daily SMA trend filter, limit orders with pullback entry
- **Correlation awareness** — Asset class concentration limits + linear position size scaling

## Component Scores

| Component | Score | Notes |
|-----------|-------|-------|
| Signal Engine | 7/10 | Good dual-mode, but strength calc mismatch |
| Pre-Trade Gates | 8/10 | Comprehensive 7-gate chain, minor code smells |
| Trade Execution | 7/10 | Paper/live split clean, but PnL fee bug |
| Position Monitor | 7/10 | SL/TP/trailing works, peaks not persisted |
| Position Analyst | 8/10 | Pro model, good prompt, validation added |
| Circuit Breaker | 6/10 | Missing unrealized PnL is a real gap |
| News Pipeline | 8/10 | Strong after recent improvements |
| Macro Regime | 8/10 | Reliable, well-tested, good caching |
| Event Calendar | 7/10 | Stocks good, crypto events missing |
| Feedback Loop | 6/10 | Functional but uniqueness metric broken |
| Auto-Tuner | 6/10 | Conservative safety, but Sharpe calc is off |
| Infrastructure | 7/10 | Runs reliably, lacks monitoring depth |

## Recommended Fix Priority

### Phase 1 — Fix money-losing bugs
1. Fix paper PnL fee double-counting
2. Include unrealized PnL in circuit breaker drawdown check
3. Persist trailing stop peaks to DB
4. Fix `pending` variable scope in pre-trade gates

### Phase 2 — Harden live trading
5. Close position if OCO placement completely fails
6. Fix add-to-position OCO race condition
7. Add Dockerfile HEALTHCHECK
8. Add graceful SIGTERM handler

### Phase 3 — Improve signal quality
9. Fix scoring mode signal strength denominator
10. Add crypto events to calendar
11. Fix auto-tuner Sharpe annualization
12. Implement real uniqueness tracking in feedback loop

---

## Detailed Findings by Area

### Signal Engine
- Scoring mode: 7 indicators (SMA, RSI, news, MACD, BB, volume, order book) vote +/-1 each, threshold=3
- Signal strength = score/7.0 → at threshold=3, strength=0.43 which fails the 0.65 quality gate
- Sentiment mode: Gemini confidence x freshness multiplier, gated by SMA trend + RSI veto
- Fallback: if Gemini unavailable, falls back to scoring mode (implemented 2026-03-15)
- Hardcoded values: bid/ask ratios (1.5/0.67), freshness multipliers, BB std dev, MACD periods

### Trade Execution
- Paper PnL: `simulated_fees = d_price * d_qty * fee_pct + d_entry * d_qty * fee_pct` — double-counts
- Live PnL: `pnl = (d_exit - d_entry) * d_qty - d_fees` — correct (single deduction)
- Slippage: uniform 0.1%, not order-size-aware
- Limit orders: PENDING → OPEN transition on fill, no slippage simulation for limits

### Circuit Breaker (5 checks)
1. Daily loss limit (% of balance)
2. Max drawdown from peak (% — but peak doesn't include unrealized PnL)
3. Balance floor (absolute USD minimum)
4. Consecutive losses (last N trades)
5. Cooldown period (24h after trip)
- `get_unrealized_pnl()` exists but is NOT used in drawdown check

### Position Monitor
- Trailing stop peaks stored in `bot_state._trailing_stop_peaks` (in-memory dict)
- `save_trailing_stop_peak()` function exists but is never called from monitor_position
- On restart: peaks reset to current price, potentially triggering premature exits
- Strategic positions: catastrophic SL only (15-30%), no TP, no trailing

### Auto-Tuner
- Sharpe approximation: caps actual PnL at SL/TP bounds, no intraday OHLC
- Annualization: `sqrt(min(250, len(returns) * 7))` — non-standard, varies with sample size
- Revert bug: if old Sharpe was negative, revert logic is skipped (`old_sharpe > 0` check)
- Parameters updated in-memory only, lost on restart

### Macro Regime
- Inputs: VIX level + trend, S&P 500 vs SMA200, 10Y yield direction, BTC vs SMA50
- Weighted scoring: VIX signal 2.0x, others 1.0-1.5x
- Classification: RISK_ON (score >= 3), CAUTION (-3 to 3), RISK_OFF (score <= -3)
- Binary buy suppression in RISK_OFF — no accelerated exits

### Event Calendar
- Covers: stock earnings (yfinance), FOMC (8 dates/2026), CPI (12 dates/2026)
- Missing: Bitcoin halving, protocol upgrades, regulatory announcements, sector-specific events
- Warning cooldown is per-symbol (not per-event), can miss second event within 12h

### Feedback Loop
- Source reliability = 0.3x availability + 0.2x relevance + 0.2x uniqueness + 0.3x signal_contribution
- Uniqueness hardcoded to 0.5 for all sources (tracking not implemented)
- Promotion thresholds: 5 signals for trial->standard, 10 for standard->premium (low samples)

### Infrastructure
- e2-micro spot VM, Frankfurt, 30GB disk, 2GB swap for TensorFlow
- Connection pool: ThreadedConnectionPool(1, 10) — no saturation monitoring
- Health check: verifies tasks running + DB connectivity + Telegram init — doesn't detect hangs
- No SIGTERM handler, no Dockerfile HEALTHCHECK

## Cost Breakdown (~$6.13/mo)

| Component | Cost |
|-----------|------|
| GCE e2-micro SPOT (Frankfurt) | ~$2.55 |
| Persistent disk (30 GB) | ~$1.20 |
| Network egress | ~$0.50 |
| Gemini API (article scoring + news assessment) | ~$1.88 |
| Grounded search (1,344/day) | $0.00 (free tier) |
| Binance, Alpha Vantage, RSS, yfinance | Free |
| **Total** | **~$6.13** |

## Test Coverage

- 884 tests passing (as of 2026-03-15)
- Good coverage: signal engines, news pipeline, circuit breaker, position monitor, sector limits, event calendar
- Weak coverage: OCO failure recovery, unrealized PnL in drawdown, trailing stop persistence, partial fills
