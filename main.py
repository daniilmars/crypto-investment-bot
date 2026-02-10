#!/usr/bin/env python3
# --- Main Application File ---
# This script orchestrates the entire bot's workflow.
# Force redeploy 2025-11-02_v2
# Force redeploy 2025-11-02
# Force redeploy 2025-10-16

import argparse
import asyncio
import os
import time

import pandas as pd
import uvicorn
from fastapi import FastAPI, Request
from telegram import Update

from src.analysis.gemini_news_analyzer import analyze_news_impact, analyze_news_with_search
from src.analysis.signal_engine import generate_signal
from src.analysis.stock_signal_engine import generate_stock_signal
from src.analysis.technical_indicators import (calculate_rsi, calculate_sma,
                                               calculate_transaction_velocity,
                                               detect_market_regime,
                                               multi_timeframe_confirmation)
from src.collectors.alpha_vantage_data import (get_company_overview,
                                               get_daily_prices,
                                               get_stock_price)
from src.collectors.binance_data import get_current_price
from src.collectors.news_data import collect_news_sentiment
from src.collectors.whale_alert import (get_stablecoin_flows,
                                        get_whale_transactions)
from src.config import app_config
from src.database import (get_historical_prices,
                          get_trade_history_stats,
                          get_transaction_timestamps_since, initialize_database,
                          save_signal)
from src.execution.binance_trader import (get_account_balance,
                                          get_open_positions, place_order)
from src.logger import log
from src.notify.telegram_bot import (send_news_alert, send_telegram_alert,
                                     start_bot)
from src.state import bot_is_running

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

# --- Trailing Stop-Loss State ---
# Tracks the highest price seen since each position was opened.
# Key: order_id, Value: highest price observed
_trailing_stop_peaks = {}


def _update_trailing_stop(order_id: str, current_price: float) -> float:
    """Updates and returns the peak price for a position (used for trailing stop)."""
    prev_peak = _trailing_stop_peaks.get(order_id, current_price)
    new_peak = max(prev_peak, current_price)
    _trailing_stop_peaks[order_id] = new_peak
    return new_peak


def _clear_trailing_stop(order_id: str):
    """Removes tracking data for a closed position."""
    _trailing_stop_peaks.pop(order_id, None)


async def run_bot_cycle():
    """
    Executes one full cycle of the bot's logic.
    """
    log.info("--- Starting new bot cycle ---")
    settings = app_config.get('settings', {})

    # Load all settings
    watch_list = settings.get('watch_list', ['BTC'])  # Default to BTC if not configured
    min_whale_value = settings.get('min_whale_transaction_usd', 1000000)
    high_interest_wallets = settings.get('high_interest_wallets', [])
    stablecoins_to_monitor = settings.get('stablecoins_to_monitor', [])
    baseline_hours = settings.get('transaction_velocity_baseline_hours', 24)
    sma_period = settings.get('sma_period', 20)
    rsi_period = settings.get('rsi_period', 14)
    rsi_overbought_threshold = settings.get('rsi_overbought_threshold', 70)
    rsi_oversold_threshold = settings.get('rsi_oversold_threshold', 30)

    # Paper trading and risk management settings
    paper_trading = settings.get('paper_trading', True)
    paper_trading_initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    max_concurrent_positions = settings.get('max_concurrent_positions', 3)
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)  # activate after 2% gain
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)     # trail 1.5% from peak

    # --- Dynamic Position Sizing (Kelly Criterion) ---
    trade_stats = get_trade_history_stats()
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

    # 1. Collect data
    log.info("Fetching data from all sources...")
    # Collect all transactions above a low threshold to ensure a rich dataset
    all_whale_transactions = get_whale_transactions(min_value_usd=500000)

    # Filter transactions in memory for signal analysis based on the higher, configured threshold
    whale_transactions = [
        tx for tx in all_whale_transactions
        if tx['amount_usd'] >= min_whale_value
    ] if all_whale_transactions else []
    log.info(f"Found {len(whale_transactions)} transactions above analysis threshold of ${min_whale_value:,.2f}")

    stablecoin_data = get_stablecoin_flows(whale_transactions, stablecoins_to_monitor)

    # Collect news for all symbols (crypto + stock combined)
    stock_settings = settings.get('stock_trading', {})
    stock_watch_list = stock_settings.get('watch_list', []) if stock_settings.get('enabled', False) else []
    all_symbols = list(set(watch_list + stock_watch_list))

    news_config = settings.get('news_analysis', {})
    gemini_assessments = None
    news_per_symbol = {}

    # --- Primary path: Gemini with Google Search grounding ---
    if news_config.get('enabled', False):
        # Build current prices dict from Binance for crypto symbols
        current_prices_dict = {}
        for sym in all_symbols:
            api_sym = sym if "USDT" in sym else f"{sym}USDT"
            pd = get_current_price(api_sym)
            if pd and pd.get('price'):
                current_prices_dict[sym] = float(pd['price'])

        gemini_assessments = analyze_news_with_search(all_symbols, current_prices_dict)

    # --- Fallback: RSS + VADER + old Gemini pipeline ---
    if gemini_assessments is None:
        log.info("Grounded news analysis unavailable — falling back to RSS+VADER pipeline.")
        news_result = collect_news_sentiment(all_symbols)
        news_per_symbol = news_result.get('per_symbol', {})
        triggered_symbols = news_result.get('triggered_symbols', [])

        if triggered_symbols and news_config.get('enabled', False):
            headlines_by_symbol = {}
            current_prices_for_news = {}
            for sym in triggered_symbols:
                sym_data = news_per_symbol.get(sym, {})
                headlines_by_symbol[sym] = sym_data.get('headlines', [])
                current_prices_for_news[sym] = sym_data.get('current_price', 0)

            gemini_assessments = analyze_news_impact(headlines_by_symbol, current_prices_for_news)
            await send_news_alert(triggered_symbols, news_per_symbol, gemini_assessments=gemini_assessments)

    # Process each symbol in the watch list
    for symbol in watch_list:
        log.info(f"--- Processing symbol: {symbol} ---")

        # Ensure the symbol format is correct for the Binance API (e.g., BTCUSDT)
        api_symbol = symbol if "USDT" in symbol else f"{symbol}USDT"
        price_data = get_current_price(api_symbol)

        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for {api_symbol}. Skipping analysis.")
            continue

        current_price = float(price_data.get('price'))
        log.info(f"Current price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring with Trailing Stop ---
        if paper_trading:
            open_positions = get_open_positions()
            for position in open_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']

                    # Update trailing stop peak tracker
                    peak_price = _update_trailing_stop(order_id, current_price)
                    drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0

                    # Trailing stop: activates once position is up by trailing_stop_activation,
                    # then closes if price drops trailing_stop_distance from the peak
                    if trailing_stop_enabled and pnl_percentage >= trailing_stop_activation:
                        if drawdown_from_peak >= trailing_stop_distance:
                            locked_gain = (peak_price - entry_price) / entry_price
                            log.info(f"[PAPER TRADE] Trailing stop triggered for {symbol}. "
                                     f"Peak: ${peak_price:,.2f}, Current: ${current_price:,.2f}")
                            place_order(symbol, "SELL", position['quantity'], current_price,
                                        existing_order_id=order_id)
                            _clear_trailing_stop(order_id)
                            await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                       "current_price": current_price,
                                                       "reason": f"Trailing stop hit (peak ${peak_price:,.2f}, "
                                                                 f"locked ~{locked_gain * 100:.1f}% gain)."})
                            continue

                    # Fixed stop-loss (always active as a floor)
                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[PAPER TRADE] Stop-loss hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id)
                        _clear_trailing_stop(order_id)
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price,
                                                   "reason": f"Stop-loss hit ({stop_loss_percentage * 100:.2f}% loss)."})

                    # Take profit (as ultimate cap)
                    elif pnl_percentage >= take_profit_percentage:
                        log.info(f"[PAPER TRADE] Take-profit hit for {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id)
                        _clear_trailing_stop(order_id)
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol, "current_price": current_price,
                                                   "reason": f"Take-profit hit ({take_profit_percentage * 100:.2f}% gain)."})

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new signal generation and trading.")
            continue

        # 2. Analyze data for a signal
        log.info(f"Analyzing data for {symbol}...")

        # Fetch enough data for regime detection + indicators
        price_limit = max(sma_period, rsi_period, 30) + 1
        historical_prices = get_historical_prices(symbol, limit=price_limit)
        historical_timestamps = get_transaction_timestamps_since(symbol.lower(), hours_ago=baseline_hours)

        market_price_data = {'current_price': current_price, 'sma': None, 'rsi': None}
        if len(historical_prices) >= sma_period:
            price_series = pd.Series(historical_prices)
            market_price_data['sma'] = price_series.rolling(window=sma_period).mean().iloc[-1]
        market_price_data['rsi'] = calculate_rsi(historical_prices, period=rsi_period)
        log.info(f"Technical Indicators for {symbol}: SMA={market_price_data['sma']}, RSI={market_price_data['rsi']}")

        # --- Market Regime Detection ---
        regime_data = detect_market_regime(historical_prices)
        regime = regime_data.get('regime', 'ranging')
        regime_params = regime_data.get('strategy_params', {})
        log.info(f"Market regime for {symbol}: {regime} (ADX={regime_data.get('adx')}, ATR%={regime_data.get('atr_pct')})")

        # --- Multi-Timeframe Confirmation ---
        mtf = multi_timeframe_confirmation(historical_prices, sma_period=sma_period, rsi_period=rsi_period)
        log.info(f"Multi-TF for {symbol}: {mtf['confirmed_direction']} ({mtf['agreement_count']}/3 agree)")

        transaction_velocity = calculate_transaction_velocity(symbol, whale_transactions, historical_timestamps,
                                                              baseline_hours)
        log.info(f"Transaction Velocity for {symbol}: {transaction_velocity}")

        # 3. Generate a signal
        log.info(f"Generating signal for {symbol}...")

        # Build per-symbol news sentiment data for the signal engine
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
            whale_transactions=whale_transactions,
            market_data=market_price_data,
            high_interest_wallets=high_interest_wallets,
            stablecoin_data=stablecoin_data,
            velocity_data=transaction_velocity,
            rsi_overbought_threshold=rsi_overbought_threshold,
            rsi_oversold_threshold=rsi_oversold_threshold,
            news_sentiment_data=symbol_news_data,
            historical_prices=historical_prices
        )
        log.info(f"Generated Signal for {symbol}: {signal}")

        # --- Multi-Timeframe & Regime Filter ---
        # Downgrade BUY/SELL to HOLD if multi-timeframe disagrees or regime is unfavorable
        original_signal = signal['signal']
        if original_signal in ("BUY", "SELL"):
            mtf_direction = mtf['confirmed_direction']
            signal_direction = 'bullish' if original_signal == 'BUY' else 'bearish'

            # In volatile regime, require 3/3 timeframe agreement
            min_agreement = regime_params.get('signal_threshold', 2)
            if regime == 'volatile' and mtf['agreement_count'] < 3:
                signal['signal'] = 'HOLD'
                signal['reason'] += f" [Filtered: volatile regime requires 3/3 TF agreement, got {mtf['agreement_count']}/3]"
                log.info(f"[{symbol}] Signal downgraded from {original_signal} to HOLD (volatile regime filter)")
            elif mtf_direction == 'mixed':
                signal['signal'] = 'HOLD'
                signal['reason'] += f" [Filtered: multi-TF mixed — no directional consensus]"
                log.info(f"[{symbol}] Signal downgraded from {original_signal} to HOLD (mixed multi-TF)")
            elif mtf_direction != signal_direction:
                signal['signal'] = 'HOLD'
                signal['reason'] += f" [Filtered: signal={signal_direction} but multi-TF={mtf_direction}]"
                log.info(f"[{symbol}] Signal downgraded from {original_signal} to HOLD (TF conflict)")

        # Annotate signal with regime info
        signal['regime'] = regime
        signal['mtf_direction'] = mtf['confirmed_direction']
        save_signal(signal)

        # --- 4. Paper Trading Logic with Dynamic Sizing ---
        if paper_trading:
            log.info(f"Processing signal for paper trading...")
            open_positions = get_open_positions()
            current_balance = get_account_balance().get('total_usd', paper_trading_initial_capital)

            # Apply regime risk multiplier to effective risk percentage
            risk_multiplier = regime_params.get('risk_multiplier', 1.0)
            adjusted_risk_pct = effective_risk_pct * risk_multiplier

            if signal['signal'] == "BUY":
                if any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in open_positions):
                    log.info(f"Skipping BUY for {symbol}: Position already open.")
                elif len(open_positions) >= max_concurrent_positions:
                    log.info(
                        f"Skipping BUY for {symbol}: Max concurrent positions ({max_concurrent_positions}) reached.")
                else:
                    capital_to_risk = current_balance * adjusted_risk_pct
                    quantity_to_buy = capital_to_risk / current_price
                    if quantity_to_buy * current_price > current_balance:
                        log.warning(f"Skipping BUY for {symbol}: Insufficient balance.")
                    else:
                        log.info(f"Executing paper trade: BUY {quantity_to_buy:.4f} {symbol} "
                                 f"(risk={adjusted_risk_pct:.4f}, regime={regime}).")
                        place_order(symbol, "BUY", quantity_to_buy, current_price)
                        await send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                position_to_close = next(
                    (p for p in open_positions if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if position_to_close:
                    log.info(f"Executing paper trade: SELL {position_to_close['quantity']:.4f} {symbol}.")
                    place_order(symbol, "SELL", position_to_close['quantity'], current_price,
                                existing_order_id=position_to_close['order_id'])
                    _clear_trailing_stop(position_to_close['order_id'])
                    await send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for {symbol}: No open position found.")
            elif signal['signal'] == "VOLATILITY_WARNING":
                log.info(f"VOLATILITY_WARNING for {symbol}. Suppressing new trades.")
                await send_telegram_alert(signal)
            else:  # HOLD
                log.info(f"Signal is HOLD for {symbol}. No trade action taken.")

    # --- Run Stock Trading Cycle ---
    await run_stock_cycle(settings, news_per_symbol=news_per_symbol,
                          news_config=news_config,
                          gemini_assessments=gemini_assessments)


async def run_stock_cycle(settings, news_per_symbol=None, news_config=None, gemini_assessments=None):
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

    watch_list = stock_settings.get('watch_list', [])
    if not watch_list:
        log.info("Stock watch list is empty. Skipping stock cycle.")
        return

    log.info(f"--- Starting stock trading cycle for {len(watch_list)} symbols ---")

    # Load stock-specific settings with fallbacks to shared settings
    sma_period = stock_settings.get('sma_period', settings.get('sma_period', 20))
    rsi_period = stock_settings.get('rsi_period', settings.get('rsi_period', 14))
    rsi_overbought = stock_settings.get('rsi_overbought_threshold', settings.get('rsi_overbought_threshold', 70))
    rsi_oversold = stock_settings.get('rsi_oversold_threshold', settings.get('rsi_oversold_threshold', 30))
    pe_buy = stock_settings.get('pe_ratio_buy_threshold', 25)
    pe_sell = stock_settings.get('pe_ratio_sell_threshold', 40)
    earnings_sell = stock_settings.get('earnings_growth_sell_threshold', -10)
    vol_multiplier = stock_settings.get('volume_spike_multiplier', 1.5)

    # Shared risk management settings
    paper_trading = settings.get('paper_trading', True)
    stop_loss_percentage = settings.get('stop_loss_percentage', 0.02)
    take_profit_percentage = settings.get('take_profit_percentage', 0.05)
    trade_risk_percentage = settings.get('trade_risk_percentage', 0.01)
    max_concurrent_positions = settings.get('max_concurrent_positions', 3)
    paper_trading_initial_capital = settings.get('paper_trading_initial_capital', 10000.0)
    trailing_stop_enabled = settings.get('trailing_stop_enabled', True)
    trailing_stop_activation = settings.get('trailing_stop_activation', 0.02)
    trailing_stop_distance = settings.get('trailing_stop_distance', 0.015)

    for symbol in watch_list:
        log.info(f"--- Processing stock: {symbol} ---")

        # Fetch current stock price
        price_data = get_stock_price(symbol)
        if not price_data or not price_data.get('price'):
            log.warning(f"Could not fetch current price for stock {symbol}. Skipping.")
            continue

        current_price = price_data['price']
        log.info(f"Current stock price for {symbol}: ${current_price:,.2f}")

        # --- Position Monitoring with Trailing Stop ---
        if paper_trading:
            open_positions = get_open_positions()
            for position in open_positions:
                if position['symbol'] == symbol and position['status'] == 'OPEN':
                    entry_price = position['entry_price']
                    pnl_percentage = (current_price - entry_price) / entry_price
                    order_id = position['order_id']

                    peak_price = _update_trailing_stop(order_id, current_price)
                    drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0

                    if trailing_stop_enabled and pnl_percentage >= trailing_stop_activation:
                        if drawdown_from_peak >= trailing_stop_distance:
                            locked_gain = (peak_price - entry_price) / entry_price
                            log.info(f"[PAPER TRADE] Trailing stop triggered for stock {symbol}.")
                            place_order(symbol, "SELL", position['quantity'], current_price,
                                        existing_order_id=order_id)
                            _clear_trailing_stop(order_id)
                            await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                       "current_price": current_price, "asset_type": "stock",
                                                       "reason": f"Trailing stop hit (peak ${peak_price:,.2f}, "
                                                                 f"locked ~{locked_gain * 100:.1f}% gain)."})
                            continue

                    if pnl_percentage <= -stop_loss_percentage:
                        log.info(f"[PAPER TRADE] Stop-loss hit for stock {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id)
                        _clear_trailing_stop(order_id)
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                   "current_price": current_price, "asset_type": "stock",
                                                   "reason": f"Stop-loss hit ({stop_loss_percentage * 100:.2f}% loss)."})
                    elif pnl_percentage >= take_profit_percentage:
                        log.info(f"[PAPER TRADE] Take-profit hit for stock {symbol}. Closing position.")
                        place_order(symbol, "SELL", position['quantity'], current_price,
                                    existing_order_id=order_id)
                        _clear_trailing_stop(order_id)
                        await send_telegram_alert({"signal": "SELL", "symbol": symbol,
                                                   "current_price": current_price, "asset_type": "stock",
                                                   "reason": f"Take-profit hit ({take_profit_percentage * 100:.2f}% gain)."})

        # --- Pause Check ---
        if not bot_is_running.is_set():
            log.info("Bot is paused. Skipping new stock signal generation.")
            continue

        # Fetch daily prices for technical analysis
        daily_data = get_daily_prices(symbol)
        if not daily_data or not daily_data.get('prices'):
            log.warning(f"Could not fetch daily prices for {symbol}. Skipping analysis.")
            continue

        prices = daily_data['prices']
        volumes = daily_data.get('volumes', [])

        # Calculate technical indicators locally
        sma_value = calculate_sma(prices, period=sma_period)
        rsi_value = calculate_rsi(prices, period=rsi_period)

        market_data = {
            'current_price': current_price,
            'sma': sma_value,
            'rsi': rsi_value
        }

        # Prepare volume data
        volume_data = {}
        if volumes:
            current_volume = volumes[-1] if volumes else None
            avg_volume = sum(volumes) / len(volumes) if volumes else None
            volume_data = {
                'current_volume': current_volume,
                'avg_volume': avg_volume,
                'price_change_percent': price_data.get('change_percent', 0)
            }

        # Fetch fundamental data
        fundamental_data = get_company_overview(symbol) or {}

        # Build per-symbol news sentiment data for the stock signal engine
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

        # Generate stock signal
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
            historical_prices=prices
        )
        signal['asset_type'] = 'stock'
        log.info(f"Generated Stock Signal for {symbol}: {signal}")
        save_signal(signal)

        # --- Paper Trading Logic (with dynamic sizing) ---
        if paper_trading:
            open_positions = get_open_positions()
            current_balance = get_account_balance().get('total_usd', paper_trading_initial_capital)

            # Use Kelly-based sizing if available
            stock_trade_stats = get_trade_history_stats()
            stock_kelly = stock_trade_stats.get('kelly_fraction', 0.0)
            stock_risk_pct = stock_kelly if (stock_kelly > 0 and stock_trade_stats.get('total_trades', 0) >= 10) else trade_risk_percentage

            if signal['signal'] == "BUY":
                if any(p['symbol'] == symbol and p['status'] == 'OPEN' for p in open_positions):
                    log.info(f"Skipping BUY for stock {symbol}: Position already open.")
                elif len(open_positions) >= max_concurrent_positions:
                    log.info(f"Skipping BUY for stock {symbol}: Max concurrent positions reached.")
                else:
                    capital_to_risk = current_balance * stock_risk_pct
                    quantity_to_buy = capital_to_risk / current_price
                    if quantity_to_buy * current_price > current_balance:
                        log.warning(f"Skipping BUY for stock {symbol}: Insufficient balance.")
                    else:
                        log.info(f"Executing paper trade: BUY {quantity_to_buy:.4f} {symbol} "
                                 f"(risk={stock_risk_pct:.4f}).")
                        place_order(symbol, "BUY", quantity_to_buy, current_price)
                        await send_telegram_alert(signal)

            elif signal['signal'] == "SELL":
                position_to_close = next(
                    (p for p in open_positions if p['symbol'] == symbol and p['status'] == 'OPEN'), None)
                if position_to_close:
                    log.info(f"Executing paper trade: SELL {position_to_close['quantity']:.4f} {symbol}.")
                    place_order(symbol, "SELL", position_to_close['quantity'], current_price,
                                existing_order_id=position_to_close['order_id'])
                    _clear_trailing_stop(position_to_close['order_id'])
                    await send_telegram_alert(signal)
                else:
                    log.info(f"Skipping SELL for stock {symbol}: No open position found.")
            else:
                log.info(f"Signal is HOLD for stock {symbol}. No trade action taken.")

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
        summary = get_trade_summary(hours_ago=interval_hours)
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


@app.on_event("startup")
async def startup_event():
    """
    On startup, initialize the Telegram bot, set the webhook,
    and start the background tasks.
    """
    global application
    log.info("Starting application...")

    # Initialize the Telegram application
    application = await start_bot()

    # Set the webhook. The URL must be passed as an environment variable.
    # For Google Cloud Run, this is often provided as `GOOGLE_CLOUD_RUN_SERVICE_URL`.
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

    # Start background tasks
    _background_tasks.append(asyncio.create_task(bot_loop()))
    _background_tasks.append(asyncio.create_task(status_update_loop()))
    log.info("Startup complete. Background tasks running.")


@app.on_event("shutdown")
async def shutdown_event_handler():
    """
    On shutdown, cancel background tasks and gracefully clean up.
    """
    log.info("Shutting down application...")

    # Cancel background tasks
    for task in _background_tasks:
        task.cancel()
    for task in _background_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    _background_tasks.clear()

    # Stop Telegram bot
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
    Validates the secret token header to prevent CSRF attacks.
    """
    if not application:
        log.error("Webhook received but application not initialized.")
        return {"status": "error", "message": "Bot not initialized"}, 500

    # Validate the Telegram secret token if configured
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
        async def collect_data():
            log.info("--- 📊 Collecting Market Data (Collect-only mode) ---")
            # We don't need to collect binance data here as it's done in the backfill script
            
            log.info("--- 🐋 Collecting Whale Alert Data (Collect-only mode) ---")
            whale_transactions = get_whale_transactions(min_value_usd=app_config.get('settings', {}).get('min_whale_transaction_usd', 1000000))
            log.info(f"Fetched {len(whale_transactions)} whale transactions.")

        asyncio.run(collect_data())
    else:
        port = int(os.environ.get("PORT", 8080))
        log.info(f"Starting Uvicorn server on port {port}...")
        uvicorn.run(app, host="0.0.0.0", port=port)

