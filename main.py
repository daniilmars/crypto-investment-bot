#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-11-02_v2
# Force redeploy 2025-11-02
# Force redeploy 2025-10-16

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import uvicorn
from fastapi import FastAPI, Request
from telegram import Update

from src.analysis.signal_engine import generate_signal
from src.analysis.stock_signal_engine import generate_stock_signal
from src.analysis.technical_indicators import calculate_rsi, calculate_sma
from src.collectors.alpha_vantage_data import (get_company_overview,
                                               get_daily_prices,
                                               get_stock_price,
                                               get_batch_stock_prices,
                                               get_batch_daily_prices)
from src.collectors.binance_data import get_current_price, get_all_prices
from src.config import app_config
from src.analysis.macro_regime import get_macro_regime
from src.analysis.event_calendar import get_event_warnings_for_positions
from src.database import (get_historical_prices,
                          get_trade_history_stats, get_trade_summary,
                          initialize_database,
                          load_trailing_stop_peaks, save_signal,
                          load_stoploss_cooldowns, load_signal_cooldowns,
                          save_signal_cooldown,
                          save_macro_regime)
from src.execution.binance_trader import (get_account_balance,
                                          get_open_positions, place_order,
                                          _is_live_trading, _get_trading_mode)
from src.execution.circuit_breaker import (check_circuit_breaker, get_daily_pnl,
                                           get_recent_closed_trades,
                                           get_unrealized_pnl,
                                           resolve_stale_circuit_breaker_events,
                                           update_session_peak,
                                           load_session_peaks)
from src.execution.stock_trader import (
    get_stock_positions, get_stock_balance,
    _is_market_open, _check_pdt_rule,
)
from src.logger import log
from src.analysis.market_alerts import generate_daily_digest
from src.notify.telegram_bot import (send_telegram_alert, start_bot,
                                     register_execute_callback,
                                     cleanup_expired_signals,
                                     send_auto_bot_summary,
                                     send_market_event_alert)
from src.notify.telegram_alerts_enhanced import (
    send_morning_briefing, send_portfolio_digest,
    check_realtime_alerts, send_realtime_alerts,
)
from src.state import bot_is_running
from src.orchestration import bot_state
from src.orchestration.position_monitor import monitor_position
from src.orchestration.position_analyst import run_position_analyst
from src.orchestration.trade_executor import (
    execute_confirmed_signal, process_trade_signal,
)
from src.orchestration.news_pipeline import (
    collect_and_analyze_news, run_proactive_market_alerts,
)
from src.analysis.signal_attribution import (
    record_signal_attribution, link_attribution_to_order,
)

# Initialize the database at the start of the application
try:
    initialize_database()
except Exception as e:
    log.error(f"Failed to initialize database: {e}", exc_info=True)
    log.warning("Continuing startup — database may be unavailable.")

# --- FastAPI App Initialization ---
app = FastAPI()
application = None
_background_tasks = []


# State is managed centrally in bot_state module


async def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    settings = app_config.get('settings', {})

    # Load all settings
    watch_list = settings.get('watch_list', ['BTC'])
    sma_period = settings.get('sma_period', 20)
    rsi_period = settings.get('rsi_period', 14)
    rsi_overbought_threshold = settings.get('rsi_overbought_threshold', 70)
    rsi_oversold_threshold = settings.get('rsi_oversold_threshold', 30)
    stoploss_cooldown_hours = settings.get('stoploss_cooldown_hours', 6)
    signal_cooldown_hours = settings.get('signal_cooldown_hours', 4)

    signal_mode = settings.get('signal_mode', 'scoring')
    sentiment_signal_cfg = settings.get('sentiment_signal', {})
    sentiment_config = {
        'min_gemini_confidence': sentiment_signal_cfg.get('min_gemini_confidence', 0.7),
        'min_vader_score': sentiment_signal_cfg.get('min_vader_score', 0.3),
        'rsi_buy_veto_threshold': sentiment_signal_cfg.get('rsi_buy_veto_threshold', 75),
        'rsi_sell_veto_threshold': sentiment_signal_cfg.get('rsi_sell_veto_threshold', 25),
    }

    paper_trading = settings.get('paper_trading', True)
    paper_trading_initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    max_concurrent_positions = settings.get('max_concurrent_positions', 3)
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)

    # --- Dynamic Position Sizing (Kelly Criterion) ---
    trade_stats = await get_trade_history_stats()
    kelly_fraction = trade_stats.get('kelly_fraction', 0.0)
    if kelly_fraction > 0 and trade_stats.get('total_trades', 0) >= 10:
        effective_risk_pct = kelly_fraction
        log.info(f"Using Kelly-based position sizing: {effective_risk_pct:.4f} "
                 f"(based on {trade_stats['total_trades']} trades, "
                 f"win rate {trade_stats['win_rate']:.1%})")
    else:
        effective_risk_pct = trade_risk_percentage
        log.info(f"Using fixed position sizing: {effective_risk_pct:.4f} "
                 f"({trade_stats.get('total_trades', 0)} trades, need 10+ for Kelly)")

    # --- Macro Regime Detection ---
    macro_regime_result = get_macro_regime()
    macro_multiplier = macro_regime_result['position_size_multiplier']
    suppress_buys = macro_regime_result.get('suppress_buys', False)
    log.info(f"Macro regime: {macro_regime_result['regime']} "
             f"(mult={macro_multiplier}, suppress_buys={suppress_buys})")

    try:
        await save_macro_regime(macro_regime_result)
    except Exception as e:
        log.warning(f"Failed to save macro regime: {e}")

    # --- Real-time Market Alerts (regime change, VIX spike) ---
    try:
        rt_alerts = check_realtime_alerts(macro_regime_result)
        if rt_alerts and application:
            await send_realtime_alerts(application, rt_alerts)
    except Exception as e:
        log.warning(f"Realtime alerts check failed: {e}")

    # 1. Collect news data
    log.info("Fetching data from all sources...")

    stock_settings = settings.get('stock_trading', {})
    stock_watch_list = stock_settings.get('watch_list', []) if stock_settings.get('enabled', False) else []
    all_symbols = list(set(watch_list + stock_watch_list))

    # Build current prices dict via batch API call
    current_prices_dict = {}
    all_binance_prices = get_all_prices()
    for sym in all_symbols:
        api_sym = sym if sym.endswith("USDT") else f"{sym}USDT"
        price = all_binance_prices.get(api_sym)
        if price:
            current_prices_dict[sym] = price

    gemini_assessments, news_per_symbol = await collect_and_analyze_news(
        all_symbols, current_prices_dict, settings)

    # --- Proactive Market Event Alerts ---
    all_watch_symbols = watch_list + settings.get('stock_trading', {}).get('watch_list', [])
    await run_proactive_market_alerts(
        all_watch_symbols, settings, gemini_assessments, news_per_symbol)

    # Cache open positions once per cycle
    is_live = _is_live_trading()
    _cached_crypto_positions = get_open_positions(trading_strategy='manual') if (paper_trading or is_live) else []

    auto_cfg = settings.get('auto_trading', {})
    auto_enabled = auto_cfg.get('enabled', False)
    _cached_auto_positions = get_open_positions(trading_strategy='auto') if auto_enabled else []

    open_positions = _cached_crypto_positions
    auto_open_crypto = [p for p in _cached_auto_positions
                        if p.get('asset_type', 'crypto') == 'crypto' and p['status'] == 'OPEN']

    # Risk management config for position monitor
    risk_cfg = dict(
        stop_loss_pct=stop_loss_percentage,
        take_profit_pct=take_profit_percentage,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_stop_activation=trailing_stop_activation,
        trailing_stop_distance=trailing_stop_distance,
        stoploss_cooldown_hours=stoploss_cooldown_hours,
    )
    trading_mode = _get_trading_mode()
    news_config = settings.get('news_analysis', {})

    # Circuit breaker check — once per cycle, crypto only
    live_config = settings.get('live_trading', {})
    cb_tripped = False
    if is_live:
        cb_balance = get_account_balance(asset_type='crypto').get('USDT', 0)
        update_session_peak(cb_balance, 'crypto')
        cb_daily_pnl = get_daily_pnl(asset_type='crypto')
        cb_unrealized = get_unrealized_pnl(current_prices_dict, asset_type='crypto')
        cb_effective_pnl = cb_daily_pnl + cb_unrealized
        cb_recent_trades = get_recent_closed_trades(
            limit=live_config.get('max_consecutive_losses', 3), asset_type='crypto')
        cb_tripped, cb_reason = check_circuit_breaker(
            cb_balance, cb_effective_pnl, cb_recent_trades, asset_type='crypto')
        if cb_tripped:
            log.warning(f"Circuit breaker active for this cycle: {cb_reason}")
            await send_telegram_alert({
                "signal": "CIRCUIT_BREAKER", "symbol": "ALL",
                "reason": f"Circuit breaker (crypto): {cb_reason}. Skipping all crypto trading this cycle."
            })

    # Process each symbol in the watch list
    for symbol in watch_list:
        signal = None
        log.info(f"--- Processing symbol: {symbol} ---")

        # Use batch price from all_binance_prices, fall back to individual call
        api_symbol = symbol if symbol.endswith("USDT") else f"{symbol}USDT"
        current_price = all_binance_prices.get(api_symbol)
        if current_price:
            from src.collectors.binance_data import save_price_data
            save_price_data({'symbol': api_symbol, 'price': current_price})
        else:
            price_data = get_current_price(api_symbol)
            if not price_data or not price_data.get('price'):
                log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
                continue
            current_price = float(price_data.get('price'))

        if not current_price:
            log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
            continue
        log.info(f"Current price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring: SL/TP/Trailing Stop ---
        if paper_trading or is_live:
            for position in _cached_crypto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    await monitor_position(
                        position, current_price, **risk_cfg,
                        mode_label=trading_mode.upper())

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new signal generation and trading.")
            continue

        # 2. Compute technical indicators (SMA/RSI)
        log.info(f"Analyzing data for {symbol}...")

        price_limit = max(sma_period, rsi_period) + 1
        historical_prices = await get_historical_prices(symbol, limit=price_limit)

        market_price_data = {'current_price': current_price, 'sma': None, 'rsi': None}
        if len(historical_prices) >= sma_period:
            price_series = pd.Series(historical_prices)
            market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]
        market_price_data['rsi'] = calculate_rsi(historical_prices, period=rsi_period)
        log.info(f"Technical Indicators for {symbol}: SMA={market_price_data['sma']}, RSI={market_price_data['rsi']}")

        # --- Position Analyst (tri-state: HOLD / INCREASE / SELL) ---
        if paper_trading or is_live:
            for position in _cached_crypto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    await run_position_analyst(
                        position, current_price, market_price_data, settings,
                        news_per_symbol, trailing_stop_activation=trailing_stop_activation)

        # Skip signal generation for symbols with open positions
        if (paper_trading or is_live) and any(
            p['symbol'] == symbol and p['status'] == 'OPEN'
            for p in _cached_crypto_positions
        ):
            log.debug(f"Skipping signal generation for {symbol}: position open (managed by position monitor).")
            continue

        # 3. Generate a signal
        log.info(f"Generating signal for {symbol}...")

        symbol_news_data = None
        ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None
        sym_news = news_per_symbol.get(symbol)

        if ga or sym_news:
            symbol_news_data = {
                'avg_sentiment_score': sym_news.get('avg_sentiment_score', 0) if sym_news else 0,
                'sentiment_buy_threshold': news_config.get('sentiment_buy_threshold', 0.15),
                'sentiment_sell_threshold': news_config.get('sentiment_sell_threshold', -0.15),
                'min_gemini_confidence': news_config.get('min_gemini_confidence',
                                                         news_config.get('min_claude_confidence', 0.6)),
            }
            if ga:
                symbol_news_data['gemini_assessment'] = ga

        signal = generate_signal(
            symbol=symbol,
            market_data=market_price_data,
            news_sentiment_data=symbol_news_data,
            signal_mode=signal_mode,
            sentiment_config=sentiment_config,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
        )
        log.info(f"Generated Signal for {symbol}: {signal}")
        await save_signal(signal)

        # Record signal attribution for non-HOLD signals
        _attribution_id = None
        if signal.get('signal') not in ('HOLD', None):
            try:
                sym_articles = news_per_symbol.get(symbol, {}).get('articles', [])
                _attribution_id = record_signal_attribution(
                    signal, articles=sym_articles, gemini_assessment=ga)
            except Exception as _attr_err:
                log.debug(f"Attribution recording skipped: {_attr_err}")

        # --- 4. Trade Execution (Paper & Live) with Dynamic Sizing ---
        can_trade = paper_trading or is_live

        if can_trade:
            # Circuit breaker — already checked once before the loop
            if cb_tripped:
                continue

            log.info(f"Processing signal for {trading_mode} trading...")

            # Balance and position limits
            if is_live:
                current_balance = get_account_balance(asset_type='crypto').get('USDT', live_config.get('initial_capital', 100.0))
                active_max_positions = live_config.get('max_concurrent_positions', max_concurrent_positions)
            else:
                current_balance = get_account_balance(asset_type='crypto').get('total_usd', paper_trading_initial_capital)
                active_max_positions = max_concurrent_positions

            await process_trade_signal(
                symbol, signal, current_price, _cached_crypto_positions, current_balance,
                effective_risk_pct, signal_cooldown_hours, active_max_positions,
                suppress_buys, macro_multiplier, label=trading_mode)

        # --- Auto-Trading Shadow Bot: Position Monitoring ---
        if auto_enabled:
            for position in _cached_auto_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    await monitor_position(
                        position, current_price, **risk_cfg,
                        trading_strategy='auto', mode_label='AUTO')

        # --- Auto-Trading Shadow Bot: Signal Execution ---
        if auto_enabled and not cb_tripped and bot_is_running.is_set() and signal is not None:
            auto_open_crypto = [p for p in _cached_auto_positions
                                if p.get('asset_type', 'crypto') == 'crypto' and p['status'] == 'OPEN']
            auto_max = auto_cfg.get('max_concurrent_positions', max_concurrent_positions)
            auto_balance = get_account_balance(asset_type='crypto', trading_strategy='auto')
            auto_available = auto_balance.get('USDT', 0)

            # Create a copy of the signal for auto-trading (don't mutate the original)
            auto_signal = dict(signal)
            await process_trade_signal(
                symbol, auto_signal, current_price, auto_open_crypto, auto_available,
                effective_risk_pct, signal_cooldown_hours, auto_max,
                suppress_buys, macro_multiplier,
                trading_strategy='auto', label='AUTO', is_auto=True)

    # --- Event warnings for open positions ---
    try:
        all_open = open_positions + (auto_open_crypto if auto_enabled else [])
        event_warnings = get_event_warnings_for_positions(all_open)
        for warn in event_warnings:
            log.info(f"Event warning: {warn['symbol']} — {warn['event_type']} in {warn['hours_until']:.0f}h")
            await send_telegram_alert({
                'signal': 'INFO', 'symbol': warn['symbol'],
                'current_price': 0,
                'reason': f"Upcoming {warn['event_type']} in {warn['hours_until']:.0f}h — "
                          f"consider reducing exposure.",
            })
    except Exception as e:
        log.warning(f"Event warnings failed: {e}")

    # --- Run Stock Trading Cycle ---
    await run_stock_cycle(settings, news_per_symbol=news_per_symbol,
                          news_config=news_config,
                          gemini_assessments=gemini_assessments,
                          signal_mode=signal_mode,
                          sentiment_config=sentiment_config,
                          macro_multiplier=macro_multiplier,
                          suppress_buys=suppress_buys)


async def run_stock_cycle(settings, news_per_symbol=None, news_config=None,
                          gemini_assessments=None, signal_mode="scoring",
                          sentiment_config=None,
                          macro_multiplier=1.0, suppress_buys=False):
    """
    Executes one cycle of stock trading analysis for all configured stock symbols.
    """
    if news_per_symbol is None:
        news_per_symbol = {}
    if news_config is None:
        news_config = {}
    stock_settings = settings.get('stock_trading', {})
    if not stock_settings.get('enabled', False):
        log.info("Stock trading is disabled. Skipping stock cycle.")
        return

    # IPO watchlist promotion
    ipo_cfg = settings.get('ipo_tracking', {})
    if ipo_cfg.get('enabled', False) and ipo_cfg.get('auto_add_to_watchlist', True):
        try:
            from src.collectors.ipo_watchlist_promoter import promote_new_listings
            new_tickers = promote_new_listings(settings)
            if new_tickers:
                log.info(f"[IPO] Promoted {len(new_tickers)} new tickers to watchlist: {new_tickers}")
                for ticker in new_tickers:
                    try:
                        await send_telegram_alert({
                            'signal': 'INFO', 'symbol': ticker, 'current_price': 0,
                            'reason': f'IPO Watchlist Update: Added {ticker} to stock watchlist. Now being tracked for signals.',
                        })
                    except Exception:
                        pass
        except Exception as e:
            log.warning(f"[IPO] Watchlist promotion failed: {e}")

    watch_list = stock_settings.get('watch_list', [])
    if not watch_list:
        log.info("Stock watch list is empty. Skipping stock cycle.")
        return

    broker = stock_settings.get('broker', 'paper_only')
    use_alpaca_data = broker == 'alpaca'

    if broker == 'alpaca' and not _is_market_open():
        log.info("NYSE is closed. Skipping stock cycle (broker=alpaca).")
        return

    log.info(f"--- Starting stock trading cycle for {len(watch_list)} symbols (broker={broker}) ---")

    # Load stock-specific settings
    sma_period = stock_settings.get('sma_period', settings.get('sma_period', 20))
    rsi_period = stock_settings.get('rsi_period', settings.get('rsi_period', 14))
    rsi_overbought = stock_settings.get('rsi_overbought_threshold', settings.get('rsi_overbought_threshold', 70))
    rsi_oversold = stock_settings.get('rsi_oversold_threshold', settings.get('rsi_oversold_threshold', 30))
    pe_buy = stock_settings.get('pe_ratio_buy_threshold', 25)
    pe_sell = stock_settings.get('pe_ratio_sell_threshold', 40)
    earnings_sell = stock_settings.get('earnings_growth_sell_threshold', -10)
    vol_multiplier = stock_settings.get('volume_spike_multiplier', 1.5)
    signal_threshold = settings.get('signal_threshold', 3)
    stoploss_cooldown_hours = settings.get('stoploss_cooldown_hours', 6)
    signal_cooldown_hours = settings.get('signal_cooldown_hours', 4)

    paper_trading = settings.get('paper_trading', True)
    is_live = _is_live_trading()
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    max_concurrent_positions = stock_settings.get('max_concurrent_positions', settings.get('max_concurrent_positions', 3))
    paper_trading_initial_capital = stock_settings.get('paper_trading_initial_capital', settings.get('paper_trading_initial_capital', 10000.0))
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)

    risk_cfg = dict(
        stop_loss_pct=stop_loss_percentage,
        take_profit_pct=take_profit_percentage,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_stop_activation=trailing_stop_activation,
        trailing_stop_distance=trailing_stop_distance,
        stoploss_cooldown_hours=stoploss_cooldown_hours,
    )

    # Batch-fetch stock prices and daily data
    stock_batch_prices = get_batch_stock_prices(watch_list) if not use_alpaca_data else {}
    stock_batch_daily = get_batch_daily_prices(watch_list) if not use_alpaca_data else {}

    # Cache open stock positions once per cycle
    _cached_stock_positions = get_open_positions(asset_type='stock', trading_strategy='manual') if (paper_trading or is_live) else []
    _cached_alpaca_positions = get_stock_positions() if broker == 'alpaca' else []

    auto_cfg = settings.get('auto_trading', {})
    auto_enabled = auto_cfg.get('enabled', False)
    _cached_auto_stock_positions = get_open_positions(asset_type='stock', trading_strategy='auto') if auto_enabled else []

    # Stock circuit breaker check — once per cycle
    live_config = settings.get('live_trading', {})
    stock_cb_tripped = False
    if is_live or paper_trading:
        stock_cb_balance = get_account_balance(asset_type='stock').get('total_usd', paper_trading_initial_capital)
        update_session_peak(stock_cb_balance, 'stock')
        stock_cb_daily_pnl = get_daily_pnl(asset_type='stock')
        # Include stock unrealized PnL in circuit breaker check
        stock_prices_dict = {sym: stock_batch_prices[sym]['price']
                             for sym in stock_batch_prices
                             if stock_batch_prices.get(sym, {}).get('price')}
        stock_cb_unrealized = get_unrealized_pnl(stock_prices_dict, asset_type='stock')
        stock_cb_effective_pnl = stock_cb_daily_pnl + stock_cb_unrealized
        stock_cb_recent = get_recent_closed_trades(
            limit=live_config.get('max_consecutive_losses', 3), asset_type='stock')
        stock_cb_tripped, stock_cb_reason = check_circuit_breaker(
            stock_cb_balance, stock_cb_effective_pnl, stock_cb_recent, asset_type='stock')
        if stock_cb_tripped:
            log.warning(f"Circuit breaker active for stocks: {stock_cb_reason}")
            await send_telegram_alert({
                "signal": "CIRCUIT_BREAKER", "symbol": "ALL_STOCKS",
                "reason": f"Circuit breaker (stock): {stock_cb_reason}. Skipping all stock trading this cycle."
            })

    for symbol in watch_list:
        signal = None
        log.info(f"--- Processing stock: {symbol} ---")

        # Use batch price first, fall back to per-symbol call
        if use_alpaca_data:
            from src.collectors.alpaca_data import get_stock_price_alpaca
            price_data = get_stock_price_alpaca(symbol)
        elif symbol in stock_batch_prices:
            price_data = stock_batch_prices[symbol]
        else:
            price_data = get_stock_price(symbol)
        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for stock {symbol}. Skipping.")
            continue

        current_price = price_data['price']
        log.info(f"Current stock price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring with Trailing Stop ---
        if paper_trading or is_live:
            trading_mode_label = 'PAPER TRADE' if paper_trading else _get_trading_mode().upper()
            for position in _cached_stock_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    result = await monitor_position(
                        position, current_price, **risk_cfg,
                        asset_type='stock', mode_label=trading_mode_label)

                    # Run position analyst only if position wasn't closed
                    if result == 'none':
                        await run_position_analyst(
                            position, current_price,
                            {'current_price': current_price, 'sma': None, 'rsi': None},
                            settings, news_per_symbol,
                            trailing_stop_activation=trailing_stop_activation,
                            asset_type='stock')

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new stock signal generation.")
            continue

        # Skip signal generation for stocks with open positions
        if (paper_trading or is_live) and any(
            p['symbol'] == symbol and p['status'] == 'OPEN'
            for p in _cached_stock_positions
        ):
            log.debug(f"Skipping signal generation for stock {symbol}: position open.")
            continue
        if broker == 'alpaca' and any(
            p['symbol'] == symbol for p in _cached_alpaca_positions
        ):
            log.debug(f"Skipping signal generation for stock {symbol}: Alpaca position open.")
            continue

        # Fetch daily prices for technical analysis
        if use_alpaca_data:
            from src.collectors.alpaca_data import get_daily_prices_alpaca
            daily_data = get_daily_prices_alpaca(symbol)
        elif symbol in stock_batch_daily:
            daily_data = stock_batch_daily[symbol]
        else:
            daily_data = get_daily_prices(symbol)
        if not daily_data or not daily_data.get('prices'):
            log.warning(f"Could not fetch daily prices for {symbol}. Skipping analysis.")
            continue

        prices = daily_data['prices']
        volumes = daily_data.get('volumes', [])

        sma_value = calculate_sma(prices, period=sma_period)
        rsi_value = calculate_rsi(prices, period=rsi_period)

        market_data = {'current_price': current_price, 'sma': sma_value, 'rsi': rsi_value}

        volume_data = {}
        if volumes:
            current_volume = volumes[-1] if volumes else None
            avg_volume = sum(volumes) / len(volumes) if volumes else None
            volume_data = {
                'current_volume': current_volume,
                'avg_volume': avg_volume,
                'price_change_percent': price_data.get('change_percent', 0)
            }

        is_international = '.' in symbol and not symbol.startswith('BRK')
        fundamental_data = get_company_overview(symbol) if not is_international else {}

        stock_news_data = None
        ga = gemini_assessments.get('symbol_assessments', {}).get(symbol) if gemini_assessments else None
        sym_news = news_per_symbol.get(symbol)

        if ga or sym_news:
            stock_news_data = {
                'avg_sentiment_score': sym_news.get('avg_sentiment_score', 0) if sym_news else 0,
                'sentiment_buy_threshold': news_config.get('sentiment_buy_threshold', 0.15),
                'sentiment_sell_threshold': news_config.get('sentiment_sell_threshold', -0.15),
                'min_gemini_confidence': news_config.get('min_gemini_confidence',
                                                         news_config.get('min_claude_confidence', 0.6)),
            }
            if ga:
                stock_news_data['gemini_assessment'] = ga

        stock_sentiment_config = dict(sentiment_config or {})
        stock_sentiment_config['pe_buy_veto_threshold'] = pe_sell

        signal = generate_stock_signal(
            symbol=symbol,
            market_data=market_data,
            volume_data=volume_data,
            fundamental_data=fundamental_data,
            rsi_overbought_threshold=rsi_overbought,
            rsi_oversold_threshold=rsi_oversold,
            pe_ratio_buy_threshold=pe_buy,
            pe_ratio_sell_threshold=pe_sell,
            earnings_growth_sell_threshold=earnings_sell,
            volume_spike_multiplier=vol_multiplier,
            news_sentiment_data=stock_news_data,
            historical_prices=prices,
            signal_threshold=signal_threshold,
            signal_mode=signal_mode,
            sentiment_config=stock_sentiment_config,
        )
        signal['asset_type'] = 'stock'
        log.info(f"Generated Stock Signal for {symbol}: {signal}")
        await save_signal(signal)

        # --- Trade Execution (broker-aware, unified pipeline) ---
        if stock_cb_tripped:
            log.info(f"Skipping trade execution for stock {symbol}: circuit breaker active.")
            signal['signal'] = 'HOLD'

        stock_trade_stats = await get_trade_history_stats()
        stock_kelly = stock_trade_stats.get('kelly_fraction', 0.0)
        stock_risk_pct = stock_kelly if (stock_kelly > 0 and stock_trade_stats.get('total_trades', 0) >= 10) else trade_risk_percentage

        if broker == 'alpaca':
            pdt_status = _check_pdt_rule()
            balance = get_stock_balance()
            buying_power = balance.get('buying_power', 0)
            await process_trade_signal(
                symbol, signal, current_price, _cached_alpaca_positions, buying_power,
                stock_risk_pct, signal_cooldown_hours, max_concurrent_positions,
                suppress_buys, macro_multiplier,
                asset_type='stock', broker='alpaca', pdt_status=pdt_status)
        else:
            current_balance = get_account_balance(asset_type='stock').get('total_usd', paper_trading_initial_capital)
            await process_trade_signal(
                symbol, signal, current_price, _cached_stock_positions, current_balance,
                stock_risk_pct, signal_cooldown_hours, max_concurrent_positions,
                suppress_buys, macro_multiplier, asset_type='stock')

        # --- Auto-Trading Shadow Bot: Stock Position Monitoring ---
        if auto_enabled:
            for position in _cached_auto_stock_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    await monitor_position(
                        position, current_price, **risk_cfg,
                        asset_type='stock', trading_strategy='auto', mode_label='AUTO')

        # --- Auto-Trading Shadow Bot: Stock Signal Execution ---
        if auto_enabled and not stock_cb_tripped and bot_is_running.is_set() and signal is not None:
            auto_open_stocks = [p for p in _cached_auto_stock_positions if p['status'] == 'OPEN']
            auto_max = auto_cfg.get('max_concurrent_positions', max_concurrent_positions)
            auto_balance = get_account_balance(asset_type='stock', trading_strategy='auto')
            auto_available = auto_balance.get('USDT', 0)

            auto_signal = dict(signal)
            await process_trade_signal(
                symbol, auto_signal, current_price, auto_open_stocks, auto_available,
                trade_risk_percentage, signal_cooldown_hours, auto_max,
                suppress_buys, macro_multiplier,
                asset_type='stock', trading_strategy='auto', label='AUTO', is_auto=True)

    log.info("--- Stock trading cycle complete ---")


async def bot_loop():
    """
    The main indefinite loop for the bot.
    """
    run_interval_minutes = app_config.get('settings', {}).get('run_interval_minutes', 15)
    while True:
        try:
            await run_bot_cycle()
        except Exception as e:
            log.error(f"Error in bot_loop cycle: {e}", exc_info=True)
        log.info(f"Cycle complete. Waiting for {run_interval_minutes} minutes...")
        await asyncio.sleep(run_interval_minutes * 60)


async def run_single_status_update():
    """Fetches and sends a single status update."""
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    interval_hours = status_config.get('interval_hours', 1)

    from src.database import get_trade_summary
    from src.notify.telegram_bot import send_performance_report

    try:
        log.info("Fetching trade summary for status update...")
        summary = await get_trade_summary(hours_ago=interval_hours)
        if application:
            await send_performance_report(application, summary, interval_hours)
    except Exception as e:
        log.error(f"Error in run_single_status_update: {e}")


async def status_update_loop():
    """
    A separate loop to send periodic status updates.
    """
    status_config = app_config.get('settings', {}).get('regular_status_update', {})
    if not status_config.get('enabled'):
        log.info("Regular status updates are disabled.")
        return

    interval_hours = status_config.get('interval_hours', 1)
    log.info(f"Starting regular status update loop. Interval: {interval_hours} hours.")

    while True:
        try:
            await run_single_status_update()
        except Exception as e:
            log.error(f"Error in status_update_loop: {e}", exc_info=True)
        await asyncio.sleep(interval_hours * 3600)


async def _signal_cleanup_loop():
    """Periodically cleans up expired pending signals."""
    while True:
        try:
            await cleanup_expired_signals()
        except Exception as e:
            log.error(f"Error in signal cleanup loop: {e}", exc_info=True)
        await asyncio.sleep(60)


async def auto_bot_summary_loop():
    """Periodic summary of auto-bot performance."""
    auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
    if not auto_cfg.get('enabled', False):
        return
    interval = auto_cfg.get('summary_interval_hours', 1)
    while True:
        await asyncio.sleep(interval * 3600)
        try:
            summary = await get_trade_summary(hours_ago=interval, trading_strategy='auto')
            auto_positions = get_open_positions(trading_strategy='auto')
            auto_balance = get_account_balance(trading_strategy='auto')
            if application:
                await send_auto_bot_summary(application, summary, auto_positions, auto_balance, interval)
        except Exception as e:
            log.error(f"Auto-bot summary error: {e}", exc_info=True)


async def daily_digest_loop():
    """Sends a daily market calendar digest at the configured hour (UTC)."""
    alerts_cfg = app_config.get('settings', {}).get('market_alerts', {})
    if not alerts_cfg.get('enabled', True):
        return
    target_hour = alerts_cfg.get('daily_digest_hour_utc', 8)
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        try:
            digest = generate_daily_digest()
            if digest:
                await send_market_event_alert(digest)
                log.info("Daily market digest sent.")
        except Exception as e:
            log.error(f"Daily digest error: {e}", exc_info=True)


async def morning_briefing_loop():
    """Sends a morning briefing at the configured hour (UTC)."""
    cfg = app_config.get('settings', {}).get('telegram_enhancements', {}).get(
        'morning_briefing', {})
    if not cfg.get('enabled', False):
        return
    target_hour = cfg.get('hour_utc', 13)
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_seconds = (next_run - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        try:
            if application:
                await send_morning_briefing(application)
        except Exception as e:
            log.error(f"Morning briefing error: {e}", exc_info=True)


async def portfolio_digest_loop():
    """Periodic portfolio digest sent every N hours."""
    cfg = app_config.get('settings', {}).get('telegram_enhancements', {}).get(
        'portfolio_digest', {})
    if not cfg.get('enabled', False):
        return
    interval = cfg.get('interval_hours', 4)
    await asyncio.sleep(interval * 3600)  # skip first interval
    while True:
        try:
            if application:
                await send_portfolio_digest(application)
        except Exception as e:
            log.error(f"Portfolio digest error: {e}", exc_info=True)
        await asyncio.sleep(interval * 3600)


@app.on_event("startup")
async def startup_event():
    """
    On startup, initialize the Telegram bot, set the webhook,
    and start the background tasks.
    """
    global application
    log.info("Starting application...")

    # Restore trailing stop peaks from database (survives restarts)
    try:
        loaded = await load_trailing_stop_peaks()
        bot_state.load_peaks(loaded)
        log.info(f"Loaded {len(loaded)} trailing stop peaks from database.")
    except Exception as e:
        log.warning(f"Could not load trailing stop peaks: {e}")

    # Restore stoploss cooldowns from database (survives restarts)
    try:
        loaded_cooldowns = await load_stoploss_cooldowns()
        bot_state.load_cooldowns(loaded_cooldowns)
        log.info(f"Loaded {len(loaded_cooldowns)} stoploss cooldowns from database.")
    except Exception as e:
        log.warning(f"Could not load stoploss cooldowns: {e}")

    # Restore signal cooldowns from database (survives restarts)
    try:
        manual_cd, auto_cd = await load_signal_cooldowns()
        bot_state.load_signal_cooldown_state(manual_cd, auto_cd)
        log.info(f"Loaded {len(manual_cd)} manual + {len(auto_cd)} auto signal cooldowns from database.")
    except Exception as e:
        log.warning(f"Could not load signal cooldowns: {e}")

    # Resolve stale circuit breaker events from previous runs
    try:
        resolve_stale_circuit_breaker_events()
    except Exception as e:
        log.warning(f"Could not resolve stale circuit breaker events: {e}")

    # Load session peak balances from DB (survives restarts)
    try:
        load_session_peaks()
    except Exception as e:
        log.warning(f"Could not load session peaks: {e}")

    # Initialize the Telegram application
    application = await start_bot()

    # Set the webhook
    service_url = os.environ.get("SERVICE_URL")
    if not service_url:
        log.warning("SERVICE_URL environment variable not set. Webhook will not be set.")
    else:
        webhook_url = f"{service_url}/webhook"
        webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        log.info(f"Setting webhook to: {webhook_url}")
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret
        )

    # Register signal confirmation callback
    register_execute_callback(execute_confirmed_signal)

    # Start background tasks
    _background_tasks.append(asyncio.create_task(bot_loop()))
    _background_tasks.append(asyncio.create_task(status_update_loop()))
    _background_tasks.append(asyncio.create_task(_signal_cleanup_loop()))

    auto_cfg = app_config.get('settings', {}).get('auto_trading', {})
    if auto_cfg.get('enabled', False):
        _background_tasks.append(asyncio.create_task(auto_bot_summary_loop()))
        log.info("Auto-trading shadow bot enabled — summary loop started.")

    alerts_cfg = app_config.get('settings', {}).get('market_alerts', {})
    if alerts_cfg.get('enabled', True):
        _background_tasks.append(asyncio.create_task(daily_digest_loop()))
        log.info("Daily market digest loop started.")

    enhancements_cfg = app_config.get('settings', {}).get('telegram_enhancements', {})
    if enhancements_cfg.get('morning_briefing', {}).get('enabled', False):
        _background_tasks.append(asyncio.create_task(morning_briefing_loop()))
        log.info("Morning briefing loop started.")
    if enhancements_cfg.get('portfolio_digest', {}).get('enabled', False):
        _background_tasks.append(asyncio.create_task(portfolio_digest_loop()))
        log.info("Portfolio digest loop started.")

    log.info("Startup complete. Background tasks running.")


@app.on_event("shutdown")
async def shutdown_event_handler():
    """
    On shutdown, cancel background tasks and gracefully clean up.
    """
    log.info("Shutting down application...")

    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _background_tasks.clear()

    if application:
        try:
            from src.notify.telegram_bot import stop_bot
            await stop_bot(application)
        except Exception as e:
            log.error(f"Error stopping Telegram bot during shutdown: {e}", exc_info=True)

    log.info("Shutdown complete.")


@app.get("/health", status_code=200)
async def health_check():
    """
    Health check endpoint for Cloud Run.
    """
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(request: Request):
    """
    Handles incoming updates from the Telegram API webhook.
    """
    if not application:
        log.error("Webhook received but application not initialized.")
        return {"status": "error", "message": "Bot not initialized"}, 500

    webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if webhook_secret:
        token_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token_header != webhook_secret:
            log.warning("Webhook request rejected: invalid secret token.")
            return {"status": "error", "message": "Unauthorized"}, 403

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        log.error(f"Error processing webhook: {e}", exc_info=True)
        return {"status": "error"}, 500


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the crypto trading bot.")
    parser.add_argument('--collect-only', action='store_true',
                        help='Run in data collection mode only.')
    args = parser.parse_args()

    if args.collect_only:
        log.info("--- Collect-only mode: no standalone data collection needed ---")
        log.info("News scraping is handled by scripts/scrape_news_standalone.py")
    else:
        port = int(os.environ.get("PORT", 8080))
        log.info(f"Starting Uvicorn server on port {port}...")
        uvicorn.run(app, host="0.0.0.0", port=port)
